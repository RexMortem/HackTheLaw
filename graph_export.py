"""
graph_export — turn the pleading-to-proof matrix into a graph.

The proof matrix is already a graph flattened into a table: each pleaded
proposition links to the evidence that bears on it. This module makes that graph
explicit as nodes + edges, which is the single representation we feed to BOTH:

  * the web UI's "Case Graph" view (out/graph.json -> /api/graph), and
  * Neo4j (out/graph.cypher, or neo4j_load.py via the official driver).

Graph model
-----------
Nodes:
  (:Proposition {id, text, type, party, status})   one per pleaded proposition
  (:Evidence    {id, witness, doc_id, quote})       one per evidence item (deduped)

Edges (evidence -> proposition; reads "this evidence SUPPORTS that proposition"):
  [:SUPPORTS   {confidence, rationale, quote}]       link.relation == supportive
  [:UNDERMINES {confidence, rationale, quote}]       link.relation == adverse
  [:NEUTRAL    {confidence, rationale, quote}]       link.relation == neutral

Derived, for the load-bearing / contradiction views:
  * each Evidence node carries `supports` / `undermines` counts and a `degree`
    (distinct propositions it touches) so the UI can size "load-bearing" evidence;
  * each Proposition node is flagged `contested` when it has BOTH supportive and
    adverse evidence — i.e. the evidence contradicts itself on that point.

Pure stdlib. Run:
    python graph_export.py            # out/matrix.json -> out/graph.{json,cypher}
                                      #                  + case_ui/data/graph.json
"""
from __future__ import annotations

import json
import os
import pathlib
from collections import Counter, defaultdict

# supportive/adverse/neutral (matrix link vocabulary) -> graph edge type
_REL = {"supportive": "SUPPORTS", "adverse": "UNDERMINES", "neutral": "NEUTRAL"}

_HERE = pathlib.Path(__file__).parent
_DEFAULT_MATRIX = _HERE / "out" / "matrix.json"
_FALLBACK_MATRIX = _HERE / "case_ui" / "data" / "matrix.json"


def _short(text: str, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Graph analytics — pure Python so they ship in graph.json / the app with no
# Neo4j required. neo4j_load.py runs the equivalent Neo4j GDS procedures
# (gds.pageRank / gds.louvain / gds.articulationPoints) for those who load the
# graph into Aura; the algorithms below mirror those so the app and the database
# tell the same story. All run on the *probative* subgraph (SUPPORTS +
# UNDERMINES, undirected) — neutral "on-topic but non-probative" links would
# only add noise to centrality and clustering.
# ---------------------------------------------------------------------------
def _probative_adjacency(node_ids: set, edges: list) -> dict[str, set]:
    adj: dict[str, set] = {n: set() for n in node_ids}
    for e in edges:
        if e["relation"] not in ("SUPPORTS", "UNDERMINES"):
            continue
        a, b = e["source"], e["target"]
        if a in adj and b in adj and a != b:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def _pagerank(node_ids: list, adj: dict, damping: float = 0.85,
              iters: int = 60) -> dict[str, float]:
    """Undirected PageRank by power iteration (each edge flows both ways, so
    evidence and propositions reinforce each other — important evidence backs
    important propositions). Returned normalised to 0..1 by the max."""
    n = len(node_ids)
    if n == 0:
        return {}
    pr = {x: 1.0 / n for x in node_ids}
    deg = {x: (len(adj[x]) or 1) for x in node_ids}
    for _ in range(iters):
        nxt = {x: (1 - damping) / n for x in node_ids}
        dangling = damping * sum(pr[x] for x in node_ids if not adj[x]) / n
        for x in node_ids:
            if adj[x]:
                share = damping * pr[x] / deg[x]
                for m in adj[x]:
                    nxt[m] += share
            nxt[x] += dangling
        pr = nxt
    top = max(pr.values()) or 1.0
    return {x: pr[x] / top for x in node_ids}


def _label_propagation(node_ids: list, adj: dict, rounds: int = 40) -> dict[str, int]:
    """Deterministic label propagation (a fast community detector akin to
    Louvain): each node adopts the commonest label among its neighbours, ties
    broken by lowest label id, nodes visited in fixed order — so a given graph
    always yields the same clustering."""
    order = sorted(node_ids)
    comm = {x: i for i, x in enumerate(order)}
    for _ in range(rounds):
        changed = False
        for x in order:
            if not adj[x]:
                continue
            counts: Counter = Counter(comm[m] for m in adj[x])
            best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
            if comm[x] != best:
                comm[x] = best
                changed = True
        if not changed:
            break
    return comm


def _articulation_points(node_ids: list, adj: dict) -> set:
    """Hopcroft–Tarjan cut vertices: nodes whose removal disconnects the
    support graph — i.e. single points of failure in the case theory."""
    visited, disc, low, parent, ap = set(), {}, {}, {}, set()
    timer = [0]
    import sys
    sys.setrecursionlimit(max(10000, len(node_ids) * 4))

    def dfs(root):
        # iterative DFS to stay safe on big bundles
        stack = [(root, None, iter(sorted(adj[root])))]
        visited.add(root)
        disc[root] = low[root] = timer[0]; timer[0] += 1
        parent[root] = None
        children_count = {root: 0}
        while stack:
            u, pu, it = stack[-1]
            advanced = False
            for v in it:
                if v not in visited:
                    parent[v] = u
                    children_count[u] = children_count.get(u, 0) + 1
                    visited.add(v)
                    disc[v] = low[v] = timer[0]; timer[0] += 1
                    children_count[v] = 0
                    stack.append((v, u, iter(sorted(adj[v]))))
                    advanced = True
                    break
                elif v != pu:
                    low[u] = min(low[u], disc[v])
            if not advanced:
                stack.pop()
                if pu is not None:
                    low[pu] = min(low[pu], low[u])
                    if parent[pu] is not None and low[u] >= disc[pu]:
                        ap.add(pu)
        if children_count.get(root, 0) > 1:
            ap.add(root)

    for x in sorted(node_ids):
        if x not in visited:
            dfs(x)
    return ap


def _strongly_connected_components(node_ids: list, out_adj: dict) -> list[list]:
    """Tarjan's SCC over a DIRECTED graph (out_adj[u] = set of successors).
    Components of size > 1 (or a self-loop) are cycles — here, circular
    reasoning in the proposition dependency graph."""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    counter = [0]
    result = []

    def strongconnect(v0):
        work = [(v0, iter(sorted(out_adj.get(v0, ()))))]
        index[v0] = low[v0] = counter[0]; counter[0] += 1
        stack.append(v0); on_stack.add(v0)
        while work:
            v, it = work[-1]
            recurse = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]; counter[0] += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, iter(sorted(out_adj.get(w, ())))))
                    recurse = True
                    break
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop(); on_stack.discard(w); comp.append(w)
                    if w == v:
                        break
                result.append(comp)
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])

    for v in sorted(node_ids):
        if v not in index:
            strongconnect(v)
    return result


