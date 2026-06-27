# LLM × Law: Pleading-to-Proof

Our entry for the CMS Pleading-to-Proof challenge. It takes a litigation bundle
(a set of `.docx` documents) and stress-tests the case theory: it extracts every
pleaded allegation and denial, maps each one to the available evidence, and
labels it **supported**,
**contested**, **undermined**, or **missing**. The result is a pleading-to-proof
matrix with a trial-readiness score, an AI case summary, and a built-in
litigation assistant you can chat with.

---

## Run it

### Quick start: the web app

```bash
pip install -r requirements.txt
python case_ui/app.py
```

Then open **http://localhost:8001**.

It works out of the box: a sample proof matrix is bundled with the repo
(`case_ui/data/matrix.json`), so the matrix, filters, goals, and risk view all
run with no API key and no pipeline run.

To enable the AI case summary and the Second Chair chat assistant, set an
Anthropic API key first (these features degrade gracefully without one):

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY="..."
```

### Regenerate the matrix from the source bundle (optional)

The bundled matrix is pre-computed. To rebuild it from the raw `.docx` bundle,
run the **preprocessing step first**, then the pipeline:

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# PREPROCESSING — run this BEFORE the app/pipeline. It is NOT part of the app;
# it converts the .docx case bundle into LLM-readable Markdown, once.
python convert_docx.py     # dataset/<bundle>/*.docx -> bundle_md/*.md

# Pipeline (reads the preprocessed bundle_md/)
python extract.py          # bundle_md/  -> out/propositions.json, out/evidence.json
python build_matrix.py     # the above   -> out/matrix.{json,csv,md}
python case_ui/snapshot_data.py   # copy out/matrix.json -> case_ui/data/ for the app
```

Cheap dry run first: `python extract.py --mode sync --limit 3`.

> **Preprocessing is a separate, offline step.** `convert_docx.py` is never
> invoked by the running app — the app only reads the matrix produced downstream.
> Re-run it only when the source `.docx` bundle changes.

---

## What we built

In complex litigation, the pleadings set out dozens of allegations and denials,
but the evidence that proves or disproves them is scattered across witness
statements, exhibits, correspondence, and contracts. Working out which parts of
the case are actually evidenced is slow, manual cross-referencing, and teams
often discover a weak point far too late.

Our tool does that cross-referencing automatically. It reads the bundle, pulls
out each atomic pleaded proposition, finds the evidence that bears on it, and
decides whether that evidence **supports**, **contests**, **undermines**, or
**fails to address** the proposition. The result is a single matrix that shows,
at a glance, where the case is strong, where it is vulnerable, and where there
is no evidence at all.

On top of that matrix, the web app lets a lawyer state what they care about
(e.g. "contradictions in the Defence", "allegations with no witness support")
and instantly filters the case to those points. A header trial-readiness score
rolls the whole bundle into one number. An AI case summary explains, in plain
English, what the dispute is actually about, grounded by a live web search and by
relevant EU legal materials, and folds in the proof figures. And "Second Chair",
an embedded AI associate, answers free-text questions about the case, citing the
exact propositions it relies on.

Every cell is auditable: each link back to the evidence carries the source
document, page, and paragraph, plus a verbatim quote that is checked against the
original text, so nothing in the matrix is a hallucination.

---

## How it works (overview)

The system is two halves:

1. **Offline pipeline:** turns the `.docx` case bundle into the proof matrix. A
   preprocessing step (`convert_docx.py`) plus the pipeline (`extract.py` →
   `build_matrix.py`) run in sequence, before the app.
2. **Web app** (`case_ui/`): serves the matrix as an interactive UI and adds the
   goals, AI summary, and chat features on top.

```
.docx bundle ─convert_docx.py─▶ bundle_md/*.md ─extract.py─▶ out/propositions.json
                                                 │                out/evidence.json
                                                 └─build_matrix.py─▶ out/matrix.{json,csv,md}
                                                                             │
                                                                 case_ui/app.py ──▶ browser UI
```

Key design choices, in brief:

- **Two layers, one mapping:** allegations and denials come from the *pleadings*;
  witness statements and exhibits are *evidence*. The product is the mapping
  between them.
