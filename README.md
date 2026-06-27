# LLM × Law — Pleading-to-Proof

Our entry for the **CMS — Pleading to Proof** challenge. We built a tool that
takes a litigation bundle (PDFs) and stress-tests the case theory: it extracts
every pleaded allegation and denial, maps each one to the available evidence,
and labels it **supported**, **contested**, **undermined**, or **missing** — a
*pleading-to-proof matrix* with a trial-readiness score, an AI case summary, and
a built-in litigation assistant you can chat with.

---

## Run it

### Quick start — the web app

```bash
pip install -r requirements.txt
python case_ui/app.py
```

Then open **http://localhost:8001**.

It works out of the box: a sample proof matrix is bundled with the repo
(`case_ui/data/matrix.json`), so the matrix, filters, goals, and risk view all
run with no API key and no pipeline run.

To enable the **AI case summary** and the **Second Chair chat assistant**, set
an Anthropic API key first (these features degrade gracefully without one):

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY="..."
```

### Regenerate the matrix from source PDFs (optional)

The bundled matrix is pre-computed. To rebuild it from a raw bundle:

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python convert_pdfs.py     # PDFs        -> converted/*.md
python extract.py          # converted/  -> out/propositions.json, out/evidence.json
python build_matrix.py     # the above   -> out/matrix.{json,csv,md}
python case_ui/snapshot_data.py   # copy out/matrix.json -> case_ui/data/ for the app
```

Cheap dry run first: `python extract.py --mode sync --limit 3`.

---

## What we built

In complex litigation, the pleadings set out dozens of allegations and denials,
but the evidence that proves (or disproves) them is scattered across witness
statements, exhibits, correspondence and contracts. Working out which parts of
the case are actually evidenced is slow, manual cross-referencing — and teams
often discover a weak point far too late.

Our tool does that cross-referencing automatically. It reads the bundle, pulls
out each atomic pleaded proposition, finds the evidence that bears on it, and
decides whether that evidence **supports**, **contests**, **undermines**, or
**fails to address** the proposition. The result is a single matrix that shows,
at a glance, where the case is strong, where it is vulnerable, and where there
is simply no evidence at all.

On top of that matrix, the web app lets a lawyer **state what they care about**
(e.g. "contradictions in the Defence", "allegations with no witness support")
and instantly filters the case to those points. A header **trial-readiness
score** rolls the whole bundle into one number. An **AI case summary** explains,
in plain English, what the dispute is actually about — grounded by a live web
search and by relevant EU legal materials — and folds in the proof figures. And
**"Second Chair"**, an embedded AI associate, answers free-text questions about
the case, citing the exact propositions it relies on.

Every cell is **auditable**: each link back to the evidence carries the source
document, page, and paragraph, plus a verbatim quote that is checked against the
original text — so nothing in the matrix is a hallucination.

---

## How it works (overview)

The system is two halves:

1. **An offline pipeline** that turns PDFs into the proof matrix — three scripts
   run in sequence (`convert_pdfs.py` → `extract.py` → `build_matrix.py`).
2. **A web app** (`case_ui/`) that serves the matrix as an interactive UI and
   adds the goals, AI summary, and chat features on top.

```
PDFs ─convert_pdfs.py─▶ converted/*.md ─extract.py─▶ out/propositions.json
                                          │                out/evidence.json
                                          └─build_matrix.py─▶ out/matrix.{json,csv,md}
                                                                      │
                                                          case_ui/app.py ──▶ browser UI
```

Key design choices, in brief:

- **Two layers, one mapping.** Allegations/denials come from the *pleadings*;
  witness statements and exhibits are *evidence*. The product is the mapping
  between them.
- **Atomic + provenance.** One claim per record, each tied to a source paragraph
  and a quote verified against the text — so every matrix cell is checkable.
- **Retrieval + classification.** For each proposition we retrieve candidate
  evidence (BM25 by default, with legal-tuned embeddings optional) and have
  Claude classify each candidate as supportive / adverse / neutral.
- **Grounded AI, graceful fallback.** The summary and chat are grounded in the
  matrix (and external sources for the summary); if no API key is present they
  fall back to deterministic, matrix-derived answers rather than failing.

---

## Technical detail

### The pipeline

```
PDFs ─convert_pdfs.py─▶ converted/*.md ─extract.py─▶ out/propositions.json
                                          │                out/evidence.json
                                          └─build_matrix.py─▶ out/matrix.{json,csv,md}
```

