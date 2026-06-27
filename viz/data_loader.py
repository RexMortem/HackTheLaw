"""Load the project's real case data for the animations.

Every scene builds itself from these helpers, so the video shows the *actual*
case in case_ui/data/ and re-renders automatically when the pipeline output
changes. Nothing here is hand-authored fiction.

Files consumed (committed snapshots, always present):
    case_ui/data/matrix.json   four-bucket proof matrix (list of propositions)
    case_ui/data/graph.json    {nodes, edges, stats} case graph
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "case_ui" / "data"

# Trial-readiness weights — must match case_ui/app.py.
_WEIGHT = {"supported": 1.0, "contested": 0.5, "undermined": 0.0, "missing": 0.0}


def _norm(status: str) -> str:
    return (status or "missing").strip().lower()


# ---------------------------------------------------------------------------
# raw loaders
# ---------------------------------------------------------------------------
def load_matrix() -> list[dict]:
    with open(DATA_DIR / "matrix.json", encoding="utf-8") as f:
        return json.load(f)


def load_graph() -> dict:
    with open(DATA_DIR / "graph.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# matrix-derived figures
# ---------------------------------------------------------------------------
def status_counts(matrix: list[dict] | None = None) -> dict[str, int]:
    matrix = matrix if matrix is not None else load_matrix()
    counts = {k: 0 for k in _WEIGHT}
    for item in matrix:
        counts[_norm(item.get("status"))] = counts.get(_norm(item.get("status")), 0) + 1
    return counts


def trial_readiness(matrix: list[dict] | None = None) -> float:
    """Single header score in [0, 1]. supported=1, contested=0.5, else 0."""
    matrix = matrix if matrix is not None else load_matrix()
    if not matrix:
        return 0.0
    total = sum(_WEIGHT.get(_norm(x.get("status")), 0.0) for x in matrix)
    return total / len(matrix)


def proposition_links(pid: str, matrix: list[dict] | None = None) -> list[dict]:
    """For one proposition id, return its evidence links normalised to
    {evidence_id, relation in {support, undermine, neutral}, confidence, quote}.
    """
    matrix = matrix if matrix is not None else load_matrix()
    rel_map = {"supportive": "support", "adverse": "undermine", "neutral": "neutral"}
    for item in matrix:
        if item.get("proposition", {}).get("id") == pid:
            out = []
            for lk in item.get("links", []):
                out.append(
                    {
                        "evidence_id": lk.get("evidence_id"),
                        "relation": rel_map.get(lk.get("relation"), lk.get("relation")),
                        "confidence": lk.get("confidence", 0.0),
                        "quote": lk.get("quote", ""),
                    }
                )
            return out
    return []


def proposition_text(pid: str, matrix: list[dict] | None = None) -> str:
    matrix = matrix if matrix is not None else load_matrix()
    for item in matrix:
        prop = item.get("proposition", {})
        if prop.get("id") == pid:
            return prop.get("text", pid)
    return pid


# ---------------------------------------------------------------------------
# graph helpers
# ---------------------------------------------------------------------------
def nodes_by_kind(graph: dict | None = None, kind: str = "proposition") -> list[dict]:
    graph = graph if graph is not None else load_graph()
    return [n for n in graph["nodes"] if n.get("kind") == kind]


def node_index(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def signal_edges(graph: dict | None = None) -> list[dict]:
    """SUPPORTS / UNDERMINES edges only — the 849 NEUTRAL edges are visual
    noise for a wide shot, so drop them by default."""
    graph = graph if graph is not None else load_graph()
    return [e for e in graph["edges"] if e.get("relation") in ("SUPPORTS", "UNDERMINES")]


def best_articulation_proposition(graph: dict | None = None) -> dict | None:
    """The highest-PageRank proposition flagged as an articulation point — the
    most load-bearing 'remove this and the case fragments' node. The punchline."""
    graph = graph if graph is not None else load_graph()
    arts = [n for n in graph["nodes"]
            if n.get("kind") == "proposition" and n.get("articulation")]
    if not arts:
        arts = [n for n in graph["nodes"] if n.get("kind") == "proposition"]
    return max(arts, key=lambda n: n.get("pagerank", 0.0)) if arts else None


def ego_subgraph(center_id: str, graph: dict | None = None,
                 hops: int = 2, cap: int = 26) -> tuple[list[str], list[tuple]]:
    """Connected neighbourhood around a node, for the fragmentation scene.

    Returns (node_ids, edge_pairs) using only SUPPORTS/UNDERMINES edges so the
    'remove the node, watch it split' effect is true to the data and legible.
    """
    graph = graph if graph is not None else load_graph()
    adj: dict[str, set[str]] = {}
    for e in signal_edges(graph):
        a, b = e["source"], e["target"]
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    # BFS in distance order, so a cap drops the *farthest* nodes and keeps the
    # neighbourhood connected through the centre.
    order = [center_id]
    seen = {center_id}
    frontier = [center_id]
    for _ in range(hops):
        nxt = []
        for n in frontier:
            for m in sorted(adj.get(n, ())):
                if m not in seen:
                    seen.add(m)
                    order.append(m)
                    nxt.append(m)
        frontier = nxt
    kept = set(order[:cap])

    pairs = [(e["source"], e["target"]) for e in signal_edges(graph)
             if e["source"] in kept and e["target"] in kept]

    # Restrict to the connected component containing the centre, so the intact
    # subgraph is genuinely ONE piece before the articulation node is removed.
    comp_of_center = next((c for c in connected_components(list(kept), pairs)
                           if center_id in c), {center_id})
    pairs = [(a, b) for (a, b) in pairs
             if a in comp_of_center and b in comp_of_center]
    return list(comp_of_center), pairs


def connected_components(node_ids: list[str], edge_pairs: list[tuple]) -> list[set]:
    """Union-find components — used to prove the graph really fragments when an
    articulation node is removed."""
    parent = {n: n for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edge_pairs:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    comps: dict[str, set] = {}
    for n in node_ids:
        comps.setdefault(find(n), set()).add(n)
    return list(comps.values())


# ---------------------------------------------------------------------------
# layout: a small vectorised Fruchterman-Reingold spring solver (numpy only,
# so we inherit no extra dependency). Gives the 3b1b "graph blooms open" look.
# ---------------------------------------------------------------------------
def spring_layout(node_ids: list[str], edge_pairs: list[tuple],
                  iterations: int = 160, seed: int = 7,
                  scale: tuple[float, float] = (11.0, 5.6)) -> dict[str, np.ndarray]:
    n = len(node_ids)
    if n == 0:
        return {}
    idx = {nid: i for i, nid in enumerate(node_ids)}
    rng = np.random.default_rng(seed)
    pos = rng.standard_normal((n, 2)) * 0.5
    E = np.array([(idx[a], idx[b]) for a, b in edge_pairs
                  if a in idx and b in idx], dtype=int)
    k = math.sqrt(1.0 / n)
    t = 0.1
    for _ in range(iterations):
        diff = pos[:, None, :] - pos[None, :, :]          # n x n x 2
        dist = np.linalg.norm(diff, axis=2) + 1e-9
        rep = (k * k) / dist
        np.fill_diagonal(rep, 0.0)
        disp = np.einsum("ijk,ij->ik", diff / dist[:, :, None], rep)
        if len(E):
            d = pos[E[:, 0]] - pos[E[:, 1]]
            dl = np.linalg.norm(d, axis=1) + 1e-9
            att = (dl * dl) / k
            f = (d / dl[:, None]) * att[:, None]
            np.add.at(disp, E[:, 0], -f)
            np.add.at(disp, E[:, 1], f)
        length = np.linalg.norm(disp, axis=1) + 1e-9
        pos += (disp / length[:, None]) * np.minimum(length, t)[:, None]
        t *= 0.97
    pos -= pos.mean(axis=0)
    span = np.max(np.abs(pos), axis=0) + 1e-9
    pos = pos / span * np.array(scale)
    return {nid: pos[idx[nid]] for nid in node_ids}


def truncate(text: str, n: int = 48) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