- **Atomic + provenance:** one claim per record, each tied to a source paragraph
  and a quote verified against the text, so every matrix cell is checkable.
- **Retrieval + classification:** for each proposition we retrieve candidate
  evidence (BM25 by default, with legal-tuned embeddings optional) and have
  Claude classify each candidate as supportive, adverse, or neutral.
- **Grounded AI, graceful fallback:** the summary and chat are grounded in the
  matrix (and external sources for the summary); if no API key is present they
  fall back to deterministic, matrix-derived answers rather than failing.

---

## Technical detail

### The pipeline

```
.docx bundle ─convert_docx.py─▶ bundle_md/*.md ─extract.py─▶ out/propositions.json
                                                 │                out/evidence.json
                                                 └─build_matrix.py─▶ out/matrix.{json,csv,md}
```

0. **`convert_docx.py` (preprocessing, run before the app):** converts the
   `.docx` case bundle into LLM-readable Markdown in `bundle_md/`, matching the
   format the pipeline expects (frontmatter, a `<!-- page 1 -->` marker,
   blank-line-separated paragraphs with leading numbers preserved, tables
   rendered as Markdown). Pure stdlib (`zipfile` + ElementTree); no app
   involvement. Supersedes the deprecated PDF path (see [Challenges](#challenges)).
1. **`extract.py`:** reads `bundle_md/`, classifies each doc (pleading, witness
   statement, or exhibit), and extracts atomic propositions from pleadings and
   evidence units from the rest. Every record carries provenance (doc_id, page,
   paragraph) and a verbatim quote verified against the source (anti-hallucination
   gate).
2. **`build_matrix.py`:** BM25-retrieves candidate evidence per proposition, has
   Claude classify each candidate, and rolls up the four-bucket matrix.

**Why this design**

- **Structured outputs** (`output_config.format`) for schema-valid records.
- **Batch API** (50% cheaper) for per-document extraction, with **prompt
  caching** on the shared instruction prefix.
- **Model:** `claude-sonnet-4-6`, adaptive thinking, `effort: high`.

**Retrieval and scoring knobs**

- **Top-k:** `build_matrix.py --top-k N` controls how many candidate evidence
  units are considered per proposition (default 25). Higher means better
  adverse-evidence recall at higher cost. The count is logged; no silent
  truncation.
- **Retriever:** `--retriever bm25` (default, no extra deps), `embeddings`, or
  `hybrid`. Embeddings and hybrid use **Voyage AI** (`voyage-law-2`,
  legal-domain tuned) and need `VOYAGE_API_KEY`; vectors cache in
  `out/emb_*.npz`. Hybrid fuses BM25 and embeddings via reciprocal-rank fusion,
  best for catching adverse evidence worded differently from the allegation:
  ```bash
  export VOYAGE_API_KEY=pa-...
  python build_matrix.py --retriever hybrid --top-k 40
  ```
- **Confidence:** `--min-confidence` (default 0.5) gates which links count toward
  a proposition's status and appear in the report.

### The web app

`case_ui/app.py` is a pure-stdlib Python HTTP server (no framework, no build
step) serving a single-page vanilla HTML/CSS/JS UI (`case_ui/index.html`).

- **Goals:** the page opens by asking what the lawyer wants to focus on. Goals
  become clickable pills that filter the matrix to matching propositions and show
  a coverage strip ("4 match: 2 supported, 1 undermined, 1 missing"). Matching is
  keyword-based (no LLM call). Goals auto-save (debounced) to
  `case_ui/data/goals.json`.
- **Trial readiness:** a single header score. `supported` counts 1.0,
  `contested` 0.5, `undermined` and `missing` 0.0, averaged over all
  propositions.
- **Matrix views:** Full Matrix and Risk & Gaps tabs, status filters, free-text
  search, and expandable evidence drawers showing verbatim quotes and citations.

**Data source.** The app prefers the live pipeline output (`out/matrix.json`)
and falls back to the committed snapshot (`case_ui/data/matrix.json`) when `out/`
is absent, which is why it runs out of the box and on a deploy host where `out/`
(gitignored) doesn't exist. Refresh the snapshot with `case_ui/snapshot_data.py`.

**API endpoints**

| Method | Path | Description |
|---|---|---|
| `GET`  | `/api/matrix`  | Full matrix + summary stats |
| `GET`  | `/api/summary` | AI case summary (cached; `?refresh=1` to regenerate) |
| `GET`  | `/api/status`  | Health check (data present, goals exist) |
| `GET`  | `/api/goals`   | Saved goals (`{}` if none) |
| `POST` | `/api/goals`   | Save goals; body `{"goals": [{id, text}, ...]}` |
| `POST` | `/api/chat`    | Chat with Second Chair; body `{"messages": [...]}` |

### AI case summary

`GET /api/summary` produces a plain-English narrative of the dispute:

1. Pick legal-concept search terms that actually appear in the pleadings.
2. Query the **EU Publications Office Cellar** SPARQL endpoint (public, no auth)
   for relevant English-language legal materials.
3. Call Claude (`claude-sonnet-4-6`, adaptive thinking) with the pleaded
   propositions, the proof-matrix figures, and the retrieved EU materials, plus
   the **`web_search` tool** to identify and ground the real-world dispute.
4. Parse the model's `HEADLINE:` and paragraph format and return it with the
   sources used.

The narrative is **cached to disk** (`case_ui/data/summary.json`), keyed by a
SHA-256 signature of the matrix content, so the LLM is only re-called when the
case changes; the HTTP response adds an ETag and `Cache-Control` for browser
caching. If the LLM is unavailable, a deterministic matrix-derived summary is
returned instead.

### "Second Chair" chat

`POST /api/chat` answers questions as an embedded litigation associate. The
system prompt memorises the pleaded propositions and their matrix status, and
the model is told to cite propositions inline as `[P0003]` (the UI turns these
into chips). Without an API key it falls back to a keyword-matched, grounded
reply over the pleadings so it still says something true about the case.

### Deployment (Render)

`render.yaml` defines a free Render web service:

- Build `pip install -r requirements.txt`, start `python case_ui/app.py`.
- The server reads `$PORT` (Render-injected) and binds all interfaces.
- `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY` are dashboard secrets (`sync: false`),
  never committed.
- Health check at `/api/status`.

Because `out/` is gitignored and absent on the host, the app serves the
committed `case_ui/data/matrix.json` snapshot. Note: the free tier sleeps when
idle and has an ephemeral disk, so goal and summary writes don't survive a
restart.

---

## Notes and caveats

- **Pleadings input:** if `bundle_md/` contains no pleadings (e.g. only witness
  statements), `extract.py` reports *no pleadings detected* and propositions stay
  empty until the Claim Form / Particulars of Claim / Defence are present (or
  designated with `--pleadings "<glob>"`). Evidence extraction runs regardless.
  The CMS bundle includes `01_Claim_Form.docx` and `02_Particulars_of_Claim.docx`.
- **Goals are single-session, single-user** (hackathon scope). Delete
  `case_ui/data/goals.json` to reset.
- **Port:** defaults to `8001` locally; override with `$PORT`.

---

## Challenges

**We jumped the gun on the data.** Before the challenge was fully released we
started analysing a preliminary bundle to plan our approach, a large set of PDF
witness statements. We built our first ingestion path around it: a PDF-to-JSON
converter (`pdf_to_ai_safe.py`, with OCR fallback) feeding the extraction and
proof-matrix pipeline. That early analysis shaped the whole design (atomic
propositions, provenance-checked quotes, the four-bucket matrix).

**Then the real bundle landed in a different format.** When the challenge was
fully released, the official data came as a new bundle of `.docx` files,
different documents and a different format from the PDFs we'd planned against. So
we had to pivot the ingestion layer: a new preprocessing step, `convert_docx.py`,
reads the `.docx` bundle into `bundle_md/`, which the pipeline now consumes — and
we kept the extraction, matrix, and UI work we'd already built on top.

As a result, the original PDF path is deprecated. The PDF-to-JSON `.jsonl`
outputs now live in `out2_deprecated_pdf_jsonl/` (renamed to flag that they came
from the superseded PDF bundle, not the released `.docx` data). The
`pdf_to_ai_safe.py` and `convert_pdfs.py` scripts are kept for reference but are
no longer in the active path.
