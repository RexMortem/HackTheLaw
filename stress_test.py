"""
stress_test — stress-test the case theory (and a built strategy plan).

This is the "find the weak point before opposing counsel does" suite. It runs a
battery of checks over the proof matrix, the dependency graph, the generated
arguments and (optionally) the lawyer's built case, and reports findings grouped
by the six capability lenses the challenge calls for:

  1. extraction     — pleading & proposition extraction integrity
  2. classification — evidence categorisation (supportive/adverse/neutral/missing)
  3. grounding      — source-grounded reasoning (every output traces to a quote)
  4. contradiction  — contradiction & gap detection
  5. human_review   — organised, prioritised risk-spotting for the lawyer
  6. stress         — case-theory stress-testing (single points of failure, weak
                      arguments, dependency cascades, circular reasoning)

The deterministic checks reuse graph_export's analytics (articulation points,
single points of failure, contradictions, circular reasoning) and the matrix's
own provenance flags (quote_ok, confidence) — no LLM, so they always run, fast.

With adversarial=True it adds a red-team pass: Claude argues the *other* side
against each generated argument and names its weakest link, and Perplexity looks
for contrary authority. These are the researched techniques (adversarial
verification + real-time legal search) applied to the strategy plan.

Used by the web app (/api/stress) and runnable standalone:
    python stress_test.py                 # deterministic checks
    python stress_test.py --adversarial   # + LLM red-team and contrary authority
"""
from __future__ import annotations

import json
import pathlib

import graph_export

_HERE = pathlib.Path(__file__).parent

LENSES = [
    ("extraction", "Pleading & proposition extraction"),
    ("classification", "Evidence classification"),
    ("grounding", "Source-grounded reasoning"),
    ("contradiction", "Contradiction & gap detection"),
    ("human_review", "Human-in-the-loop review"),
    ("stress", "Case-theory stress-testing"),
]

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
# robustness-score penalty per finding type (capped by count below)
_PENALTY = {"critical": 10, "high": 6, "medium": 3, "low": 1, "info": 0}


def _index(matrix: list) -> dict:
    return {row["proposition"]["id"]: row for row in matrix if row.get("proposition")}


def _finding(severity, title, detail, items=None, capability=""):
    return {"severity": severity, "title": title, "detail": detail,
            "items": items or [], "capability": capability}


# ---------------------------------------------------------------------------
# Deterministic checks (no LLM)
# ---------------------------------------------------------------------------
def _check_extraction(matrix: list) -> list[dict]:
    out = []
    total = len(matrix)
    unverified = [r["proposition"]["id"] for r in matrix
                  if not r["proposition"].get("quote_ok", True)]
    if unverified:
        out.append(_finding(
            "high", f"{len(unverified)} proposition(s) have an unverified source quote",
            "The pleaded text could not be matched verbatim to the source document, "
            "so the extraction may be paraphrased or misattributed. Verify against the "
            "pleading before relying on it.", unverified))
    orphan_denials = [r["proposition"]["id"] for r in matrix
                      if r["proposition"].get("type") == "denial"
                      and not r["proposition"].get("responds_to")]
    if orphan_denials:
        out.append(_finding(
            "low", f"{len(orphan_denials)} denial(s) not linked to an allegation",
            "These denials don't identify the allegation they answer, so the "
            "allegation/response pairing can't be checked automatically.", orphan_denials))
    out.append(_finding("info", f"{total} pleaded propositions extracted",
                        "Atomic allegations and denials parsed from the pleadings.",
                        capability="extraction"))
    return out


def _check_classification(matrix: list, min_conf: float = 0.5) -> list[dict]:
    out = []
    fragile, unverified_links = [], 0
    for r in matrix:
        links = r.get("links") or []
        probative = [l for l in links if l.get("relation") in ("supportive", "adverse")]
        if probative and all((l.get("confidence") or 0) < min_conf for l in probative):
            fragile.append(r["proposition"]["id"])
        unverified_links += sum(1 for l in links if not l.get("quote_ok", True))
    if fragile:
        out.append(_finding(
            "medium", f"{len(fragile)} proposition(s) rest on low-confidence evidence only",
            f"Every supporting/adverse link is below {min_conf:.0%} confidence — the "
            "classification is shaky and a small change could flip the status.", fragile))
    if unverified_links:
        out.append(_finding(
            "medium", f"{unverified_links} evidence link(s) carry an unverified quote",
            "The cited evidence quote couldn't be matched to the source, so the "
            "support/adverse call rests on an unverifiable extract.", capability="classification"))
    return out


