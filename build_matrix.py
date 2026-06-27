#!/usr/bin/env python3
"""
Pass 2 — pleading-to-proof matrix.

For each pleaded proposition:
  1. retrieve the top-K candidate evidence units with BM25 (local, no API),
  2. ask Claude to classify each candidate as supportive / adverse / neutral
     (structured output, with a verbatim quote), and
  3. roll the links up into a per-proposition status:
        supported   — has supportive evidence, no adverse
        contested   — has both supportive and adverse
        undermined  — has adverse, no supportive
        MISSING     — no supportive/adverse evidence found (evidential gap)

Outputs:
  out/matrix.json   full structured matrix (propositions, links, status)
  out/matrix.csv    one row per (proposition, evidence link); gaps flagged
  out/matrix.md     readable report grouped by status, with citations

Usage:
  python build_matrix.py                 # batch, top-K=25, conf>=0.5
  python build_matrix.py --top-k 40 --min-confidence 0.6
  python build_matrix.py --mode sync --limit 10
  python build_matrix.py --no-cache      # force fresh LLM calls, ignore out/cache

Classifications are cached under out/cache keyed by (model, prompt, schema), so
re-running over unchanged propositions/evidence makes no LLM calls.
"""
from __future__ import annotations

import argparse
import csv
import os

import caselib as cl


def aggregate(links: list[dict], min_conf: float) -> str:
    strong = [l for l in links if l["confidence"] >= min_conf]
    sup = any(l["relation"] == "supportive" for l in strong)
    adv = any(l["relation"] == "adverse" for l in strong)
    if sup and adv:
        return "contested"
    if sup:
        return "supported"
    if adv:
        return "undermined"
    return "MISSING"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--mode", choices=["batch", "sync"], default="batch")
    ap.add_argument("--top-k", type=int, default=25,
                    help="candidate evidence units retrieved per proposition")
    ap.add_argument("--retriever", choices=["bm25", "embeddings", "hybrid"],
                    default="bm25",
                    help="candidate retrieval method (hybrid = BM25 + embeddings via RRF)")
    ap.add_argument("--embed-model", default=cl.DEFAULT_EMBED_MODEL,
                    help="Voyage embedding model (needs VOYAGE_API_KEY)")
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the on-disk classification cache (always call the LLM)")
    ap.add_argument("--limit", type=int, default=0, help="cap propositions (dry run)")
    ap.add_argument("--resume-batch", default=None,
                    help="recover a previously-submitted classification batch by id "
                         "(GET-only; no new requests / credits) instead of submitting")
    args = ap.parse_args()

    props = cl.load_json(os.path.join(args.out, "propositions.json"))
    evidence = cl.load_json(os.path.join(args.out, "evidence.json"))
    if args.limit:
        props = props[: args.limit]
    if not props:
        raise SystemExit("No propositions found. Run extract.py against a bundle "
                         "that includes pleadings first.")
    if not evidence:
        raise SystemExit("No evidence found in out/evidence.json.")

    ev_by_id = {e["id"]: e for e in evidence}
    ev_list = evidence
    corpus = [f'{e["assertion"]} {e.get("quote","")}' for e in evidence]

    # Build the chosen retriever (assertion + quote gives best recall).
    bm = cl.BM25(corpus)
    if args.retriever == "bm25":
        retriever = bm
    elif args.retriever == "embeddings":
        retriever = cl.EmbeddingRetriever(
            corpus, model=args.embed_model,
            cache_path=os.path.join(args.out, f"emb_{args.embed_model}.npz"))
    else:  # hybrid
        emb = cl.EmbeddingRetriever(
            corpus, model=args.embed_model,
            cache_path=os.path.join(args.out, f"emb_{args.embed_model}.npz"))
        retriever = cl.HybridRetriever([bm, emb])

    print(f"{len(props)} propositions, {len(evidence)} evidence units. "
          f"Retrieving top-{args.top_k} candidates each ({args.retriever})...")

    # Build classification jobs, one per proposition.
    jobs, cand_map = [], {}
    for i, p in enumerate(props):
        hits = retriever.search(p["text"], args.top_k)
        cands = [{"id": ev_list[idx]["id"], "assertion": ev_list[idx]["assertion"],
                  "witness": ev_list[idx].get("witness", ""),
                  "quote": ev_list[idx].get("quote", "")} for idx, _ in hits]
        cand_map[p["id"]] = cands
        if not cands:
            continue
        jobs.append(dict(custom_id=f"cls-{i}", prop_id=p["id"],
                         system=cl.CLASSIFY_SYSTEM,
                         user=cl.build_classify_user(p, cands),
                         schema=cl.CLASSIFY_SCHEMA, max_tokens=8000))

    # Cache key per proposition's classification request: identical (model,
    # prompt, schema) => cache hit, so re-runs over unchanged inputs cost nothing.
    cache_dir = None if args.no_cache else os.path.join(args.out, "cache")
    for j in jobs:
        j["_key"] = cl.llm_cache_key(j["system"], j["user"], j["schema"])

    client = cl.get_client()
    if args.resume_batch:
        print(f"Resuming classification batch {args.resume_batch} (GET-only)...")
        res = cl.collect_batch(client, args.resume_batch)
        for j in jobs:                       # backfill the cache from the recovered batch
            cl.cache_store(cache_dir, j["_key"], res.get(j["custom_id"]))
    else:
        # Serve cached classifications; only call the LLM for the rest.
        res, pending = {}, []
        for j in jobs:
            hit = cl.cache_load(cache_dir, j["_key"])
            if hit is not None:
                res[j["custom_id"]] = hit
            else:
                pending.append(j)
        n_cached = len(jobs) - len(pending)
        if n_cached:
            print(f"{n_cached}/{len(jobs)} classifications from cache (no LLM call)")

        if pending and args.mode == "batch":
            fresh = cl.run_batch(client, [{k: j[k] for k in
                                           ("custom_id", "system", "user", "schema", "max_tokens")}
                                          for j in pending])
        else:
            fresh = {}
            for j in pending:
                print(f"  {j['custom_id']} {j['prop_id']}")
                try:
                    fresh[j["custom_id"]] = cl.run_sync(
                        client, j["system"], j["user"], j["schema"], j["max_tokens"])
                except Exception as e:
                    print(f"    ERROR {e}")
                    fresh[j["custom_id"]] = None
        for j in pending:
            r = fresh.get(j["custom_id"])
            res[j["custom_id"]] = r
            cl.cache_store(cache_dir, j["_key"], r)

    cls_by_prop = {j["prop_id"]: (res.get(j["custom_id"]) or {}).get("classifications", [])
                   for j in jobs}

    # ---- Assemble the matrix ---------------------------------------------
    matrix = []
    for p in props:
        links = []
        for c in cls_by_prop.get(p["id"], []):
            ev = ev_by_id.get(c.get("evidence_id"))
            if not ev:
                continue
            quote = c.get("quote", "")
            links.append({
                "evidence_id": ev["id"],
                "relation": c.get("relation", "neutral"),
                "confidence": float(c.get("confidence", 0.0) or 0.0),
                "rationale": c.get("rationale", ""),
                "quote": quote,
                "quote_ok": cl.verify_quote(quote, ev["assertion"] + " " + ev.get("quote", "")),
                "witness": ev.get("witness", ""),
                "evidence_source": ev["source"],
            })
        links.sort(key=lambda l: l["confidence"], reverse=True)
        matrix.append({
            "proposition": p,
            "status": aggregate(links, args.min_confidence),
            "n_candidates": len(cand_map.get(p["id"], [])),
            "links": links,
        })

    cl.dump_json(os.path.join(args.out, "matrix.json"), matrix)
    write_csv(os.path.join(args.out, "matrix.csv"), matrix, args.min_confidence)
    write_md(os.path.join(args.out, "matrix.md"), matrix, args.min_confidence)

    counts = {}
    for m in matrix:
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    print("\nProposition status:", counts)
    print(f"Wrote {args.out}/matrix.json, {args.out}/matrix.csv, {args.out}/matrix.md")
    return 0


