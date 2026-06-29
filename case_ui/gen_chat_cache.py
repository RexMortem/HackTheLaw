"""
Pre-generate Second Chair answers for the default starter questions.

The chat endpoint serves these from case_ui/data/chat_cache/ (with a simulated
"thinking" delay) so the starter chips are instant, reliable, and cost no tokens
at demo time. Re-run whenever the matrix or the default questions change:

    export ANTHROPIC_API_KEY=sk-ant-...
    python case_ui/gen_chat_cache.py

Answers are keyed by (matrix signature, question), so they're only used while the
case is unchanged; otherwise the endpoint falls back to a live LLM call.
"""

import pathlib
import sys

# Make `import app` (this dir) and `import caselib` (repo root) both work.
_HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import app  # noqa: E402  (imports define helpers; does not start the server)


def main() -> None:
    matrix, err = app._load_matrix()
    if err:
        sys.exit(f"ERROR: {err}")
    sig = app._matrix_signature(matrix)
    print(f"Matrix signature {sig} — {len(matrix)} propositions")
    print(f"Caching {len(app.DEFAULT_QUESTIONS)} default questions -> "
          f"{app.CHAT_CACHE_DIR}\n")

    ok = 0
    for q in app.DEFAULT_QUESTIONS:
        print(f"  generating: {q!r} ...", flush=True)
        reply, source, navigate = app._chat_reply(
            [{"role": "user", "content": q}], matrix)
        if source != "claude" or not reply:
            print(f"    SKIP (source={source}) — not cached")
            continue
        app._chat_cache_put(sig, q, {
            "question": q, "reply": reply,
            "navigate": navigate, "signature": sig, "source": "cached",
        })
        print(f"    cached ({len(reply)} chars, navigate={navigate})")
        ok += 1
    print(f"\nDone: {ok}/{len(app.DEFAULT_QUESTIONS)} cached into {app.CHAT_CACHE_DIR}")


if __name__ == "__main__":
    main()
