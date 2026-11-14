#!/usr/bin/env python3
"""Dispatch Swarm backend.

Serves the UI and proxies decisions to Drex (Nace AI's decision model).
Optionally ingests messy documents through NDI (Nace Document Intelligence).

Env vars (put them in .env next to this file, or export them):
  DREX_API_KEY    bearer key for the Drex API          (omit -> MOCK mode)
  DREX_BASE_URL   default https://drex.nace.ai
  DREX_MODEL      default drex-latest
  DREX_PATH       default /v1/systemone
  NDI_API_KEY     key for NDI                          (omit -> text-only ingest)
  NDI_BASE_URL    default https://ndi-api.nace.ai
"""
import base64, json, math, os, random, re, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).parent


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
DREX_KEY = os.environ.get("DREX_API_KEY", "")
DREX_URL = os.environ.get("DREX_BASE_URL", "https://drex.nace.ai").rstrip("/")
DREX_MODEL = os.environ.get("DREX_MODEL", "drex-latest")
DREX_PATH = os.environ.get("DREX_PATH", "/v1/systemone")
NDI_KEY = os.environ.get("NDI_API_KEY", "")

try:
    from ndi_sdk import NdiClient  # type: ignore
    HAVE_NDI_SDK = True
except Exception:
    HAVE_NDI_SDK = False

STATS = {"calls": 0, "errors": 0, "lat_ms_total": 0.0, "in_chars": 0, "last_raw": None, "last_error": None}
LOCK = threading.Lock()
SEM = threading.Semaphore(int(os.environ.get("DREX_CONCURRENCY", 3)))