def write_csv(path, matrix, min_conf):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["prop_id", "prop_type", "party", "status", "proposition",
                    "relation", "confidence", "evidence_id", "witness",
                    "evidence_doc", "evidence_page", "evidence_para",
                    "rationale", "quote", "quote_ok"])
        for m in matrix:
            p = m["proposition"]
            if not m["links"]:
                w.writerow([p["id"], p["type"], p.get("party", ""), m["status"],
                            p["text"], "MISSING", "", "", "", "", "", "",
                            "no evidence retrieved", "", ""])
                continue
            for l in m["links"]:
                if l["confidence"] < min_conf:
                    continue
                s = l["evidence_source"]
                w.writerow([p["id"], p["type"], p.get("party", ""), m["status"],
                            p["text"], l["relation"], f'{l["confidence"]:.2f}',
                            l["evidence_id"], l["witness"], s.get("doc_id", ""),
                            s.get("page", ""), s.get("paragraph", ""),
                            l["rationale"], l["quote"], l["quote_ok"]])


_ORDER = ["MISSING", "undermined", "contested", "supported"]


def write_md(path, matrix, min_conf):
    buckets = {k: [] for k in _ORDER}
    for m in matrix:
        buckets.setdefault(m["status"], []).append(m)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Pleading-to-Proof Matrix\n\n")
        fh.write("Per-proposition status. **MISSING** = no supportive or adverse "
                 "evidence found (evidential gap). Links shown at "
                 f"confidence ≥ {min_conf}.\n\n")
        fh.write("| Status | Count |\n|---|---|\n")
        for k in _ORDER:
            fh.write(f"| {k} | {len(buckets.get(k, []))} |\n")
        fh.write("\n")
        for status in _ORDER:
            items = buckets.get(status, [])
            if not items:
                continue
            fh.write(f"\n## {status} ({len(items)})\n\n")
            for m in items:
                p = m["proposition"]
                src = p["source"]
                fh.write(f"### {p['id']} — {p['type']} ({p.get('party','')})\n\n")
                fh.write(f"> {p['text']}\n\n")
                fh.write(f"*Pleaded at {src.get('doc_id','')} "
                         f"p{src.get('page','?')} ¶{src.get('paragraph','')}*\n\n")
                qualifying = [l for l in m["links"] if l["confidence"] >= min_conf]
                # The report shows only decision-relevant links (supportive /
                # adverse); neutrals are counted. Full detail is in matrix.csv.
                shown = [l for l in qualifying if l["relation"] != "neutral"]
                n_neutral = len(qualifying) - len(shown)
                if not shown:
                    fh.write(f"_No supportive or adverse evidence "
                             f"({n_neutral} neutral on-topic)._\n\n")
                    continue
                for l in shown:
                    s = l["evidence_source"]
                    flag = "" if l["quote_ok"] else " ⚠unverified-quote"
                    fh.write(f"- **{l['relation']}** ({l['confidence']:.2f}){flag} "
                             f"— {l['witness']} {s.get('doc_id','')} "
                             f"p{s.get('page','?')} ¶{s.get('paragraph','')}: "
                             f"{l['rationale']}\n")
                    if l["quote"]:
                        fh.write(f'  > "{l["quote"]}"\n')
                if n_neutral:
                    fh.write(f"- _(+{n_neutral} neutral on-topic items — see matrix.csv)_\n")
                fh.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
