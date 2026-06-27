# `viz/` — 3b1b-style demo animations

Manim Community Edition animations for the project demo video. Every scene
builds itself from the project's **real** data (`case_ui/data/matrix.json` and
`graph.json`), so the film shows the actual case and re-renders when the
pipeline output changes.

## Setup (one-off)

Keep this in its own virtualenv — Manim's toolchain is heavy and must never
leak into the app's `requirements.txt`.

```powershell
python -m venv .venv-viz
.venv-viz\Scripts\Activate.ps1
pip install -r viz/requirements-viz.txt
```

Notes:
- **gTTS voice needs internet** (no API key). For broadcast-quality narration,
  `pip install "manim-voiceover[azure]"`, set `AZURE_SUBSCRIPTION_KEY` /
  `AZURE_SERVICE_REGION`, and swap the service in `NarratedScene.setup`
  (`scenes.py`) for `AzureService(voice="en-GB-RyanNeural")`.
- **No LaTeX required** for these scenes (all text uses `Text()`). Install
  MiKTeX only if you later add typeset math via `MathTex()`.
- Manim also needs **ffmpeg** on PATH (bundled with the pip install on Windows;
  otherwise `winget install ffmpeg`).

## Render

Run from the **repo root** (scenes `import data_loader` from this folder).

```powershell
# Fast preview while iterating on one beat:
manim -pql viz/scenes.py OneProposition

# High quality, all five beats (each renders to its own .mp4 clip):
manim -pqh viz/scenes.py TheProblem OneProposition TheMatrix TheGraph SinglePointOfFailure

# 4K final pass:
manim -qk viz/scenes.py TheProblem OneProposition TheMatrix TheGraph SinglePointOfFailure
```

Flags: `-p` play when done · `-q l/h/k` quality (low/high/4k) · `-a` render all
scenes in the file. Output lands in `media/videos/scenes/<res>/`.

## The five beats (the storyboard)

| Scene | Beat | Drawn from |
|---|---|---|
| `TheProblem` | The unread bundle; "which parts are actually proven?" | counts in `matrix.json` / `graph.json` |
| `OneProposition` | One allegation → evidence lights up green/red → **contested** | `proposition_links("P0001")` |
| `TheMatrix` | The full grid fills by status; trial-readiness counts up to 59% | `matrix.json` statuses + `trial_readiness()` |
| `TheGraph` | The case blooms into a force-directed graph | `signal_edges()` + `spring_layout()` |
| `SinglePointOfFailure` | Remove the top articulation node → the cluster shatters | `best_articulation_proposition()` + `ego_subgraph()` |

Stitch the five clips (plus narration already baked in by manim-voiceover) in
any editor, or concatenate with ffmpeg.

## Files

- `data_loader.py` — reads the JSON, computes the score, builds ego subgraphs,
  and runs a tiny numpy spring-layout solver (no extra dependency).
- `theme.py` — the palette; status colours mirror the web app so video and UI
  read as one product.
- `scenes.py` — the five `Scene` classes. `NarratedScene` is the shared base
  (dark backdrop + voice).

## Tuning knobs

- `OneProposition.PID` — animate a different allegation.
- `TheGraph.MAX_EDGES` — cap on drawn edges (default 300 of ~580 signal edges,
  strongest by confidence; the cap is printed at render time, never silent).
- `SinglePointOfFailure` uses the highest-PageRank articulation proposition
  automatically; change `ego_subgraph(..., cap=)` for a denser/sparser cluster.
