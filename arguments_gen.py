"""
arguments_gen — turn the proof matrix into linked legal arguments.

Claude reads the pleaded propositions and their evidential status and drafts the
arguments each side can actually run, with every argument explicitly LINKED to:
  * the propositions it relies on (by id, e.g. P0003), and
  * the evidence that carries it (by id, e.g. E08337),
so nothing is asserted that the matrix can't back. Each argument also names its
own weakest link, which the stress-test suite later attacks.

Perplexity then adds real, citable supporting AUTHORITY (cases / principles) for
each argument via live web search — turning "here's the argument" into "here's
the argument and the law behind it."

Used by the web app (/api/arguments, cached) and runnable standalone:
    python arguments_gen.py                 # -> out/arguments.json (+ snapshot)
    python arguments_gen.py --no-authority  # skip the Perplexity step
"""
from __future__ import annotations

import json
import pathlib

import caselib

_HERE = pathlib.Path(__file__).parent

ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "arguments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "party": {"type": "string"},
                    "issue": {"type": "string"},
                    "thesis": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "relies_on": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "strength": {"type": "string",
                                 "enum": ["strong", "moderate", "weak"]},
                    "vulnerability": {"type": "string"},
                },
                "required": ["id", "party", "issue", "thesis", "reasoning",
                             "relies_on", "evidence", "strength", "vulnerability"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["arguments"],
    "additionalProperties": False,
}

ARGUMENTS_SYSTEM = """\
You are lead counsel preparing the case theory for an English commercial dispute. \
You are given the pleaded propositions (allegations and denials), each tagged \
with its evidential status from a proof matrix (supported / contested / \
undermined / MISSING), the witnesses, and evidence ids.

Draft the concrete legal ARGUMENTS each side can run. For each argument:
- party: who runs it ("Claimant" or "Defendant").
- issue: a short label for the legal issue (e.g. "Reliability of the IT system").
- thesis: one sentence — the proposition you are asking the court to accept.
- reasoning: 2-4 sentences of how the argument runs.
- relies_on: the ids of the pleaded propositions it depends on (e.g. ["P0003"]). \
Only use ids present in the input.
- evidence: the ids of the evidence items it leans on (e.g. ["E08337"]). Only \
use ids present in the input; [] if it rests on pleading alone.
- strength: your honest assessment given the evidential status of what it relies \
on (an argument resting on a MISSING/undermined proposition is "weak").
- vulnerability: the single weakest link an opponent will attack.

Cover the main contested issues for BOTH sides. Be realistic and grounded in the \
matrix — do not invent evidence. Return JSON only."""


def _digest(matrix: list) -> str:
    lines = []
    for row in matrix:
        p = row["proposition"]
        ev = ", ".join(
            f"{lk['evidence_id']}({lk['relation']},{lk.get('witness', '') or '?'})"
            for lk in (row.get("links") or [])
            if lk.get("relation") in ("supportive", "adverse")
        )
        lines.append(
            f"[{p.get('id')}] ({p.get('type')}, {p.get('party')}, "
            f"status={row.get('status')}) {p.get('text')}"
            + (f"  EVIDENCE: {ev}" if ev else "  EVIDENCE: (none)")
        )
    return "\n".join(lines)


def _add_authority(args: list[dict], limit: int = 8) -> None:
    """Attach real supporting legal authority to each argument via Perplexity.
    Best-effort and capped; mutates the argument dicts in place."""
    if not caselib.perplexity_available():
        return
    system = ("You are a UK legal research assistant. For the argument below, find "
              "REAL, citable supporting authority from English law (decided cases, "
              "statutes, or established principles). Give one or two sentences and "
              "cite sources. If you find nothing on point, say so briefly.")
    for a in args[:limit]:
        q = (f"Issue: {a.get('issue')}\nArgument: {a.get('thesis')} "
             f"({a.get('party')} in an English commercial dispute).\n"
             "What supporting legal authority is there?")
        text, cites = caselib.perplexity_chat(system, q, max_tokens=400)
        if text:
            a["authority_note"] = text
            a["authority"] = cites[:3]


def _add_eu_authority(args: list[dict]) -> None:
    """Attach persuasive EU authority (CJEU decisions / directives) to each
    argument from the EU Publications Office Cellar, by the legal concepts the
    argument raises. Best-effort; mutates in place. Cached per concept-set so we
    don't re-query Cellar for the same terms."""
    cache: dict[tuple, list] = {}
    for a in args:
        terms = caselib.concepts_in(
            f"{a.get('issue', '')} {a.get('thesis', '')} {a.get('reasoning', '')}")
        if not terms:
            continue
        key = tuple(sorted(terms))
        if key not in cache:
            cache[key] = caselib.cellar_search(list(key), limit=3)
        if cache[key]:
            a["eu_authority"] = cache[key]


def generate_arguments(matrix: list, with_authority: bool = True) -> dict:
    """Generate linked arguments. Returns {arguments, generated_by}. Never raises;
    on LLM failure returns an empty, well-formed payload so callers degrade."""
    if not matrix:
        return {"arguments": [], "generated_by": "empty"}
    try:
        client = caselib.get_client()
        user = ("PLEADED PROPOSITIONS WITH EVIDENCE:\n" + _digest(matrix) +
                "\n\nDraft the arguments for both sides as JSON.")
        out = caselib.run_json(client, ARGUMENTS_SYSTEM, user,
                               ARGUMENTS_SCHEMA, max_tokens=10000)
        args = out.get("arguments", [])
    except Exception as exc:
        print(f"  [arguments] generation failed: {type(exc).__name__}: {exc}")
        return {"arguments": [], "generated_by": "unavailable"}

    # keep only ids that exist, so the UI never renders a dead citation
    valid_p = {row["proposition"]["id"] for row in matrix}
    valid_e = {lk["evidence_id"] for row in matrix for lk in (row.get("links") or [])}
    for a in args:
        a["relies_on"] = [x for x in a.get("relies_on", []) if x in valid_p]
        a["evidence"] = [x for x in a.get("evidence", []) if x in valid_e]
        a.setdefault("authority", [])
        a.setdefault("authority_note", "")
        a.setdefault("eu_authority", [])

    if with_authority:
        _add_authority(args)
    _add_eu_authority(args)   # Cellar is public/no-auth — always attempt

    return {"arguments": args, "generated_by": "claude"}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Generate linked legal arguments.")
    ap.add_argument("--matrix", help="path to matrix.json (default: out/ then snapshot)")
    ap.add_argument("--no-authority", action="store_true",
                    help="skip the Perplexity supporting-authority step")
    args_cli = ap.parse_args()

    import graph_export
    matrix = graph_export.load_matrix(args_cli.matrix)
    result = generate_arguments(matrix, with_authority=not args_cli.no_authority)

    out_path = _HERE / "out" / "arguments.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    snap = _HERE / "case_ui" / "data" / "arguments.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    print(f"Generated {len(result['arguments'])} arguments "
          f"({result['generated_by']}). Wrote {out_path} and snapshot {snap}.")


if __name__ == "__main__":
    main()