def load_dependencies(path: str | None = None) -> list[dict]:
    """Load proposition dependency edges (from derive_dependencies.py), preferring
    the live output, then the committed snapshot. Returns [] if none exist —
    dependencies are an optional enrichment, so the graph works without them."""
    candidates = ([path] if path else
                  [_HERE / "out" / "dependencies.json",
                   _HERE / "case_ui" / "data" / "dependencies.json"])
    for c in candidates:
        if c and pathlib.Path(c).exists():
            try:
                with open(c, encoding="utf-8") as fh:
                    return (json.load(fh) or {}).get("dependencies", [])
            except (json.JSONDecodeError, OSError):
                return []
    return []


def build_graph(matrix: list, include_neutral: bool = True,
                min_confidence: float = 0.0,
                dependencies: list | None = None) -> dict:
    """Build {nodes, edges, stats} from a loaded matrix list.

    include_neutral / min_confidence let callers thin the graph; we keep
    everything by default and let the UI filter, so the same export drives
    every view.
    """
    prop_nodes: dict[str, dict] = {}
    ev_nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for row in matrix:
        p = row.get("proposition", {})
        pid = p.get("id")
        if not pid:
            continue
        src = p.get("source") or {}
        prop_nodes[pid] = {
            "id": pid,
            "kind": "proposition",
            "label": _short(p.get("text", "")),
            "text": p.get("text", ""),
            "type": p.get("type", ""),
            "party": p.get("party", ""),
            "status": row.get("status", "MISSING"),
            "responds_to": p.get("responds_to", ""),
            "doc_id": src.get("doc_id", ""),
            "page": src.get("page"),
            "paragraph": src.get("paragraph", ""),
            # filled in below
            "supports": 0,
            "undermines": 0,
        }

        for link in row.get("links", []) or []:
            relation = _REL.get(link.get("relation", ""), None)
            if relation is None:
                continue
            conf = float(link.get("confidence") or 0.0)
            if relation == "NEUTRAL" and not include_neutral:
                continue
            if conf < min_confidence:
                continue

            eid = link.get("evidence_id")
            if not eid:
                continue
            es = link.get("evidence_source") or {}
            node = ev_nodes.setdefault(eid, {
                "id": eid,
                "kind": "evidence",
                "label": _short(link.get("quote") or link.get("rationale") or eid, 70),
                "witness": link.get("witness", ""),
                "doc_id": es.get("doc_id", ""),
                "page": es.get("page"),
                "paragraph": es.get("paragraph", ""),
                "quote": link.get("quote", ""),
                "supports": 0,
                "undermines": 0,
                "neutral": 0,
                "_props": set(),
                "_probative": set(),   # props it actually supports/undermines
            })
            node["_props"].add(pid)
            if relation == "SUPPORTS":
                node["supports"] += 1
                node["_probative"].add(pid)
                prop_nodes[pid]["supports"] += 1
            elif relation == "UNDERMINES":
                node["undermines"] += 1
                node["_probative"].add(pid)
                prop_nodes[pid]["undermines"] += 1
            else:
                node["neutral"] += 1

            edges.append({
                "source": eid,
                "target": pid,
                "relation": relation,
                "confidence": round(conf, 3),
                "rationale": link.get("rationale", ""),
                "quote": link.get("quote", ""),
            })

    # finalise evidence nodes:
    #   degree  = distinct propositions touched at all (drives node size), and
    #   carries = distinct propositions it actually supports/undermines — the
    #             real "load-bearing" signal (one email proving four allegations
    #             is critical; a witness mentioned in passing is not).
    for node in ev_nodes.values():
        node["degree"] = len(node.pop("_props"))
        node["carries"] = len(node.pop("_probative"))

    # flag propositions whose evidence contradicts itself (support AND adverse).
    for node in prop_nodes.values():
        node["contradicted"] = node["supports"] > 0 and node["undermines"] > 0

    nodes = list(prop_nodes.values()) + list(ev_nodes.values())
    analytics = _annotate_analytics(prop_nodes, ev_nodes, edges)

    # Proposition dependency layer (optional) + circular-reasoning detection.
    if dependencies is None:
        dependencies = load_dependencies()
    dep_info = _add_dependencies(prop_nodes, edges, dependencies)

    stats = {
        "propositions": len(prop_nodes),
        "evidence": len(ev_nodes),
        "edges": len(edges),
        "supports": sum(1 for e in edges if e["relation"] == "SUPPORTS"),
        "undermines": sum(1 for e in edges if e["relation"] == "UNDERMINES"),
        "neutral": sum(1 for e in edges if e["relation"] == "NEUTRAL"),
        "load_bearing": sorted(
            (
                {"id": n["id"], "witness": n["witness"], "degree": n["carries"],
                 "supports": n["supports"], "undermines": n["undermines"]}
                for n in ev_nodes.values()
                if n["carries"] > 0          # only genuinely probative evidence
            ),
            key=lambda d: (-d["degree"], -d["supports"]),
        )[:10],
        **analytics,
        **dep_info,
    }
    return {"nodes": nodes, "edges": edges, "stats": stats}


