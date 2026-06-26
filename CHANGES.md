# Changes

## 2026-06-26 — Single-surface flow; demo mode removed

- **Removed demo mode:** dropped the `CASE_UI_DEMO` env var, deleted `out/matrix.sample.json`, and removed the yellow demo banner. The UI now reads `out/matrix.json` only.
- **Shortened brand:** header title is now "Proof Matrix" (two words). Dropped the dynamic case-name subtitle.
- **New hero tagline:** the goals prompt is now "What do you want to focus on?" rendered as a large hero. It auto-minimises as soon as the user types or adds a goal, freeing space for the analysis below.
- **Removed "Continue to Analysis" button:** goals are now auto-saved (debounced) on every add/remove. Adding the first goal smoothly reveals and scrolls to the analysis section.
- **Single scrolling surface:** Step 1 and Step 2 now live on the same page. Step 2 is hidden until the first goal is added, then slides into view (opacity + transform transition). On reload with existing goals, both sections are visible immediately.
- **Background matrix preload:** `loadMatrix()` is fired on page mount, so the analysis is populated by the time the user finishes typing their first goal.
- **Removed the header "Edit goals" link:** no longer needed — the goals input is always on the same page above the analysis.
- **Docs updated:** `case_ui/README.md` rewritten to describe the single-surface flow and the demo-mode removal.

## 2026-06-26 — Two-step goals flow

- **Added Step 1 (goals entry):** new landing screen where the user enters one or more litigation-review goals via free-text input or one-click suggestion chips before reaching the matrix.
- **Added goals persistence:** `POST /api/goals` saves goals to `case_ui/data/goals.json`; `GET /api/goals` retrieves them. Server holds an in-memory cache. Directory is created automatically on first write.
- **Added goals banner in Step 2:** clickable goal pills above the matrix show per-goal proposition counts and filter the Full Matrix and Risk & Gaps tabs on click; a coverage summary strip (e.g. "4 propositions match — 2 supported, 1 missing") appears when a goal is active.
- **Smart routing:** if `case_ui/data/goals.json` exists at page load, the UI skips Step 1 and goes directly to the matrix with goals prefilled; an "Edit goals" link in the header returns to Step 1.
- **Updated `case_ui/app.py`:** added `GET /api/goals`, `POST /api/goals`, in-memory `_GOALS` cache, `do_POST` handler, and `data/` directory creation — no new dependencies.
- **Updated docs:** `case_ui/README.md` now documents the two-step flow, goals JSON schema, keyword-matching approach, and a note on the future LLM-integration path.
