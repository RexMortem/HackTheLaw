# quantum — damages calculator

Self-contained module that answers **"how much is actually recoverable?"** — not
just "how big was the loss?". Pure stdlib: no API key, no model call. Run it:

```bash
python -m quantum            # the Meridian v TechFlow assessment
python -m quantum.test_quantum   # lock the math
```

## The pipeline

```
 4 methods         causation          legal constraints (2 steps)
┌──────────┐      ┌──────────┐       ┌──────────────────────────────┐
│ cost     │      │ strip    │       │ 3a contract: clause 14.1/14.2 │
│ value    │ ───▶ │ non-     │ ────▶ │ 3b law: is each clause        │ ──▶ expected
│ market*  │      │ defendant│       │    enforceable? (UCTA) +       │     recoverable
│ dcf*     │      │ causes   │       │    fraud carve-out 14.3        │
└──────────┘      └──────────┘       └──────────────────────────────┘
  * ungrounded — no bundle data, kept out of the headline
```

The key idea: **the method that yields the biggest number (value/DCF) is the most
attackable; the smallest (cost) is the most recoverable.** The calculator makes
that explicit instead of reporting a single headline.

## Why two steps in the legal layer

- **3a (contract)** — clause 14.1 *says* loss of profit is excluded; 14.2 *says*
  liability is capped at charges paid (~£1.8m). These are what the parties wrote.
- **3b (law)** — a clause is not automatically valid. Under **UCTA 1977** a court
  decides whether each exclusion/cap is *reasonable*. So the output is
  **probability-weighted**, not binary: `£X × P(clause upheld)`. And clause **14.3**
  (fraud carve-out) means proving the pleaded misrepresentation removes *both* the
  exclusion and the cap — the route that unlocks the full claim.

## Grounding

Every £ figure carries a bundle citation (`doc_id ¶para` + quote); a figure with
no source is `grounded=False` and excluded from the headline. The probabilities in
`ENFORCEABILITY` / `FRAUD` are **legal-judgement inputs**, explicitly flagged as
*not* facts from the bundle — tune them in `case_meridian.py`.

> The statutes (UCTA s.3/s.11) are **not** in `data_cms/`. The calculator models
> the contract-as-written and treats enforceability as a tunable probability — it
> does not invent case law. Supply real legal analysis to refine the inputs.

## Files

| file | role |
|---|---|
| `models.py` | dataclasses — `Cited`, `HeadOfLoss`, `ContractClause`, … |
| `methods.py` | the four valuation methods |
| `constraints.py` | causation + the 3a/3b legal scenario tree |
| `waterfall.py` | orchestration + text rendering |
| `case_meridian.py` | the bundle-grounded inputs for this case |
| `test_quantum.py` | locks the headline numbers |

## Result (Meridian v TechFlow)

| step | £ |
|---|---|
| Pleaded (cost + value) | 6,000,000 |
| Less causation (flood + downturn + unsupported) | −2,900,000 |
| Supportable (pre-legal) | 3,100,000 |
| If exclusion + cap hold (defendant's best) | 1,800,000 |
| If fraud proven (claimant's best) | 3,100,000 |
| **Expected recoverable (weighted)** | **2,242,000** |

To plug into the LLM pipeline, feed the figures `engine.py` extracts (each already
carrying a `doc_id`/`para`) straight into `Cited(...)` and call `quantum.compute`.