def _add_dependencies(prop_nodes: dict, edges: list, dependencies: list) -> dict:
    """Append DEPENDS_ON edges (proposition -> premise) and detect circular
    reasoning via strongly-connected components. Mutates edges/prop_nodes;
    returns summary stats."""
    for n in prop_nodes.values():
        n["depends_on"] = []
        n["in_cycle"] = False

    out_adj: dict[str, set] = {pid: set() for pid in prop_nodes}
    n_dep = 0
    for d in dependencies or []:
        a, b = d.get("from"), d.get("to")
        if a in prop_nodes and b in prop_nodes and a != b:
            edges.append({"source": a, "target": b, "relation": "DEPENDS_ON",
                          "confidence": 1.0, "rationale": d.get("rationale", ""),
                          "quote": ""})
            out_adj[a].add(b)
            prop_nodes[a]["depends_on"].append(b)
            n_dep += 1

    cycles = [sorted(c) for c in _strongly_connected_components(list(prop_nodes), out_adj)
              if len(c) > 1]
    for comp in cycles:
        for pid in comp:
            prop_nodes[pid]["in_cycle"] = True

    return {
        "dependencies": n_dep,
        "circular_reasoning": [
            {"members": comp,
             "texts": [_short(prop_nodes[p]["text"], 60) for p in comp]}
            for comp in cycles
        ],
    }


