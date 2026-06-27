"""
quantum_gen — preliminary quantum (damages) assessment for the pleaded case.

Litigation quantum is usually argued through several competing valuation
methodologies (market comparables, lost profits / DCF, cost-based / wasted
expenditure, etc.), each producing a different figure with a different
confidence and recovery risk. This module has Claude produce that comparison
*grounded in the proof matrix*, so each methodology is linked to the pleaded
propositions that must succeed for its figure to be recoverable — connecting
quantum to liability.

It powers the Quantum Assessment dashboard (/api/quantum). Output is a
preliminary, AI-estimated comparison for triage — NOT an expert opinion (the UI
and this module say so prominently).

Runnable standalone:
    python quantum_gen.py            # -> out/quantum.json (+ snapshot)
"""
from __future__ import annotations

import json
import pathlib

import caselib

_HERE = pathlib.Path(__file__).parent

QUANTUM_SCHEMA = {
    "type": "object",
    "properties": {
        "case_name": {"type": "string"},
        "case_type": {"type": "string"},
        "currency": {"type": "string"},
        "methods": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "figure": {"type": "number"},
                    "confidence": {"type": "number"},
                    "risk_level": {"type": "string",
                                   "enum": ["conservative", "balanced", "optimistic"]},
                    "recovery_likelihood": {"type": "number"},
                    "summary": {"type": "string"},
                    "factors": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "weaknesses": {"type": "array", "items": {"type": "string"}},
                    "relies_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "figure", "confidence", "risk_level",
                             "recovery_likelihood", "summary", "factors",
                             "strengths", "weaknesses", "relies_on"],
                "additionalProperties": False,
            },
        },
        "recommendation": {
            "type": "object",
            "properties": {
                "preferred_method": {"type": "string"},
                "range_low": {"type": "number"},
                "range_high": {"type": "number"},
                "rationale": {"type": "string"},
            },
            "required": ["preferred_method", "range_low", "range_high", "rationale"],
            "additionalProperties": False,
        },
    },
    "required": ["case_name", "case_type", "currency", "methods", "recommendation"],
    "additionalProperties": False,
}

QUANTUM_SYSTEM = """\
You are a forensic accountant preparing a PRELIMINARY quantum (damages) triage \
for a UK commercial litigation team, from the pleaded case. You are given the \
pleaded propositions (allegations and denials), each tagged with its evidential \
status from a proof matrix (supported / contested / undermined / MISSING).

Produce 3-5 competing valuation METHODOLOGIES a quantum expert might run for this \
dispute — for example market-based / comparable, lost profits or discounted cash \
flow, cost-based / wasted expenditure, reliance loss, or restitutionary. For each:
- name: the methodology (plain English).
- figure: a single headline damages estimate as a NUMBER in the case currency. \
Base it on monetary amounts pleaded or implied by the facts; if none are stated, \
give a reasoned illustrative estimate. These are PRELIMINARY estimates, not a \
calculated opinion.
- confidence: 0.0-1.0 — how reliable the methodology is for this case.
- risk_level: "conservative" (low figure, likely recoverable), "balanced", or \
"optimistic" (high figure, harder to recover).
- recovery_likelihood: 0.0-1.0 — realistic chance of recovering at/near the figure, \
informed by how well the underlying propositions are evidenced.
- summary: one or two plain-English sentences a non-financial solicitor can grasp.
- factors: the key inputs the figure depends on.
- strengths: what makes the methodology defensible.
- weaknesses: the challenges or attack points.
- relies_on: the ids of the pleaded propositions whose success is required for \
this head of loss (use only ids given; a method resting on MISSING/undermined \
propositions should have lower recovery_likelihood).

Then give a recommendation: the preferred_method (by name), a realistic damages \
RANGE (range_low, range_high as numbers), and a short rationale.

Also set case_name (short reference for the dispute), case_type (e.g. "Commercial \
— breach of contract / professional negligence"), and currency (ISO-ish, e.g. \
"GBP"). Be realistic and grounded in the pleaded case. Return JSON only."""


def _digest(matrix: list) -> str:
    return "\n".join(
        f"[{(p := row['proposition']).get('id')}] "
        f"({p.get('type')}, {p.get('party')}, status={row.get('status')}) {p.get('text')}"
        for row in matrix
    )


def generate_quantum(matrix: list) -> dict:
    """Generate the quantum assessment. Never raises; returns a well-formed empty
    payload on failure so callers degrade."""
    if not matrix:
        return {"methods": [], "generated_by": "empty"}
    try:
        client = caselib.get_client()
        user = ("PLEADED PROPOSITIONS:\n" + _digest(matrix) +
                "\n\nProduce the quantum methodology comparison as JSON.")
        out = caselib.run_json(client, QUANTUM_SYSTEM, user,
                               QUANTUM_SCHEMA, max_tokens=9000)
    except Exception as exc:
        print(f"  [quantum] generation failed: {type(exc).__name__}: {exc}")
        return {"methods": [], "generated_by": "unavailable"}

    valid_p = {row["proposition"]["id"] for row in matrix}
    n_evidence = sum(len(row.get("links") or []) for row in matrix)
    for m in out.get("methods", []):
        m["relies_on"] = [x for x in m.get("relies_on", []) if x in valid_p]

    out["generated_by"] = "claude"
    out["meta"] = {"propositions": len(matrix), "evidence_links": n_evidence}
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Generate a preliminary quantum assessment.")
    ap.add_argument("--matrix", help="path to matrix.json (default: out/ then snapshot)")
    args = ap.parse_args()

    import graph_export
    matrix = graph_export.load_matrix(args.matrix)
    result = generate_quantum(matrix)

    out_path = _HERE / "out" / "quantum.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    snap = _HERE / "case_ui" / "data" / "quantum.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    n = len(result.get("methods", []))
    print(f"Generated {n} valuation methodologies ({result.get('generated_by')}). "
          f"Wrote {out_path} and snapshot {snap}.")
    if result.get("recommendation"):
        r = result["recommendation"]
        print(f"Recommended: {r['preferred_method']} — range "
              f"{result.get('currency','')} {r['range_low']:,.0f}–{r['range_high']:,.0f}.")


if __name__ == "__main__":
    main()
