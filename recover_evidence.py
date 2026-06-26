#!/usr/bin/env python3
"""
Recover evidence for documents missing from out/evidence.json.

A document is "missing" if it is an evidence doc (non-pleading) that produced
zero evidence units — which in practice means its extraction was dropped (e.g.
output truncated and the JSON failed to parse). Re-extracts only those docs at a
higher max_tokens and appends them to evidence.json with fresh IDs.

Usage:
  python recover_evidence.py                 # sync, max_tokens=48000
  python recover_evidence.py --mode batch
"""
from __future__ import annotations

import argparse
import os

import caselib as cl
from extract import collect_docs, doc_user_prompt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="converted")
    ap.add_argument("--out", default="out")
    ap.add_argument("--mode", choices=["batch", "sync"], default="sync")
    ap.add_argument("--max-tokens", type=int, default=48000)
    args = ap.parse_args()

    ev_path = os.path.join(args.out, "evidence.json")
    evidence = cl.load_json(ev_path)
    present = {e["source"]["doc_id"] for e in evidence}
    next_id = max((int(e["id"][1:]) for e in evidence), default=0)

    docs = collect_docs(args.input, None, None)
    missing = [d for d in docs if d.kind != "pleading" and d.doc_id not in present]
    if not missing:
        print("Nothing to recover — every evidence doc is represented.")
        return 0
    print(f"Recovering {len(missing)} missing doc(s):")
    for d in missing:
        print(f"  - {d.doc_id}")

    client = cl.get_client()
    jobs = [dict(custom_id=f"rec-{i}", doc=d, system=cl.EVIDENCE_SYSTEM,
                 user=doc_user_prompt(d), schema=cl.EVIDENCE_SCHEMA,
                 max_tokens=args.max_tokens)
            for i, d in enumerate(missing)]

    if args.mode == "batch":
        res = cl.run_batch(client, [{k: j[k] for k in
                                     ("custom_id", "system", "user", "schema", "max_tokens")}
                                    for j in jobs])
    else:
        res = {}
        for j in jobs:
            print(f"  extracting {j['doc'].doc_id} ...")
            try:
                res[j["custom_id"]] = cl.run_sync(
                    client, j["system"], j["user"], j["schema"], j["max_tokens"])
            except Exception as e:
                print(f"    ERROR {e}")
                res[j["custom_id"]] = None

    added = 0
    for j in jobs:
        r = res.get(j["custom_id"]) or {}
        d = j["doc"]
        wit = d.meta.get("witness_name", "")
        for item in r.get("evidence", []):
            next_id += 1
            added += 1
            evidence.append({
                "id": f"E{next_id:05d}",
                "assertion": item.get("assertion", "").strip(),
                "witness": item.get("witness") or wit,
                "quote": item.get("quote", ""),
                "quote_ok": cl.verify_quote(item.get("quote", ""), d.full_text),
                "source": {"doc_id": d.doc_id, "page": item.get("page"),
                           "paragraph": item.get("paragraph", "")},
            })

    cl.dump_json(ev_path, evidence)
    print(f"\nAdded {added} evidence units from {len(missing)} docs. "
          f"evidence.json now has {len(evidence)} units.")
    print("Re-run build_matrix.py to include them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
