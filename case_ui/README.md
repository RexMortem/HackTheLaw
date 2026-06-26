# Proof Matrix — Case UI

Single-page web UI for the LLM x Law pleading-to-proof pipeline. No build
step, no npm, no third-party dependencies — pure stdlib Python server +
vanilla HTML/CSS/JS.

## Prerequisites

- Python 3.9+ (uses `list | None` style annotations)
- The pipeline output file `out/matrix.json` (see [Populating the data](#populating-the-data))

## Run

```powershell
# From the repo root:
python case_ui/app.py
```

Then open: **http://localhost:8001**

## Flow

The page combines goal entry and analysis on a single scrolling surface.

### 1. State your focus

The page opens with a single large prompt:

> **What do you want to focus on?**

The user can:

- Click any of the one-click **suggestion chips** for common litigation-review focuses.
- Type a custom focus in the text area and press **Enter** (or click **Add**)
  to add it as a chip.
- Remove any chip with the &times; button.

As soon as the user starts typing — or adds the first chip — the tagline
**minimises** to free up vertical space, and the analysis section reveals
itself below with a smooth slide-in. The page auto-scrolls to the analysis.

Goals are **auto-saved** to `case_ui/data/goals.json` on every change
(debounced 400 ms) — there is no explicit "Continue" button.

### 2. Analysis

The matrix UI is reframed as the answer to the stated focuses.

- The matrix is **preloaded in the background** as soon as the page mounts,
  so the analysis is ready by the time the user finishes typing.
- **Goals banner** — each focus appears as a clickable pill above the matrix
  showing the count of propositions that match it.
- **Clicking a pill** filters the Full Matrix and Risk & Gaps tabs to
  matching propositions only, and shows a Goal coverage summary strip
  (e.g. "4 propositions match — 2 supported, 1 undermined, 1 missing").
  Clicking the active pill again deselects it.
- Standard features: status counts in the header, trial readiness %,
  Full Matrix / Risk & Gaps tabs, status filter buttons, free-text search,
  and expandable evidence drawers with verbatim quotes + citations.

If `case_ui/data/goals.json` already exists when the page loads, the goals
are prefilled, the tagline starts minimised, and the analysis is already
revealed.

## Goals JSON file

Goals are persisted at:

```
case_ui/data/goals.json
```

Schema:

```json
{
  "saved_at": "2026-06-26T10:00:00+00:00",
  "goals": [
    {"id": "g1", "text": "Identify allegations with no supporting witness evidence"},
    {"id": "g2", "text": "Surface contradictions between Defence and the witness statements"}
  ]
}
```

The file is overwritten on each save. It is single-session, single-user
(hackathon scope — no per-user storage).

To reset, delete the file:

```powershell
Remove-Item case_ui\data\goals.json
```

## How goals influence the analysis

Today goal filtering is **keyword-based**: each goal's text is split into
words longer than 3 characters and any proposition whose text, type, party,
witness names, rationale, or quotes contain at least one of those words is
considered a match. Fast, no LLM call, works well for concrete legal terms
("witness", "hearsay", "breach", "contract").

A natural next step is to pass the stated goals to `build_matrix.py` as part
of the matrix-generation prompt so the LLM can score relevance at generation
time. The goals schema is already designed for that.

## Populating the data

Run the pipeline first:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python extract.py          # -> out/propositions.json, out/evidence.json
python build_matrix.py     # -> out/matrix.json, out/matrix.csv, out/matrix.md
```

If `out/matrix.json` is missing, the analysis area shows a clear empty
state with the commands above.

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/matrix` | Full matrix with summary stats |
| `GET` | `/api/status` | Health check (matrix file present, goals exist) |
| `GET` | `/api/goals` | Return saved goals (empty `{}` if none) |
| `POST` | `/api/goals` | Save goals; body: `{"goals": [{id, text}, ...]}` |

## Port

The server runs on **8001**. Change `PORT` in `case_ui/app.py` if needed.