# ---------------------------------------------------------------- normalising
def _numeric(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def find_probs(node, options):
    """Find a {option: prob} mapping (or aligned list) anywhere inside node."""
    if isinstance(node, dict):
        if options and all(o in node and _numeric(node[o]) for o in options):
            return {o: float(node[o]) for o in options}
        for v in node.values():
            r = find_probs(v, options)
            if r:
                return r
    elif isinstance(node, list):
        if options and len(node) == len(options) and all(_numeric(v) for v in node):
            return {o: float(v) for o, v in zip(options, node)}
        for v in node:
            r = find_probs(v, options)
            if r:
                return r
    return None


def normalise(ans, q):
    t = q.get("type")
    if t == "choice":
        options = list((q.get("criteria") or {}).keys())
        probs = find_probs(ans, options)
        chosen = None
        if isinstance(ans, dict):
            for k in ("choice", "selected", "answer", "label"):
                if isinstance(ans.get(k), str) and ans[k] in options:
                    chosen = ans[k]
        if probs:
            s = sum(probs.values()) or 1.0
            probs = {k: v / s for k, v in probs.items()}
            chosen = chosen or max(probs, key=probs.get)
        return {"choice": chosen, "probs": probs or {}}
    if t == "noul":
        p = None
        if isinstance(ans, dict):
            for k in ("noul", "probability", "p", "yes", "true"):
                if _numeric(ans.get(k)):
                    p = float(ans[k])
                    break
        return {"p": p}
    if t == "score":
        out = {"score": None, "probs": {}}
        if isinstance(ans, dict):
            for k in ("score", "expected", "value", "mean"):
                if _numeric(ans.get(k)):
                    out["score"] = float(ans[k])
                    break
        return out
    return {"raw": ans}


# ------------------------------------------------------------------- mock
def mock_decide(body):
    """Offline stand-in so the demo runs without keys. Clearly flagged in the UI."""
    time.sleep(random.uniform(0.12, 0.22))
    hints = body.get("_mock", {})
    answers = {}
    for name, q in body["questions"].items():
        h = hints.get(name, {})
        if q["type"] == "choice":
            opts = list(q["criteria"].keys())
            logits = [h.get(o, 0.0) for o in opts]
            m = max(logits)
            ex = [math.exp(l - m) for l in logits]
            s = sum(ex)
            probs = {o: e / s for o, e in zip(opts, ex)}
            answers[name] = {"choice": max(probs, key=probs.get), "probs": probs}
        elif q["type"] == "noul":
            answers[name] = {"p": 1 / (1 + math.exp(-h.get("logit", 0.0)))}
        else:
            answers[name] = {"score": h.get("score", 3.0), "probs": {}}
    return answers


# ------------------------------------------------------------------ drex call
def drex_decide(body):
    t0 = time.time()
    mode = "live" if DREX_KEY else "mock"
    try:
        if mode == "live":
            payload = {k: v for k, v in body.items() if not k.startswith("_")}
            payload["model"] = DREX_MODEL
            with SEM:
                for attempt in range(5):
                    r = requests.post(
                        DREX_URL + DREX_PATH,
                        headers={"Authorization": f"Bearer {DREX_KEY}", "Content-Type": "application/json"},
                        json=payload, timeout=30,
                    )
                    if r.status_code in (429, 502, 503, 504) and attempt < 4:
                        wait = float(r.headers.get("Retry-After", 0) or 0) or (0.5 * 2 ** attempt)
                        time.sleep(min(wait, 6))
                        continue
                    break
            r.raise_for_status()
            raw = r.json()
            with LOCK:
                STATS["last_raw"] = raw
            raw_ans = raw.get("answers", raw)
            answers = {n: normalise(raw_ans.get(n), q) for n, q in body["questions"].items()}
        else:
            answers = mock_decide(body)
        lat = (time.time() - t0) * 1000
        with LOCK:
            STATS["calls"] += 1
            STATS["lat_ms_total"] += lat
            STATS["in_chars"] += len(json.dumps(body.get("state", "")))
        return {"answers": answers, "latency_ms": round(lat, 1), "mode": mode}
    except Exception as e:  # surface, never crash the swarm
        with LOCK:
            STATS["errors"] += 1
            STATS["last_error"] = repr(e)
        return {"error": repr(e), "mode": mode, "latency_ms": round((time.time() - t0) * 1000, 1)}


# ------------------------------------------------------------------ ingest
KIND_WORDS = {
    "fire": ["fire", "smoke", "blaze", "burning", "flames", "explosion", "gas leak"],
    "medic": ["injur", "unconscious", "heart", "bleeding", "ambulance", "patients", "casualt", "cardiac"],
    "rescue": ["trapped", "collapse", "flood", "stranded", "rubble", "submerged", "landslide"],
}


def read_document(name, data_b64, text):
    """Return (text, source). Uses NDI for binary docs when available."""
    if text:
        return text, "text"
    raw = base64.b64decode(data_b64 or "")
    ext = Path(name or "").suffix.lower()
    if ext in (".txt", ".eml", ".md", ".csv", ".json", ".log", ""):
        return raw.decode("utf-8", "replace"), "text"
    if HAVE_NDI_SDK and NDI_KEY:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / (name or "doc.pdf")
            p.write_bytes(raw)
            with NdiClient(api_key=NDI_KEY) as c:
                up = c.documents.create_upload(str(p))
                job = c.jobs.wait(c.documents.parse(up, wait_seconds=60).job_id, timeout=120)
                return job.result.markdown, "ndi"
    raise RuntimeError("Binary document needs NDI: `pip install ndi-sdk` and set NDI_API_KEY (text/.eml files work without it)")


def triage(body):
    text, source = read_document(body.get("name"), body.get("data"), body.get("text"))
    text = text[:6000]
    districts = body["districts"]
    low = text.lower()
    hints = {"kind": {k: sum(low.count(w) for w in ws) * 1.5 for k, ws in KIND_WORDS.items()}}
    sev = 2.0 + sum(low.count(w) for w in ["critical", "multiple", "children", "trapped", "spreading", "urgent", "mass", "explod", "still inside", "not answering", "sinking", "cardiac", "unconscious", "bleeding", "hurry", "asap", "rubble", "flames"]) * 0.6
    sev = max(1.0, min(5.0, sev))
    hints["severity"] = {str(i): -abs(i - sev) * 2.0 for i in range(1, 6)}
    hints["district"] = {d: (3.0 if d.lower() in low else 0.0) for d in districts}
    req = {
        "state": text,
        "questions": {
            "kind": {"type": "choice", "instructions": "What kind of emergency response does this report need most?",
                     "criteria": {"fire": "fire, smoke, explosion or gas leak", "medic": "injured or ill people needing medical care",
                                  "rescue": "people trapped, stranded, or structural collapse or flooding"}},
            "severity": {"type": "choice", "instructions": "How severe is this incident?",
                         "criteria": {"1": "minor: nobody hurt, no immediate danger",
                                      "2": "low: small scale and contained, no serious injury",
                                      "3": "moderate: injuries or damage, needs a prompt response",
                                      "4": "serious: several people at risk or seriously hurt",
                                      "5": "critical: life-threatening and still escalating"}},
            "district": {"type": "choice", "instructions": "Which district is this incident located in?",
                         "criteria": {d: f"the {d} district" for d in districts}},
        },
        "_mock": hints,
    }
    res = drex_decide(req)
    if "error" in res:
        return res
    a = res["answers"]
    try:
        sc = int(a["severity"]["choice"])
    except (KeyError, TypeError, ValueError):  # unexpected shape -> fall back to keyword severity
        sc = sev
    summary = " ".join(text.split())[:140]
    return {"incident": {"kind": a["kind"]["choice"], "severity": max(1, min(5, round(sc))),
                         "district": a["district"]["choice"], "summary": summary},
            "source": source, "latency_ms": res["latency_ms"], "mode": res["mode"]}


# ------------------------------------------------------------------ http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/status":
            with LOCK:
                n = max(STATS["calls"], 1)
                return self._json({
                    "drex_mode": "live" if DREX_KEY else "mock", "model": DREX_MODEL,
                    "ndi": bool(NDI_KEY and HAVE_NDI_SDK), "calls": STATS["calls"], "errors": STATS["errors"],
                    "avg_latency_ms": round(STATS["lat_ms_total"] / n, 1), "last_error": STATS["last_error"],
                    "last_raw": STATS["last_raw"]})
        path = "index.html" if self.path in ("/", "") else self.path.lstrip("/").split("?")[0]
        f = (ROOT / path).resolve()
        if not f.is_file() or not (f == ROOT / "index.html" or (ROOT / "samples") in f.parents):
            self.send_error(404)
            return
        b = f.read_bytes()
        ctype = {"html": "text/html", "txt": "text/plain", "js": "text/javascript", "css": "text/css"}.get(f.suffix[1:], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/decide":
                return self._json(drex_decide(body))
            if self.path == "/api/ingest":
                return self._json(triage(body))
            self.send_error(404)
        except Exception as e:
            self._json({"error": repr(e)}, 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Dispatch Swarm on http://localhost:{port}  | Drex: {'LIVE' if DREX_KEY else 'MOCK (no DREX_API_KEY)'}  | NDI: {'on' if NDI_KEY and HAVE_NDI_SDK else 'off'}")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
