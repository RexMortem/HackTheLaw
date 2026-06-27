"""
neo4j_load — push the pleading-to-proof case graph into Neo4j.

Reads the graph produced by graph_export.py and loads it into a Neo4j database
(Aura cloud or local) using the official driver and idempotent MERGE, so re-runs
update in place rather than duplicating.

Setup
-----
    pip install neo4j
    # Neo4j Aura: create a free/credit instance, then put the connection
    # details in .env (the same .env caselib already loads):
    #   NEO4J_URI=neo4j+s://xxxx.databases.neo4j.io
    #   NEO4J_USERNAME=neo4j
    #   NEO4J_PASSWORD=...

Run
---
    python graph_export.py            # first, to (re)build out/graph.json
    python neo4j_load.py              # load nodes + relationships
    python neo4j_load.py --analytics  # ...and run GDS in-database (needs the GDS plugin)

The pure-Python analytics in graph_export.py (PageRank, communities, articulation
points) already drive the web app with no database. `--analytics` runs the
equivalent Neo4j Graph Data Science procedures *in* the database and writes the
results back onto the nodes, so the same story is queryable in Browser/Bloom and
scales past what one process can hold.

After loading, explore in Neo4j Browser / Bloom, e.g.:

    // load-bearing evidence: what proves the most
    MATCH (e:Evidence)-[:SUPPORTS]->(p:Proposition)
    RETURN e.id, e.witness, count(p) AS proves ORDER BY proves DESC LIMIT 10;

    // case-collapse: propositions that rely on a single witness
    MATCH (p:Proposition)<-[:SUPPORTS]-(e:Evidence)
    WITH p, collect(DISTINCT e.witness) AS witnesses
    WHERE size(witnesses) = 1 RETURN p.id, p.status, witnesses;

    // contradictions: evidence that cuts both ways on one proposition
    MATCH (p:Proposition)<-[:SUPPORTS]-(a), (p)<-[:UNDERMINES]-(b)
    RETURN p.id, p.text, a.id AS supports, b.id AS undermines;

    // after --analytics: highest-priority propositions and their issue cluster
    MATCH (p:Proposition)
    RETURN p.id, p.community, p.pagerank, p.betweenness, p.status
    ORDER BY p.pagerank DESC LIMIT 10;

    // single points of failure surfaced by GDS articulation points
    MATCH (n) WHERE n.articulation = true
    RETURN labels(n)[0] AS kind, n.id, coalesce(n.witness, n.text) AS detail;
"""
from __future__ import annotations

import json
import os
import pathlib

# Reuse caselib's tiny .env loader so NEO4J_* land in the environment.
try:
    import caselib
    caselib.load_dotenv()
except Exception:
    pass

_HERE = pathlib.Path(__file__).parent
_GRAPH_JSON = _HERE / "out" / "graph.json"


def _load_graph() -> dict:
    if not _GRAPH_JSON.exists():
        raise SystemExit(
            f"{_GRAPH_JSON} not found. Run `python graph_export.py` first.")
    with open(_GRAPH_JSON, encoding="utf-8") as fh:
        return json.load(fh)


# Batched, parameterised Cypher — UNWIND a list of rows per statement so the
# whole graph loads in a handful of round-trips, not one per node.
_CONSTRAINTS = [
    "CREATE CONSTRAINT prop_id IF NOT EXISTS "
    "FOR (p:Proposition) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT ev_id IF NOT EXISTS "
    "FOR (e:Evidence) REQUIRE e.id IS UNIQUE",
]

_MERGE_PROPS = """
UNWIND $rows AS row
MERGE (p:Proposition {id: row.id})
SET p.text = row.text, p.type = row.type, p.party = row.party,
    p.status = row.status, p.contradicted = row.contradicted
"""

_MERGE_EVIDENCE = """
UNWIND $rows AS row
MERGE (e:Evidence {id: row.id})
SET e.witness = row.witness, e.doc_id = row.doc_id,
    e.quote = row.quote, e.degree = row.degree
"""

# One statement per relationship type (the type can't be parameterised).
_MERGE_EDGES = """
UNWIND $rows AS row
MATCH (e:Evidence {id: row.source}), (p:Proposition {id: row.target})
MERGE (e)-[r:%s]->(p)
SET r.confidence = row.confidence, r.rationale = row.rationale, r.quote = row.quote
"""


