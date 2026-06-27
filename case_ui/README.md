# Proof Matrix: Case UI

The web front end for the pleading-to-proof pipeline: a single-page UI served by
a pure-stdlib Python server (`app.py`), with no build step, no npm, and no
third-party runtime deps.

> For setup, run instructions, the API endpoint table, and the AI summary and
> chat internals, see the [root README](../README.md). This file covers the UI
> flow and the goals file in detail.

## Run

```bash
python case_ui/app.py      # from the repo root
```

Then open **http://localhost:8001**. The matrix renders from the live pipeline
output (`out/matrix.json`) if present, else the committed snapshot
(`case_ui/data/matrix.json`), so it works out of the box.

## Flow

Goal entry and analysis live on one scrolling surface.

### 1. State your focus

The page opens with one prompt: **"What do you want to focus on?"** The user can:

- Click a one-click **suggestion chip** for a common review focus,
- Type a custom focus and press **Enter** or click **Add**, or
- Remove any chip with the &times; button.

On the first keystroke or chip, the tagline minimises to free up space and the
analysis section slides in below. Goals auto-save (debounced 400 ms) to
`case_ui/data/goals.json`, with no "Continue" button. If that file already exists
on load, goals are prefilled and the analysis starts revealed.

### 2. Analysis

The matrix is reframed as the answer to the stated focuses, and is preloaded in
the background as the page mounts so it's ready immediately.

- **Goals banner:** each focus is a clickable pill showing how many propositions
  match it.
- **Click a pill** to filter the Full Matrix and Risk & Gaps tabs to matching
  propositions and show a coverage strip (e.g. "4 match: 2 supported, 1
  undermined, 1 missing"). Click the active pill again to deselect.
- **AI case summary** and the **Second Chair** chat assistant sit alongside the
  matrix (see the root README for how these are generated).
- Standard features: header status counts, trial-readiness %, status filters,
  free-text search, and expandable evidence drawers with verbatim quotes and
  citations.

## Goals file

Persisted at `case_ui/data/goals.json`, overwritten on each save. Single-session,
single-user (hackathon scope, no per-user storage).

```json
{
  "saved_at": "2026-06-26T10:00:00+00:00",
  "goals": [
    {"id": "g1", "text": "Identify allegations with no supporting witness evidence"},
    {"id": "g2", "text": "Surface contradictions between Defence and the witness statements"}
  ]
}
```

Goal filtering is keyword-based: each goal's text is split into words longer than
3 characters, and any proposition whose text, type, party, witness names,
rationale, or quotes contain one of them is a match. It is fast, needs no LLM
call, and is effective for concrete legal terms ("witness", "hearsay", "breach",
"contract"). A natural next step is to pass the goals into `build_matrix.py` so
the LLM scores relevance at generation time; the schema is already designed for
it.

To reset: delete `case_ui/data/goals.json`.