def _check_grounding(matrix: list) -> list[dict]:
    out = []
    missing_cite = []
    for r in matrix:
        for l in (r.get("links") or []):
            src = l.get("evidence_source") or {}
            if not src.get("doc_id"):
                missing_cite.append(l.get("evidence_id", "?"))
    if missing_cite:
        out.append(_finding(
            "high", f"{len(missing_cite)} evidence link(s) lack a document citation",
            "Without a doc/page/paragraph these conclusions can't be audited back to "
            "the bundle.", missing_cite[:20]))
    else:
        out.append(_finding(
            "info", "Every evidence link carries a document citation",
            "Each support/adverse call traces to a doc_id, page and paragraph, so the "
            "analysis is auditable end to end.", capability="grounding"))
    return out


def _check_contradiction(matrix: list, graph: dict) -> list[dict]:
    out = []
    idx = _index(matrix)
    gaps = [r["proposition"]["id"] for r in matrix if r.get("status") == "MISSING"]
    undermined = [r["proposition"]["id"] for r in matrix if r.get("status") == "undermined"]
    contradicted = [n["id"] for n in graph["nodes"]
                    if n["kind"] == "proposition" and n.get("contradicted")]
    if gaps:
        out.append(_finding(
            "critical", f"{len(gaps)} pleaded proposition(s) have NO supporting evidence",
            "These are evidential gaps — pleaded but unproved. They need witness, expert "
            "or documentary support, or should be dropped from the case theory.", gaps))
    if undermined:
        out.append(_finding(
            "high", f"{len(undermined)} proposition(s) are undermined by adverse evidence",
            "The weight of evidence currently cuts against these.", undermined))
    if contradicted:
        out.append(_finding(
            "high", f"{len(contradicted)} proposition(s) have evidence cutting BOTH ways",
            "Supportive and adverse evidence coexist — a live factual dispute the trial "
            "will turn on. Resolve which evidence is stronger before relying on it.",
            contradicted))
    cycles = graph["stats"].get("circular_reasoning") or []
    if cycles:
        out.append(_finding(
            "critical", f"{len(cycles)} circular-reasoning loop(s) in the case logic",
            "A proposition is, transitively, used to prove itself. Break the loop with an "
            "independent premise.", [p for c in cycles for p in c["members"]]))
    else:
        out.append(_finding(
            "info", "No circular reasoning detected",
            "The proposition dependency graph is acyclic — the case theory's logic is "
            "well-founded.", capability="contradiction"))
    return out


def _check_eu_authority(matrix: list, limit: int = 6) -> list[dict]:
    """For evidential gaps / undermined propositions, surface persuasive EU
    authority (CJEU / directives) from the EU Cellar that could help fill the
    gap — post-Brexit EU law is no longer binding but can still persuade.
    Best-effort (no key needed); returns [] if Cellar is unreachable."""
    import caselib
    out, cache = [], {}
    weak = [r for r in matrix if r.get("status") in ("MISSING", "undermined")]
    for r in weak[:limit]:
        p = r["proposition"]
        terms = caselib.concepts_in(p.get("text", ""))
        if not terms:
            continue
        key = tuple(sorted(terms))
        if key not in cache:
            cache[key] = caselib.cellar_search(list(key), limit=3)
        mats = cache[key]
        if not mats:
            continue
        f = _finding(
            "info", f"Persuasive EU authority may bear on {p.get('id')}",
            f"{p.get('id')} is {r.get('status')}; the EU Cellar has materials on "
            f"{', '.join(terms)} that could be argued as persuasive authority.",
            [p.get("id")], capability="contradiction")
        f["sources"] = [{"title": m["title"] + (f" (CELEX {m['celex']})" if m["celex"] else ""),
                         "url": m["work"]} for m in mats]
        out.append(f)
    return out


def _check_human_review(matrix: list, graph: dict) -> list[dict]:
    """Organise the riskiest items for a lawyer to judge — the tool spots and
    prioritises; the lawyer decides."""
    pr = {n["id"]: n.get("pagerank", 0) for n in graph["nodes"]}
    risky = [r for r in matrix if r.get("status") in ("MISSING", "undermined", "contested")]
    risky.sort(key=lambda r: (
        {"MISSING": 0, "undermined": 1, "contested": 2}.get(r["status"], 3),
        -pr.get(r["proposition"]["id"], 0)))
    top = [r["proposition"]["id"] for r in risky[:8]]
    out = [_finding(
        "info", f"{len(risky)} proposition(s) flagged for a lawyer's judgement",
        "Prioritised by status and centrality. The tool surfaces and organises these; "
        "the final call on weight and strategy remains with you.", top,
        capability="human_review")]
    return out


