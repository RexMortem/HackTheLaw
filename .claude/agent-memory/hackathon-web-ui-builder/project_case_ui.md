---
name: project-case-ui
description: LLM x Law hackathon project — pleading-to-proof matrix web UI with two-step goals flow
metadata:
  type: project
---

Case UI is a litigation analysis tool that maps pleaded allegations to evidence.
It now has a two-step flow: Step 1 (goals entry) -> Step 2 (matrix analysis filtered by goals).

**Stack:** Python stdlib HTTP server (`case_ui/app.py`, port 8001) + single `index.html`
(inline CSS + vanilla JS). Zero build step, zero npm.

**Two-step flow:**
- Step 1: User enters litigation-review goals (free text or suggestion chips). Saved to `case_ui/data/goals.json`.
- Step 2: Full matrix UI with a goals banner. Clicking a goal pill filters propositions by keyword match and shows a coverage summary.
- If `case_ui/data/goals.json` exists on load, Step 1 is skipped. "Edit goals" link in header returns to Step 1.
- Delete `case_ui/data/goals.json` to reset to Step 1.

**API endpoints:**
- `GET /api/matrix` — full matrix + summary stats
- `GET /api/status` — health check (files present, goals exist)
- `GET /api/goals` — return saved goals (`{}` if none)
- `POST /api/goals` — save goals; body: `{"goals": [{id, text}, ...]}`; writes `case_ui/data/goals.json`

**Goals JSON schema (`case_ui/data/goals.json`):**
```json
{"saved_at": "ISO-8601", "goals": [{"id": "g1", "text": "..."}, ...]}
```

**Goal matching (keyword-based):** splits goal text into words >3 chars, matches against
proposition text + type + party + witness + rationale + quotes. No LLM call needed.
Future: pass goals to build_matrix.py prompt for LLM-scored relevance.

**Data schema (matrix.json):** Array of `{ proposition: {id, type, text, party, responds_to, quote, quote_ok, source: {doc_id, page, paragraph}}, status: "supported"|"contested"|"undermined"|"MISSING", n_candidates: int, links: [{evidence_id, relation, confidence, rationale, quote, quote_ok, witness, evidence_source: {doc_id, page, paragraph}}] }`.

**Status values:** supported / contested / undermined / MISSING.

**Trial readiness formula:** `(supported*1.0 + contested*0.5) / total * 100`.

**Demo mode:** Set `$env:CASE_UI_DEMO = "1"` to serve `out/matrix.sample.json` instead of `out/matrix.json`.

**Run command:** `python case_ui/app.py` (from repo root). URL: http://localhost:8001

**Python binary on this machine:** `C:\Users\Edwar\AppData\Local\Programs\Python\Python314\python.exe`

**Key files:**
- `case_ui/app.py` — HTTP server with goals API
- `case_ui/index.html` — two-step single-page UI
- `case_ui/README.md` — updated docs
- `case_ui/data/goals.json` — persisted goals (created on first save; delete to reset)
- `out/matrix.sample.json` — demo fixture
- `CHANGES.md` — project-root changelog

**Why:** Added two-step flow so users frame their review goals before seeing the matrix,
making the analysis feel purposeful and judge-demo-ready.

**How to apply:** When iterating on the UI, edit `case_ui/index.html` and `case_ui/app.py`.
Don't invent new data shapes — match the matrix schema above. Goals keyword matching
uses words >3 chars from goal.text against the haystack built by buildSearchHay().
