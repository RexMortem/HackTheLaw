---
name: project-goals-ui
description: Goals UI feature added to LLMxLaw repo — daily goal planner with JSON persistence and timeline generator
metadata:
  type: project
---

Goals UI is a standalone sub-feature living in `goals_ui/` inside the LLMxLaw repo.

- Stack: Python stdlib `http.server`, single `index.html` with inline CSS + vanilla JS — zero dependencies.
- Server: `goals_ui/app.py`, port 8000.
- Data: `goals_ui/data/goals_YYYY-MM-DD.json` (one file per day, overwrite-on-save).
- Timeline scheduler: priority order (high → medium → low), 09:00–17:00 window, configurable constants at top of `app.py`.
- UI: card layout, indigo accent (#6366f1), priority-coloured left border + mini Gantt bar per block.

**Why:** Hackathon demo feature — user wanted a planner UI that saves structured JSON that a future LLM script can load.

**How to apply:** Any future work on this feature should stay inside `goals_ui/` and must not touch the parent repo's `requirements.txt` or existing Python pipeline files.