1. **`convert_pdfs.py`** — PDF → AI-readable Markdown (frontmatter + page markers
   + preserved paragraph numbering). Already run; output in `converted/`.
2. **`extract.py`** — classifies each doc (pleading / witness statement /
   exhibit), extracts atomic propositions from pleadings and evidence units from
   the rest. Every record carries provenance (doc_id, page, paragraph) and a
   **verbatim quote verified against the source** (anti-hallucination gate).
3. **`build_matrix.py`** — BM25-retrieves candidate evidence per proposition, has
   Claude classify each candidate, and rolls up the four-bucket matrix.

**Why this design**

- **Structured outputs** (`output_config.format`) for schema-valid records.
- **Batch API** (50% cheaper) for per-document extraction; **prompt caching** on
  the shared instruction prefix.
- **Model:** `claude-opus-4-8`, adaptive thinking, `effort: high`.

**Retrieval & scoring knobs**

- **Top-k:** `build_matrix.py --top-k N` controls how many candidate evidence
  units are considered per proposition (default 25). Higher = better adverse-
  evidence recall at higher cost. The count is logged — no silent truncation.
- **Retriever:** `--retriever bm25` (default, no extra deps) | `embeddings` |
  `hybrid`. Embeddings/hybrid use **Voyage AI** (`voyage-law-2`, legal-domain
  tuned) and need `VOYAGE_API_KEY`; vectors cache in `out/emb_*.npz`. **Hybrid**
  fuses BM25 + embeddings via reciprocal-rank fusion — best for catching adverse
  evidence worded differently from the allegation:
  ```bash
  export VOYAGE_API_KEY=pa-...
  python build_matrix.py --retriever hybrid --top-k 40
  ```
- **Confidence:** `--min-confidence` (default 0.5) gates which links count toward
  a proposition's status and appear in the report.

### The web app

`case_ui/app.py` is a pure-stdlib Python HTTP server (no framework, no build
step) serving a single-page vanilla HTML/CSS/JS UI (`case_ui/index.html`).

- **Goals.** The page opens by asking what the lawyer wants to focus on. Goals
  become clickable pills that filter the matrix to matching propositions and show
  a coverage strip ("4 match — 2 supported, 1 undermined, 1 missing"). Matching
  is keyword-based (no LLM call). Goals auto-save (debounced) to
  `case_ui/data/goals.json`.
- **Trial readiness.** A single header score: `supported` counts 1.0,
  `contested` 0.5, `undermined`/`missing` 0.0, averaged over all propositions.
- **Matrix views.** Full Matrix and Risk & Gaps tabs, status filters, free-text
  search, and expandable evidence drawers showing verbatim quotes + citations.

**Data source.** The app prefers the live pipeline output (`out/matrix.json`)
and falls back to the committed snapshot (`case_ui/data/matrix.json`) when `out/`
is absent — which is why it runs out of the box and on a deploy host where `out/`
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
3. Call Claude (`claude-opus-4-8`, adaptive thinking) with the pleaded
   propositions, the proof-matrix figures, and the retrieved EU materials, plus
   the **`web_search` tool** to identify and ground the real-world dispute.
4. Parse the model's `HEADLINE:` + paragraph format and return it with the
   sources used.

The narrative is **cached to disk** (`case_ui/data/summary.json`), keyed by a
SHA-256 signature of the matrix content, so the LLM is only re-called when the
case changes; the HTTP response adds an ETag + `Cache-Control` for browser
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
- `ANTHROPIC_API_KEY` / `VOYAGE_API_KEY` are dashboard secrets (`sync: false`),
  never committed.
- Health check at `/api/status`.

Because `out/` is gitignored and absent on the host, the app serves the
committed `case_ui/data/matrix.json` snapshot. Note: the free tier sleeps when
idle and has an ephemeral disk, so goal/summary writes don't survive a restart.

---

## Notes & caveats

- **Pleadings input.** If the `converted/` set contains only witness statements,
  `extract.py` reports *no pleadings detected* and propositions stay empty until
  the Particulars of Claim / Defence are added (or designated with
  `--pleadings "<glob>"`). Evidence extraction runs regardless.
- **Goals are single-session, single-user** (hackathon scope). Delete
  `case_ui/data/goals.json` to reset.
- **Two corrupt source PDFs** (`witn04520100_2.pdf`, `witn04600300.pdf`) are
  0-byte and were skipped during conversion.
- **Port.** Defaults to `8001` locally; override with `$PORT`.