def _annotate_analytics(prop_nodes: dict, ev_nodes: dict, edges: list) -> dict:
    """Run PageRank, community detection and articulation-point / single-point-
    of-failure analysis over the probative subgraph and write the results onto
    the node dicts (mutating them). Returns the summary stats."""
    all_nodes = {**prop_nodes, **ev_nodes}
    ids = set(all_nodes)
    adj = _probative_adjacency(ids, edges)
    active = [x for x in ids if adj[x]]    # nodes with ≥1 probative edge

    pr = _pagerank(active, adj)
    aps = _articulation_points(active, adj)

    # Community detection on an augmented graph: propositions rarely share an
    # exact evidence item, but ones argued through the same witness belong to the
    # same factual issue — so we also link propositions that share a witness.
    # (PageRank and articulation stay on the pure support graph above.)
    comm_adj = {x: set(adj[x]) for x in ids}
    witness_props: dict[str, set] = defaultdict(set)
    for e in edges:
        if e["relation"] == "NEUTRAL":
            continue
        ev = ev_nodes.get(e["source"])
        if ev and ev.get("witness"):
            witness_props[ev["witness"]].add(e["target"])
    for props in witness_props.values():
        for a in props:
            for b in props:
                if a != b:
                    comm_adj[a].add(b)
                    comm_adj[b].add(a)
    comm_active = [x for x in ids if comm_adj[x]]
    raw_comm = _label_propagation(comm_active, comm_adj)

    # relabel communities 0..k-1 by descending size (stable, presentable ids)
    sizes = Counter(raw_comm.values())
    order = [c for c, _ in sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))]
    remap = {c: i for i, c in enumerate(order)}

    for x, node in all_nodes.items():
        node["pagerank"] = round(pr.get(x, 0.0), 4)
        node["community"] = remap[raw_comm[x]] if x in raw_comm else -1
        node["articulation"] = x in aps

    # Domain single-point-of-failure: a proposition whose entire support rests on
    # one witness collapses if that witness is discredited. Map evidence/witness
    # -> the propositions for which it is the sole support.
    supporters_by_prop: dict[str, list] = {}
    for e in edges:
        if e["relation"] == "SUPPORTS":
            supporters_by_prop.setdefault(e["target"], []).append(e["source"])

    sole_evidence: dict[str, list] = {}    # evidence_id -> [pids it solely supports]
    for pid, evids in supporters_by_prop.items():
        prop = prop_nodes.get(pid)
        if not prop:
            continue
        witnesses = {ev_nodes[ev]["witness"] for ev in evids if ev in ev_nodes}
        witnesses.discard("")
        prop["support_witnesses"] = len(witnesses)
        prop["sole_witness"] = next(iter(witnesses)) if len(witnesses) == 1 else ""
        if len(set(evids)) == 1:           # exactly one evidence item supports it
            sole_evidence.setdefault(evids[0], []).append(pid)

    for ev_id, pids in sole_evidence.items():
        ev_nodes[ev_id]["spof_props"] = pids

    spof = sorted(
        (
            {"id": ev_id, "witness": ev_nodes[ev_id]["witness"], "props": pids,
             "n": len(pids)}
            for ev_id, pids in sole_evidence.items()
        ),
        key=lambda d: -d["n"],
    )

    # human-readable community summaries, labelled by their top-PageRank proposition
    communities = []
    members: dict[int, list] = {}
    for x in comm_active:
        members.setdefault(remap[raw_comm[x]], []).append(x)
    for cid, mem in sorted(members.items()):
        props = [m for m in mem if m in prop_nodes]
        if not props:
            continue
        lead = max(props, key=lambda m: pr.get(m, 0.0))
        communities.append({
            "id": cid,
            "size": len(mem),
            "propositions": len(props),
            "evidence": len(mem) - len(props),
            "label": _short(prop_nodes[lead]["text"], 56),
            "lead": lead,
        })
    communities.sort(key=lambda c: -c["size"])

    priority = sorted(
        ({"id": x, "kind": all_nodes[x]["kind"], "pagerank": round(pr[x], 4)}
         for x in active),
        key=lambda d: -d["pagerank"],
    )[:10]

    return {
        "communities": communities,
        "single_points_of_failure": spof[:10],
        "articulation_count": len(aps),
        "priority": priority,
    }