def _check_stress(matrix: list, graph: dict, arguments: list) -> list[dict]:
    out = []
    idx = _index(matrix)
    stats = graph["stats"]

    spof = stats.get("single_points_of_failure") or []
    if spof:
        items = [s["id"] for s in spof]
        detail = "; ".join(
            f"{s['witness'] or s['id']} solely supports {', '.join(s['props'])}"
            for s in spof[:5])
        out.append(_finding(
            "high", f"{len(spof)} single point(s) of failure in the evidence",
            "A proposition's entire support rests on one piece of evidence — discredit it "
            "and the proposition collapses. " + detail, items))

    artic = [n["id"] for n in graph["nodes"] if n.get("articulation")]
    if artic:
        out.append(_finding(
            "medium", f"{len(artic)} node(s) are structural cut-points",
            "Removing any one fragments the support graph — high-leverage targets for "
            "cross-examination (yours or theirs).", artic[:20]))

    # arguments resting on weak ground
    weak_args = []
    for a in arguments or []:
        weak = [pid for pid in a.get("relies_on", [])
                if idx.get(pid, {}).get("status") in ("MISSING", "undermined")]
        if weak:
            weak_args.append((a, weak))
    for a, weak in weak_args:
        out.append(_finding(
            "high", f"Argument “{a.get('issue', a.get('id'))}” rests on weak ground",
            f"It relies on {', '.join(weak)}, which the matrix scores as unsupported or "
            f"undermined. {a.get('vulnerability', '')}".strip(),
            [a.get("id")] + weak))

    # dependency cascade: weak premises that others build on
    dep_children: dict[str, list] = {}
    for e in graph["edges"]:
        if e["relation"] == "DEPENDS_ON":
            dep_children.setdefault(e["target"], []).append(e["source"])
    for premise, dependents in dep_children.items():
        if idx.get(premise, {}).get("status") in ("MISSING", "undermined") and dependents:
            out.append(_finding(
                "high", f"Weak premise {premise} carries {len(dependents)} dependent proposition(s)",
                f"{premise} is unsupported/undermined yet {', '.join(dependents)} depend on "
                "it — if it falls, they fall with it.", [premise] + dependents))

    if not out:
        out.append(_finding("info", "No structural single points of failure found",
                            "No proposition depends on a lone piece of evidence.",
                            capability="stress"))
    return out


