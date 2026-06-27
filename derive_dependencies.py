"""
derive_dependencies — map the logical structure of the pleaded case.

The proof matrix tells us which evidence bears on each proposition, but not how
the propositions depend on *each other*. A claim of loss presupposes causation,
which presupposes breach, which presupposes a duty: knock out a premise and
everything built on it falls. This step asks Claude to extract those directed
logical dependencies (A DEPENDS_ON B when B must hold for A to succeed).

Two payoffs:
  * the Case Graph gains DEPENDS_ON edges between propositions, so you can see
    (and, in Neo4j, query) the inference structure, not just the evidence map;
  * running strongly-connected-components over DEPENDS_ON detects **circular
    reasoning** — a cycle means a proposition is, transitively, used to prove
    itself. In a sound case theory the dependency graph is acyclic; a cycle is a
    real defect worth flagging. (graph_export.py computes the SCC; this script
    just produces the edges.)

Output: out/dependencies.json (+ a snapshot at case_ui/data/dependencies.json so
the deployed app has it). Pure reuse of caselib's Claude helpers.

Run:
    python derive_dependencies.py            # out/matrix.json -> dependencies
    python derive_dependencies.py --no-llm   # heuristic fallback (responds_to only)
"""
from __future__ import annotations

import json
import pathlib

import caselib

_HERE = pathlib.Path(__file__).parent

DEPENDENCIES_SCHEMA = {
    "type": "object",
    "properties": {
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["from", "to", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["dependencies"],
    "additionalProperties": False,
}

DEPENDENCIES_SYSTEM = """\
You are a litigation analyst mapping the LOGICAL STRUCTURE of a pleaded case in \
an English commercial dispute. You are given the pleaded propositions \
(allegations and denials), each with an id.

Identify directed LOGICAL DEPENDENCIES between propositions. Proposition A \
DEPENDS ON proposition B when B is a premise or prerequisite that must be \
established for A to succeed — i.e. if B fails, A cannot stand.

Typical liability chain: a duty is the premise of a breach; the breach is the \
premise of causation; causation is the premise of loss/damage. So "loss" \
depends on "causation" depends on "breach" depends on "duty".

Rules:
- from = the dependent proposition's id; to = the premise's id.
- Only link propositions advanced within the SAME party's theory of the case.
- Capture genuine logical premises, not mere topical similarity.
- The result should normally be ACYCLIC. Do NOT invent a dependency just to \
connect things; omit a pair if there is no real premise relationship.
- Use only the ids provided. Return JSON only."""


def _digest(matrix: list) -> str:
    return "\n".join(
        f"[{(p := row['proposition']).get('id')}] "
        f"({p.get('type')}, {p.get('party')}) {p.get('text')}"
        for row in matrix
    )


def derive_llm(matrix: list) -> list[dict]:
    client = caselib.get_client()
    user = ("PLEADED PROPOSITIONS:\n" + _digest(matrix) +
            "\n\nReturn the logical dependency edges as JSON.")
    # Fast structured-output path (no extended thinking) so the model spends its
    # budget emitting edges, not on a long private chain of thought.
    out = caselib.run_json(client, DEPENDENCIES_SYSTEM, user,
                           DEPENDENCIES_SCHEMA, max_tokens=6000)
    return out.get("dependencies", [])


def derive_heuristic(matrix: list) -> list[dict]:
    """No-LLM fallback: a denial 'depends on' the allegation it responds to only
    in the weak sense of answering it. We don't have true premises without the
    model, so we return nothing rather than fabricate structure."""
    return []


def _valid(deps: list[dict], ids: set[str]) -> list[dict]:
    clean, seen = [], set()
    for d in deps:
        a, b = d.get("from"), d.get("to")
        if a in ids and b in ids and a != b and (a, b) not in seen:
            seen.add((a, b))
            clean.append({"from": a, "to": b, "rationale": d.get("rationale", "")})
    return clean


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Derive proposition dependencies.")
    ap.add_argument("--matrix", help="path to matrix.json (default: out/ then snapshot)")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM, use heuristic")
    ap.add_argument("--out", default=str(_HERE / "out" / "dependencies.json"))
    args = ap.parse_args()

    import graph_export
    matrix = graph_export.load_matrix(args.matrix)
    ids = {row["proposition"]["id"] for row in matrix if row.get("proposition")}

    if args.no_llm:
        deps = derive_heuristic(matrix)
    else:
        try:
            deps = derive_llm(matrix)
        except Exception as exc:
            print(f"  LLM dependency derivation failed ({type(exc).__name__}: {exc}); "
                  "falling back to heuristic.")
            deps = derive_heuristic(matrix)

    deps = _valid(deps, ids)
    record = {"dependencies": deps}

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    snap = _HERE / "case_ui" / "data" / "dependencies.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    print(f"Derived {len(deps)} dependency edges across {len(ids)} propositions.")
    print(f"Wrote {out_path} and snapshot {snap}.")


if __name__ == "__main__":
    main()