def load(graph: dict, wipe: bool = False, analytics: bool = False) -> None:
    try:
        from neo4j import GraphDatabase
    except ImportError as e:
        raise SystemExit("This loader needs the neo4j driver: pip install neo4j") from e

    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD")
    if not uri or not pwd:
        raise SystemExit(
            "Set NEO4J_URI and NEO4J_PASSWORD (and optionally NEO4J_USERNAME) "
            "in your environment or .env. See the module docstring.")

    props = [n for n in graph["nodes"] if n["kind"] == "proposition"]
    evidence = [n for n in graph["nodes"] if n["kind"] == "evidence"]
    edges_by_type: dict[str, list] = {}
    for e in graph["edges"]:
        edges_by_type.setdefault(e["relation"], []).append(e)

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            if wipe:
                print("  wiping existing Proposition/Evidence graph...")
                session.run("MATCH (n) WHERE n:Proposition OR n:Evidence "
                            "DETACH DELETE n")
            for c in _CONSTRAINTS:
                session.run(c)
            session.run(_MERGE_PROPS, rows=props)
            session.run(_MERGE_EVIDENCE, rows=evidence)
            for rel, rows in edges_by_type.items():
                session.run(_MERGE_EDGES % rel, rows=rows)
            if analytics:
                run_gds_analytics(session)
    finally:
        driver.close()

    print(f"Loaded {len(props)} propositions, {len(evidence)} evidence nodes, "
          f"{len(graph['edges'])} relationships into {uri}.")


# Graph Data Science analytics, run *inside* Neo4j (vs. the pure-Python versions
# in graph_export.py that drive the app). Requires the GDS plugin / Aura Graph
# Analytics. We project the probative support graph as UNDIRECTED, then write
# PageRank, Leiden communities, betweenness and articulation points back onto the
# nodes so they're queryable in Browser/Bloom. Leiden is preferred over Louvain:
# it guarantees well-connected communities (Louvain can return disconnected ones).
_GDS_PROJECT = """
CALL gds.graph.project(
  'caseGraph',
  ['Proposition', 'Evidence'],
  {
    SUPPORTS:   {orientation: 'UNDIRECTED'},
    UNDERMINES: {orientation: 'UNDIRECTED'}
  }
)
"""

_GDS_STEPS = [
    ("PageRank (priority)",
     "CALL gds.pageRank.write('caseGraph', {writeProperty: 'pagerank'}) "
     "YIELD nodePropertiesWritten RETURN nodePropertiesWritten"),
    ("Leiden (issue clusters)",
     "CALL gds.leiden.write('caseGraph', {writeProperty: 'community'}) "
     "YIELD communityCount RETURN communityCount"),
    ("Betweenness (cross-exam targets)",
     "CALL gds.betweenness.write('caseGraph', {writeProperty: 'betweenness'}) "
     "YIELD nodePropertiesWritten RETURN nodePropertiesWritten"),
]


def run_gds_analytics(session) -> None:
    """Project the support graph and write GDS metrics back onto the nodes.
    Best-effort: if the GDS plugin isn't installed, report and skip rather than
    fail the whole load (the app's analytics come from graph_export.py anyway)."""
    # ensure a clean projection name
    try:
        session.run("CALL gds.graph.drop('caseGraph', false) "
                    "YIELD graphName RETURN graphName")
    except Exception:
        pass
    try:
        session.run(_GDS_PROJECT)
    except Exception as exc:
        print(f"  [gds] skipped — GDS plugin not available ({type(exc).__name__}). "
              "Install Graph Data Science / enable Aura Graph Analytics to run "
              "PageRank, Leiden and articulation points in-database.")
        return

    for label, q in _GDS_STEPS:
        try:
            rec = session.run(q).single()
            print(f"  [gds] {label}: {dict(rec) if rec else 'ok'}")
        except Exception as exc:
            print(f"  [gds] {label} failed: {type(exc).__name__}: {exc}")

    # Articulation points (single points of failure): stream, then flag nodes.
    try:
        session.run("MATCH (n) WHERE n:Proposition OR n:Evidence "
                    "SET n.articulation = false")
        rec = session.run(
            "CALL gds.articulationPoints.stream('caseGraph') YIELD nodeId "
            "WITH gds.util.asNode(nodeId) AS n SET n.articulation = true "
            "RETURN count(n) AS aps"
        ).single()
        print(f"  [gds] Articulation points (single points of failure): "
              f"{rec['aps'] if rec else '?'}")
    except Exception as exc:
        print(f"  [gds] articulation points failed: {type(exc).__name__}: {exc}")

    try:
        session.run("CALL gds.graph.drop('caseGraph', false) "
                    "YIELD graphName RETURN graphName")
    except Exception:
        pass


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Load the case graph into Neo4j.")
    ap.add_argument("--wipe", action="store_true",
                    help="delete the existing Proposition/Evidence graph first")
    ap.add_argument("--analytics", action="store_true",
                    help="run GDS analytics in-database (PageRank, Leiden, "
                         "betweenness, articulation points); needs the GDS plugin")
    args = ap.parse_args()
    load(_load_graph(), wipe=args.wipe, analytics=args.analytics)


if __name__ == "__main__":
    main()
