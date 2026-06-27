# Quantum-Only Demo

This package contains only the damages/quantum demo pieces:

- `quantum/` - Python damages calculator and tests
- `web/quantum.html` - frontend page
- `api.py` - minimal backend exposing `GET /api/quantum`
- `data_cms/` - the three source documents used by the quantum citations
- `verify_quantum_grounding.py` - optional citation check

It intentionally excludes the rest of the hackathon app.

## Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn api:app --port 8000
```

Open:

```text
http://127.0.0.1:8000/quantum.html
```

## API Modes

Default demo case:

```text
GET /api/quantum
```

New structured dataset input:

```text
POST /api/quantum
```

The `POST` body should look like `example_quantum_spec.json`.

The frontend title is data-driven. The API response field:

```json
{"case": "New Claimant v New Supplier [CASE-ID]"}
```

controls the visible page title and browser tab title. When a teammate connects a
new case, update the `case` value in the backend response/spec and the page title
will change automatically.

Important: raw `.txt` documents do not go straight into the formula engine. A new
case must first be converted into structured figures:

- wasted expenditure
- loss of profit
- causation deductions
- contract exclusions/caps
- legal probability inputs
- citations for each figure

After that, the same quantum formula/engine can run on the new case.

## Verify

```bash
python -m quantum.test_quantum
python verify_quantum_grounding.py
```

Expected headline numbers:

| Step | Amount |
|---|---:|
| Pleaded cost + value | GBP 6,000,000 |
| Less causation | GBP -2,900,000 |
| Supportable pre-legal | GBP 3,100,000 |
| Exclusion + cap hold | GBP 1,800,000 |
| Fraud proven | GBP 3,100,000 |
| Expected recoverable | GBP 2,242,000 |

## Product Note

For GitHub, push this folder as a normal repo. For a live product, deploy it as a
Python backend app. GitHub Pages alone can only show the static HTML fallback; it
cannot run `api.py`.
