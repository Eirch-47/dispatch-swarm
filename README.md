# Dispatch Swarm

**14 emergency units. No central dispatcher. Every decision is a Drex probability.**

Built for the [Drexathon](https://luma.com/nace-drexathon) (Nace AI Decision Model Hackathon × Adobe EDU) — **Swarm Intelligence track**, 14 November 2026.

Each responder unit is an autonomous agent that chooses its own incident by asking Drex a typed question and acting on the probabilities it returns. No text is generated anywhere in the system. When several units want the same incident, a second round of Drex calls has them negotiate until the best-placed units keep their claims.

---

## The finding

We didn't just build the swarm — we benchmarked it. Every run plays an identical, seeded incident stream through **two policies at once**: the Drex swarm, and a classic greedy dispatcher (oldest incident first, nearest matching unit). Same city, same 14 units, same emergencies.

The result is more interesting than "our AI wins":

| Scenario | Units vs. demand | Outcome |
|---|---|---|
| Rush hour | Enough units | **Greedy wins.** 100% vs 93–100% severity served, and faster (4.2 s vs 6.7–9.5 s to first responder) |
| Earthquake | Short early | Close, varies by run |
| Festival crowd | Moderately short | Close |
| Harbor fires | 9 fires, 4 fire units | **Drex dominates.** Greedy collapses here |

**A decision model cannot beat a simple rule on a problem that contains no decision.** When there are enough ambulances for every call, "send the nearest one" is already optimal, and Drex only pays latency to match it.

Scarcity is where it flips. In Harbor fires there are nine fires — many severity 4–5 requiring two units each — and only four fire units in the city. Something *must* be sacrificed. Greedy's oldest-first rule has no concept of severity, so it sends crews to stale minor calls while critical fires escalate. Drex weighs severity, distance, wait time and existing coverage in a single judgment, and the gap opens up.

> The boundary for decision models in agent systems isn't raw capability — it's whether the problem has tradeoffs in it.

**Honest caveat:** part of greedy's speed advantage is structural, not strategic. It re-plans every 0.3 simulated seconds with no network call, while each swarm unit waits on a real API round trip (~400 ms measured).

---

## How it works

```
index.html (browser)                 server.py                 Drex API
  simulation + map                     proxy                 drex-max-v1.1
        │                                │                         │
        │  1 request, 1 named question   │                         │
        │     per free unit              │   + API key             │
        ├───────────────────────────────►├────────────────────────►│
        │                                │                         │
        │   probabilities per option     │                         │
        │◄───────────────────────────────┤◄────────────────────────┤
        │                                │
   claim → negotiate → dispatch
```

**Each tick (~1 s):**

1. **Build.** For every free unit, describe its five most relevant open incidents in plain language — `"fire emergency, severity 4/5, 2.1 km from you, needs 1 more unit, waiting 12s in Harbor"` — plus a `HOLD` option.
2. **Claim.** All units' questions travel in **one** batched request. Drex returns a probability per option per unit. Each unit takes its highest.
3. **Negotiate.** Where claimants exceed open slots, a second batched request asks each one a yes/no question naming its rivals' distances. Highest "keep" probabilities win; the rest yield and are free next tick.
4. **Dispatch.** Winners drive, arrive, and work the incident down. Unattended incidents escalate in severity every 14 s and are lost after 70 s.

The API key never reaches the browser — it lives in `.env` and is read only by the server.

### Drex question types used

All three typed-question kinds, each doing a job a chat model would do worse:

| Type | Returns | Used for |
|---|---|---|
| `choice` | Probability per named option | Which incident should this unit take, or HOLD? Also a report's emergency type, severity and district |
| `noul` | One probability of "yes" | Should this unit keep its claim, or yield to a better-placed rival? |

A chat model would emit a sentence per unit that the code must then parse. Drex returns numbers directly — the choice *is* the argmax, and the probabilities themselves become the negotiation signal. Click any unit in the UI to see the exact distribution behind its last decision.

### Document triage (messy input → dispatched response)

Paste a forwarded email, an SMS dump or an OCR'd scan into the ingest panel. Three Drex `choice` questions extract emergency type, severity (1–5) and district, and the result becomes a live incident on the map in well under a second. `samples/` contains five deliberately messy reports.

NDI (Nace Document Intelligence) is wired in for PDFs and scans — install `ndi-sdk` and set `NDI_API_KEY` to enable it. **It was not exercised in this build**; plain text and `.eml` work without it.

---

## Run it

```bash
pip install requests          # the only dependency
cp .env.example .env          # add DREX_API_KEY=nace_sk_...
python3 server.py             # → http://localhost:8000
```

Pick a scenario, press **Run benchmark**, and switch the view menu to **Split** to watch both policies race on the same emergencies. An after-action report appears at 90 simulated seconds with a **Copy results** button.

Without a key the app runs in **mock mode** (yellow badge, warning on the report) against a local stand-in, so the UI can be developed offline. Mock numbers are not Drex results.

**Rate limits.** The first version fired 14 parallel requests per tick and Drex answered 429. Each round is now a single request carrying one named question per unit, with a concurrency cap (`DREX_CONCURRENCY`, default 3) and exponential-backoff retry on 429/5xx.

### Reading the map

| Element | Meaning |
|---|---|
| Labelled dot with a trail | A unit — green medic, orange fire, blue rescue |
| Glowing ring with a number | An incident; higher severity glows wider and pulses faster |
| Bars above an incident | Units needed; filled = already assigned |
| Purple dotted line | A claim being decided this tick |
| White arc / red arc | Work progress / time until the incident is lost |

---

## What's real and what's simulated

The city, the units and the emergencies are generated by the code — seeded, so every scenario replays identically and both policies face exactly the same conditions. **What's real is the decision-making:** every unit choice, every negotiation and every triage is a live call to Drex, measured end to end.

Measured on a live run: **849 decisions across 115 batched requests, ~400 ms per request, ~$0.006 estimated input cost.**

## Known limits

- Each unit's five candidate incidents are pre-ranked by a simple heuristic before Drex chooses among them. Drex makes the decision; the shortlist is ordinary code.
- "Units only help incidents of their own type" is a prompt instruction, not a hard constraint.
- Units travel in straight lines at a fixed speed; the escalation and loss rules are invented for the simulation.
- NDI document parsing is implemented but unverified.

## Files

| File | Contents |
|---|---|
| `server.py` | Static serving, `/api/decide` (Drex proxy + mock), `/api/ingest` (report triage), `/api/status` (mode, counts, last raw Drex response) |
| `index.html` | Simulation, both policies, scenarios, map renderer, inspector, after-action report |
| `samples/` | Five messy field reports |

## Sources

- [Drexathon event page](https://luma.com/nace-drexathon)
- [Drex model API reference](https://haimaker.ai/models/nace-ai/drex-v1.5)
- [NDI Python SDK](https://pypi.org/project/ndi-sdk/)
