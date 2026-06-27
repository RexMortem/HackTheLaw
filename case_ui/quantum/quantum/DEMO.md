# Quantum demo — runbook

What it shows: the damages claim of **~£6,000,000** reduced, step by step, to an
**expected recoverable of ~£2,242,000** — four valuation methods, a causation
haircut, then the legal-constraint layer (contract clauses → enforceability).
Every figure cites the bundle. **No API key, no internet, no model call.**

---

## Option A — terminal (most foolproof, 1 command)

```bash
cd "hackathon lawxllm"
python3 -m quantum
```

Prints the full waterfall as text. If this runs, the demo works. Nothing else
required — pure standard-library Python.

To prove the maths is locked:

```bash
python3 -m quantum.test_quantum      # -> "ok — all quantum assertions pass"
```

## Option B — browser (the visual)

```bash
cd "hackathon lawxllm"
python3 -m venv .venv && source .venv/bin/activate   # first time only
pip install fastapi uvicorn                          # first time only
uvicorn api:app --port 8000
```

Then open **http://localhost:8000/quantum.html**

You get the waterfall bars, the four heads of loss, the clause table with
P(upheld), and the scenario tree. The page calls `GET /api/quantum` — the
calculator only; it does **not** need `data/chunks.json` or an API key.

---

## What to say (30 seconds)

1. "The claim is pleaded at six million. Watch what's actually recoverable."
2. **Causation** — the expert strips £2.9m (the Lutterworth flood and a downturn
   that predates go-live). Down to £3.1m.
3. **The clause** — MSA clause 14.1 excludes loss of profit and 14.2 caps liability
   at the £1.8m paid. *"The biggest method — loss of profit — is the one the
   contract kills. The smallest — wasted expenditure — is the one that survives."*
4. **The unlock** — clause 14.3: prove the pleaded misrepresentation as fraud and
   both the exclusion and the cap fall away, putting the full £3.1m back in play.
5. **Expected recoverable £2.24m** — probability-weighted across those legal
   outcomes. *"Not a guess at the harm — a defensible number for what's collectible."*

---

## If something goes wrong

| symptom | fix |
|---|---|
| `python3 -m quantum` errors | you're not in the project root — `cd` into `hackathon lawxllm` |
| browser page shows a red box | server isn't up — run the `uvicorn` line in Option B |
| `uvicorn: command not found` | `pip install fastapi uvicorn` (Option B, first-time line) |
| want different odds | edit `ENFORCEABILITY` / `FRAUD` in `quantum/case_meridian.py`, re-run |

The `/api/analyze` route (the LLM case-theory engine) is separate and needs
`data/chunks.json` + an API key. **The quantum demo needs neither** — keep the
demo on quantum if the network or key is unavailable.