# ---------------------------------------------------------------------------
# Adversarial red-team (LLM + Perplexity) — optional
# ---------------------------------------------------------------------------
REDTEAM_SCHEMA = {
    "type": "object",
    "properties": {
        "rebuttals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "argument_id": {"type": "string"},
                    "attack": {"type": "string"},
                    "weakest_link": {"type": "string"},
                    "severity": {"type": "string",
                                 "enum": ["critical", "high", "medium", "low"]},
                },
                "required": ["argument_id", "attack", "weakest_link", "severity"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rebuttals"],
    "additionalProperties": False,
}

REDTEAM_SYSTEM = """\
You are opposing counsel in an English commercial dispute. You are shown the \
other side's arguments, each with the propositions and evidence it relies on. \
For each argument, attack it: state the strongest rebuttal you would run, name \
its single weakest link, and rate how damaging your attack is (severity). Be \
specific and realistic — no boilerplate. Return JSON only."""


def _redteam(arguments: list) -> list[dict]:
    import caselib
    if not arguments:
        return []
    digest = "\n".join(
        f"[{a.get('id')}] ({a.get('party')}) {a.get('thesis')} "
        f"| relies_on={a.get('relies_on')} evidence={a.get('evidence')} "
        f"| stated vulnerability: {a.get('vulnerability', '')}"
        for a in arguments)
    try:
        client = caselib.get_client()
        out = caselib.run_json(client, REDTEAM_SYSTEM,
                               "ARGUMENTS TO ATTACK:\n" + digest,
                               REDTEAM_SCHEMA, max_tokens=8000)
        rebuttals = out.get("rebuttals", [])
    except Exception as exc:
        print(f"  [stress] red-team unavailable: {type(exc).__name__}: {exc}")
        return []

    findings = []
    for r in rebuttals:
        findings.append(_finding(
            r.get("severity", "medium"),
            f"Opposing counsel attacks {r.get('argument_id')}",
            r.get("attack", "") + (f"  Weakest link: {r['weakest_link']}."
                                   if r.get("weakest_link") else ""),
            [r.get("argument_id")], capability="stress"))
    return findings


def _contrary_authority(arguments: list, limit: int = 4) -> list[dict]:
    import caselib
    if not caselib.perplexity_available():
        return []
    system = ("You are opposing counsel's researcher. Find REAL contrary authority "
              "(English cases/statutes/principles) that CUTS AGAINST the argument "
              "below. One or two sentences, cite sources. If none, say so.")
    findings = []
    for a in (arguments or [])[:limit]:
        q = (f"Argument to undermine: {a.get('thesis')} ({a.get('party')} in an "
             "English commercial dispute). What authority cuts against it?")
        text, cites = caselib.perplexity_chat(system, q, max_tokens=350)
        if text and cites:
            f = _finding("medium", f"Contrary authority on “{a.get('issue', a.get('id'))}”",
                         text, [a.get("id")], capability="stress")
            f["sources"] = cites[:3]
            findings.append(f)
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(matrix: list, case: dict | None = None, arguments: list | None = None,
        adversarial: bool = False) -> dict:
    """Run the full suite and return a structured report."""
    graph = graph_export.build_graph(matrix)
    arguments = arguments or []

    by_lens: dict[str, list] = {k: [] for k, _ in LENSES}
    by_lens["extraction"] += _check_extraction(matrix)
    by_lens["classification"] += _check_classification(matrix)
    by_lens["grounding"] += _check_grounding(matrix)
    by_lens["contradiction"] += _check_contradiction(matrix, graph)
    by_lens["contradiction"] += _check_eu_authority(matrix)
    by_lens["human_review"] += _check_human_review(matrix, graph)
    by_lens["stress"] += _check_stress(matrix, graph, arguments)

    if adversarial:
        by_lens["stress"] += _redteam(arguments)
        by_lens["stress"] += _contrary_authority(arguments)

    # tag findings with their lens and sort each lens by severity
    lenses = []
    counts = {s: 0 for s in _SEVERITY_RANK}
    penalty = 0
    pen_count = {s: 0 for s in _PENALTY}
    for key, title in LENSES:
        findings = sorted(by_lens[key],
                          key=lambda f: -_SEVERITY_RANK.get(f["severity"], 0))
        for f in findings:
            f["lens"] = key
            counts[f["severity"]] += 1
            # cap the penalty contribution per severity tier so a long tail of
            # low findings can't sink the score
            if pen_count[f["severity"]] < 6:
                penalty += _PENALTY[f["severity"]]
                pen_count[f["severity"]] += 1
        lenses.append({"key": key, "title": title, "findings": findings})

    score = max(0, 100 - penalty)
    crit, high = counts["critical"], counts["high"]
    if crit:
        summary = (f"{crit} critical and {high} high-severity issue(s) — the case "
                   "theory has gaps that need closing before trial.")
    elif high:
        summary = (f"{high} high-severity issue(s) and no critical gaps — broadly "
                   "sound but with weak points to shore up.")
    else:
        summary = "No critical or high-severity issues — the case theory holds up well."

    return {
        "score": score,
        "summary": summary,
        "counts": counts,
        "adversarial": adversarial,
        "case_scoped": bool(case and case.get("propositions")),
        "lenses": lenses,
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Stress-test the case theory.")
    ap.add_argument("--matrix", help="path to matrix.json (default: out/ then snapshot)")
    ap.add_argument("--adversarial", action="store_true",
                    help="add the LLM red-team + Perplexity contrary-authority checks")
    args = ap.parse_args()

    matrix = graph_export.load_matrix(args.matrix)
    arguments = []
    snap = _HERE / "case_ui" / "data" / "arguments.json"
    if snap.exists():
        arguments = (json.loads(snap.read_text(encoding="utf-8")) or {}).get("arguments", [])

    report = run(matrix, arguments=arguments, adversarial=args.adversarial)
    print(f"Robustness score: {report['score']}/100 — {report['summary']}")
    for lens in report["lenses"]:
        print(f"\n## {lens['title']}")
        for f in lens["findings"]:
            print(f"  [{f['severity'].upper()}] {f['title']}")


if __name__ == "__main__":
    main()
