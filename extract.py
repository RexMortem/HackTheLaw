#!/usr/bin/env python3
"""
Pass 1 — extraction.

Reads the converted Markdown, classifies each document, then:
  * extracts pleaded propositions (allegations / denials) from PLEADINGS, and
  * extracts evidence units from WITNESS STATEMENTS and EXHIBITS,
via Claude (structured outputs). Every extracted record carries provenance
(doc_id, page, paragraph, verbatim quote); quotes are verified against the
source text and flagged if they don't match.

Outputs:
  out/propositions.json   list of {id, type, text, party, responds_to, source, quote_ok}
  out/evidence.json       list of {id, assertion, witness, source, quote_ok}
  out/classification.csv  what kind each document was judged to be

Usage:
  python extract.py                       # batch mode (default), reads ./bundle_md
  python extract.py --mode sync           # sequential calls (easier to debug)
  python extract.py --limit 5             # only the first 5 docs (a cheap dry run)
  python extract.py --pleadings "POC*.md" # force a glob to be treated as pleadings
  python extract.py --no-llm              # force the zero-cost heuristic extractor
  python extract.py --no-cache            # force fresh LLM calls, ignore out/cache

Uses the LLM by default. Results are cached under out/cache keyed by (model,
prompt, schema), so re-running over an unchanged bundle makes no LLM calls. If the
LLM is unavailable when a fresh call is needed (no API key, no credits, offline),
it falls back automatically to the heuristic extractor for those documents.
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import glob
import os

import caselib as cl


def collect_docs(in_dir: str, pleadings_glob: str | None,
                 evidence_glob: str | None) -> list[cl.Document]:
    docs = []
    for path in sorted(glob.glob(os.path.join(in_dir, "*.md"))):
        if os.path.basename(path).lower() == "readme.md":
            continue
        doc = cl.parse_markdown(path)
        name = os.path.basename(path)
        if pleadings_glob and fnmatch.fnmatch(name, pleadings_glob):
            doc.kind = "pleading"
        elif evidence_glob and fnmatch.fnmatch(name, evidence_glob):
            doc.kind = "witness_statement"
        else:
            doc.kind = cl.classify(doc)
        docs.append(doc)
    return docs


def doc_user_prompt(doc: cl.Document) -> str:
    """The document text, with page markers, fed to the model."""
    lines = []
    cur = None
    for p in doc.paragraphs:
        if p.page != cur:
            cur = p.page
            lines.append(f"<!-- page {cur} -->")
        lines.append(p.text)
    header = f"Document: {doc.meta.get('title') or doc.doc_id}\n\n"
    return header + "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="bundle_md")
    ap.add_argument("--out", default="out")
    ap.add_argument("--mode", choices=["batch", "sync"], default="batch")
    ap.add_argument("--limit", type=int, default=0, help="cap number of docs (dry run)")
    ap.add_argument("--pleadings", default=None, help="glob to force-treat as pleadings")
    ap.add_argument("--evidence", default=None, help="glob to force-treat as evidence")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the on-disk extraction cache (always call the LLM)")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the LLM entirely; extract with the heuristic fallback")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    docs = collect_docs(args.input, args.pleadings, args.evidence)
    if args.limit:
        docs = docs[: args.limit]

    by_kind = {}
    for d in docs:
        by_kind.setdefault(d.kind, []).append(d)
    print("Documents:", {k: len(v) for k, v in by_kind.items()})

    # Persist the classification so it can be reviewed / corrected.
    with open(os.path.join(args.out, "classification.csv"), "w",
              newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", "kind", "pages", "witness_name", "file"])
        for d in docs:
            pages = max((p.page for p in d.paragraphs), default=0)
            w.writerow([d.doc_id, d.kind, pages,
                        d.meta.get("witness_name", ""), os.path.basename(d.path)])

    pleadings = by_kind.get("pleading", [])
    # Everything that isn't a pleading is treated as evidence — including any
    # "other"/unclassified docs, so nothing is silently dropped.
    evidence_docs = [d for d in docs if d.kind != "pleading"]
    if not pleadings:
        print("\n  NOTE: no pleadings detected. Propositions will be empty.\n"
              "  Add the Particulars of Claim / Defence to the input folder, or pass\n"
              "  --pleadings '<glob>' to designate them. Evidence extraction still runs.\n")

    # Use the LLM by default (results are cached, so re-runs are free). The
    # heuristic extractor is the fallback: forced with --no-llm, or automatic if
    # the LLM can't be reached (no key / no credits / offline) when a (cache-miss)
    # call is actually needed.
    cache_dir = None if (args.no_cache or args.no_llm) else os.path.join(args.out, "cache")
    use_llm = not args.no_llm
    client = None
    if use_llm:
        try:
            client = cl.get_client()
        except Exception as e:                      # e.g. no API key at construction
            reason = cl.llm_unavailable_reason(e) or str(e)
            print(f"\n  LLM unavailable ({reason}); using heuristic extraction.\n")
            use_llm = False

    # ---- Build jobs -------------------------------------------------------
    prop_jobs, ev_jobs = [], []
    for i, d in enumerate(pleadings):
        prop_jobs.append(dict(custom_id=f"prop-{i}", doc=d, kind="prop",
                              system=cl.PROPOSITIONS_SYSTEM,
                              user=doc_user_prompt(d),
                              schema=cl.PROPOSITIONS_SCHEMA, max_tokens=16000))
    for i, d in enumerate(evidence_docs):
        ev_jobs.append(dict(custom_id=f"ev-{i}", doc=d, kind="ev",
                            system=cl.EVIDENCE_SYSTEM,
                            user=doc_user_prompt(d),
                            schema=cl.EVIDENCE_SCHEMA, max_tokens=32000))

    def heuristic_result(job: dict) -> dict:
        if job["kind"] == "prop":
            return {"propositions": cl.heuristic_propositions(job["doc"])}
        return {"evidence": cl.heuristic_evidence(job["doc"])}

    # ---- Run --------------------------------------------------------------
    # Set once the LLM proves unavailable mid-run (no credits, dropped network),
    # so the rest of the run goes straight to the heuristic path without retrying.
    llm_down = {"reason": None}

    def _call_llm(pending):
        """Call the LLM for cache-miss jobs. Raises on an LLM-unavailable error
        so the caller can fall back; swallows genuine per-doc errors as None."""
        if args.mode == "batch":
            return cl.run_batch(client, [{k: j[k] for k in
                                          ("custom_id", "system", "user", "schema", "max_tokens")}
                                         for j in pending])
        fresh = {}
        for j in pending:
            print(f"  {j['custom_id']} {j['doc'].doc_id}")
            try:
                fresh[j["custom_id"]] = cl.run_sync(
                    client, j["system"], j["user"], j["schema"], j["max_tokens"])
            except Exception as e:
                if cl.llm_unavailable_reason(e):
                    raise                       # bubble up: fall back wholesale
                print(f"    ERROR {e}")          # genuine per-doc error
                fresh[j["custom_id"]] = None
        return fresh

    def run(jobs):
        """Serve cached results, call the LLM for the rest (caching successes),
        and fall back to the heuristic extractor for anything the LLM can't
        produce — automatically, when there are no credits / no network."""
        if not jobs:
            return {}
        results, pending = {}, []
        for j in jobs:
            j["_key"] = cl.llm_cache_key(j["system"], j["user"], j["schema"])
            hit = None if llm_down["reason"] else cl.cache_load(cache_dir, j["_key"])
            if hit is not None:
                results[j["custom_id"]] = hit
            else:
                pending.append(j)
        n_cached = len(jobs) - len(pending)
        if n_cached:
            print(f"  {n_cached}/{len(jobs)} from cache (no LLM call)")
        if not pending:
            return results

        fresh = {}
        if not llm_down["reason"]:
            try:
                fresh = _call_llm(pending)
            except Exception as e:
                llm_down["reason"] = cl.llm_unavailable_reason(e) or str(e)
                print(f"  LLM unavailable ({llm_down['reason']}); falling back to "
                      f"heuristic extraction for the rest of this run.")

        for j in pending:
            r = fresh.get(j["custom_id"])
            if r is None:                       # cache miss the LLM couldn't fill
                results[j["custom_id"]] = heuristic_result(j)   # not cached: retried with credits
            else:
                results[j["custom_id"]] = r
                cl.cache_store(cache_dir, j["_key"], r)
        return results

    if not use_llm:
        print("\nExtracting with the heuristic (no-LLM) extractor...")
        prop_res = {j["custom_id"]: heuristic_result(j) for j in prop_jobs}
        ev_res = {j["custom_id"]: heuristic_result(j) for j in ev_jobs}
    else:
        print("\nExtracting propositions from pleadings...")
        prop_res = run(prop_jobs)
        print("Extracting evidence from witness statements/exhibits...")
        ev_res = run(ev_jobs)

    # ---- Assemble propositions -------------------------------------------
    propositions = []
    pid = 0
    for j in prop_jobs:
        res = prop_res.get(j["custom_id"]) or {}
        d = j["doc"]
        for item in res.get("propositions", []):
            pid += 1
            propositions.append({
                "id": f"P{pid:04d}",
                "type": item.get("type", "allegation"),
                "text": item.get("text", "").strip(),
                "party": item.get("party", ""),
                "responds_to": item.get("responds_to", ""),
                "quote": item.get("quote", ""),
                "quote_ok": cl.verify_quote(item.get("quote", ""), d.full_text),
                "source": {"doc_id": d.doc_id, "page": item.get("page"),
                           "paragraph": item.get("paragraph", "")},
            })

    # ---- Assemble evidence ------------------------------------------------
    evidence = []
    eid = 0
    for j in ev_jobs:
        res = ev_res.get(j["custom_id"]) or {}
        d = j["doc"]
        wit = d.meta.get("witness_name", "")
        for item in res.get("evidence", []):
            eid += 1
            evidence.append({
                "id": f"E{eid:05d}",
                "assertion": item.get("assertion", "").strip(),
                "witness": item.get("witness") or wit,
                "quote": item.get("quote", ""),
                "quote_ok": cl.verify_quote(item.get("quote", ""), d.full_text),
                "source": {"doc_id": d.doc_id, "page": item.get("page"),
                           "paragraph": item.get("paragraph", "")},
            })

    cl.dump_json(os.path.join(args.out, "propositions.json"), propositions)
    cl.dump_json(os.path.join(args.out, "evidence.json"), evidence)

    p_ok = sum(p["quote_ok"] for p in propositions)
    e_ok = sum(e["quote_ok"] for e in evidence)
    print(f"\nPropositions: {len(propositions)} ({p_ok} quote-verified)")
    print(f"Evidence:     {len(evidence)} ({e_ok} quote-verified)")
    print(f"Wrote {args.out}/propositions.json, {args.out}/evidence.json, "
          f"{args.out}/classification.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