# ---------------------------------------------------------------------------
# Cypher emitter — a standalone script you can paste into Neo4j Browser even
# without the Python driver. neo4j_load.py does the same via parameters.
# ---------------------------------------------------------------------------
def _lit(value) -> str:
    """A Cypher literal. JSON string escaping is compatible with Cypher's
    double-quoted strings; numbers/bools/None map cleanly."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def to_cypher(graph: dict) -> str:
    lines = [
        "// Pleading-to-proof case graph — generated by graph_export.py",
        "// Paste into Neo4j Browser, or load with neo4j_load.py.",
        "CREATE CONSTRAINT prop_id IF NOT EXISTS FOR (p:Proposition) REQUIRE p.id IS UNIQUE;",
        "CREATE CONSTRAINT ev_id   IF NOT EXISTS FOR (e:Evidence)    REQUIRE e.id IS UNIQUE;",
        "",
    ]
    for n in graph["nodes"]:
        if n["kind"] == "proposition":
            lines.append(
                "MERGE (p:Proposition {id: %s}) SET p.text=%s, p.type=%s, "
                "p.party=%s, p.status=%s, p.contradicted=%s, p.pagerank=%s, "
                "p.community=%s, p.articulation=%s;" % (
                    _lit(n["id"]), _lit(n["text"]), _lit(n["type"]),
                    _lit(n["party"]), _lit(n["status"]), _lit(n["contradicted"]),
                    _lit(n.get("pagerank", 0)), _lit(n.get("community", -1)),
                    _lit(n.get("articulation", False)),
                )
            )
        else:
            lines.append(
                "MERGE (e:Evidence {id: %s}) SET e.witness=%s, e.doc_id=%s, "
                "e.quote=%s, e.degree=%s, e.carries=%s, e.pagerank=%s, "
                "e.community=%s, e.articulation=%s;" % (
                    _lit(n["id"]), _lit(n["witness"]), _lit(n["doc_id"]),
                    _lit(n["quote"]), _lit(n["degree"]), _lit(n.get("carries", 0)),
                    _lit(n.get("pagerank", 0)), _lit(n.get("community", -1)),
                    _lit(n.get("articulation", False)),
                )
            )
    lines.append("")
    for e in graph["edges"]:
        if e["relation"] == "DEPENDS_ON":   # proposition -> premise proposition
            lines.append(
                "MATCH (a:Proposition {id: %s}), (b:Proposition {id: %s}) "
                "MERGE (a)-[r:DEPENDS_ON]->(b) SET r.rationale=%s;" % (
                    _lit(e["source"]), _lit(e["target"]), _lit(e["rationale"]),
                )
            )
        else:                               # evidence -> proposition
            lines.append(
                "MATCH (e:Evidence {id: %s}), (p:Proposition {id: %s}) "
                "MERGE (e)-[r:%s]->(p) SET r.confidence=%s, r.rationale=%s, r.quote=%s;" % (
                    _lit(e["source"]), _lit(e["target"]), e["relation"],
                    _lit(e["confidence"]), _lit(e["rationale"]), _lit(e["quote"]),
                )
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# IO + CLI
# ---------------------------------------------------------------------------
def load_matrix(path: str | os.PathLike | None = None) -> list:
    """Load matrix.json, preferring live pipeline output, then the snapshot."""
    candidates = [path] if path else [_DEFAULT_MATRIX, _FALLBACK_MATRIX]
    for c in candidates:
        if c and pathlib.Path(c).exists():
            with open(c, encoding="utf-8") as fh:
                return json.load(fh)
    raise SystemExit(
        "matrix.json not found. Run the pipeline first (extract.py -> "
        "build_matrix.py), or pass an explicit path."
    )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Export the proof matrix as a graph.")
    ap.add_argument("--matrix", help="path to matrix.json (default: out/ then snapshot)")
    ap.add_argument("--out-dir", default=str(_HERE / "out"))
    ap.add_argument("--no-neutral", action="store_true",
                    help="drop neutral (on-topic but non-probative) edges")
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--snapshot", action="store_true", default=True,
                    help="also copy graph.json into case_ui/data/ for the app")
    args = ap.parse_args()

    matrix = load_matrix(args.matrix)
    graph = build_graph(matrix, include_neutral=not args.no_neutral,
                        min_confidence=args.min_confidence)

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "graph.cypher").write_text(to_cypher(graph), encoding="utf-8")

    if args.snapshot:
        snap = _HERE / "case_ui" / "data" / "graph.json"
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    s = graph["stats"]
    print(f"Graph: {s['propositions']} propositions + {s['evidence']} evidence "
          f"nodes, {s['edges']} edges "
          f"({s['supports']} supports, {s['undermines']} undermines, "
          f"{s['neutral']} neutral).")
    print(f"Wrote {out_dir / 'graph.json'} and {out_dir / 'graph.cypher'}.")
    if args.snapshot:
        print(f"Snapshotted to {snap}.")
    if s["load_bearing"]:
        top = s["load_bearing"][0]
        print(f"Most load-bearing evidence: {top['id']} "
              f"({top['witness'] or 'unknown witness'}) — touches "
              f"{top['degree']} propositions.")


if __name__ == "__main__":
    main()
