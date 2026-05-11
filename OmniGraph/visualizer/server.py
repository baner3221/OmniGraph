"""
OmniGraph Graph Visualizer — FastAPI Backend

Serves the interactive graph visualization UI and provides
REST endpoints for on-demand Neo4j subgraph queries.

All queries return subgraphs (not the full graph) for performance.

Usage:
    cd OmniGraph
    source .venv/bin/activate
    python visualizer/server.py
    # Open http://localhost:8000
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from neo4j import GraphDatabase

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

app = FastAPI(title="OmniGraph Visualizer", version="1.0.0")

# ---------------------------------------------------------------------------
# Neo4j connection
# ---------------------------------------------------------------------------

_driver = None


def get_driver():
    global _driver
    if _driver is None:
        config_path = PROJECT_ROOT / "configs" / "db_config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f).get("neo4j", {})
        _driver = GraphDatabase.driver(
            cfg.get("uri", "bolt://localhost:7687"),
            auth=(cfg.get("user", "neo4j"), cfg.get("password", "omnigraph_password")),
        )
        _driver.verify_connectivity()
    return _driver


def query_neo4j(cypher: str, params: dict = None) -> list[dict]:
    driver = get_driver()
    with driver.session() as session:
        result = session.run(cypher, params or {})
        return [dict(r) for r in result]


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def graph_stats():
    """Overall graph statistics."""
    nodes = query_neo4j("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt")
    edges = query_neo4j(
        "MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS cnt"
    )
    return {"nodes": nodes, "edges": edges}


@app.get("/api/namespaces")
def list_namespaces():
    """List all namespaces/packages with class counts."""
    rows = query_neo4j("""
        MATCH (c:Class)
        WITH COALESCE(c.namespace, 'global') AS ns, count(c) AS cnt
        RETURN ns, cnt ORDER BY cnt DESC
    """)
    return rows


@app.get("/api/classes")
def list_classes(namespace: str = Query(default="", description="Namespace filter")):
    """List classes in a namespace."""
    if namespace and namespace != "global":
        rows = query_neo4j(
            "MATCH (c:Class) WHERE c.namespace = $ns RETURN c ORDER BY c.name",
            {"ns": namespace},
        )
    else:
        rows = query_neo4j(
            "MATCH (c:Class) WHERE c.namespace IS NULL OR c.namespace = '' RETURN c ORDER BY c.name LIMIT 200"
        )
    return [_node_to_dict(r["c"]) for r in rows]


@app.get("/api/methods")
def list_methods(class_usr: str = Query(..., description="Class USR")):
    """List methods defined by a class."""
    rows = query_neo4j("""
        MATCH (c {usr: $usr})-[:DEFINES]->(f:Function)
        RETURN f ORDER BY f.line
    """, {"usr": class_usr})
    return [_node_to_dict(r["f"]) for r in rows]


@app.get("/api/neighbors")
def get_neighbors(
    usr: str = Query(..., description="Node USR"),
    hops: int = Query(default=1, ge=1, le=5, description="Hop depth"),
):
    """Get N-hop neighborhood of a node — returns Cytoscape-format elements."""
    rows = query_neo4j("""
        MATCH path = (center {usr: $usr})-[*1..{hops}]-(neighbor)
        WITH center, neighbor, relationships(path) AS rels
        UNWIND rels AS r
        WITH collect(DISTINCT center) + collect(DISTINCT neighbor) AS allNodes,
             collect(DISTINCT {{
                 source: startNode(r).usr,
                 target: endNode(r).usr,
                 rel: type(r),
                 file: r.file,
                 line: r.line
             }}) AS allEdges
        RETURN allNodes, allEdges
    """.replace("{hops}", str(hops)), {"usr": usr})

    if not rows:
        # Fallback: return just the node itself
        single = query_neo4j("MATCH (n {usr: $usr}) RETURN n", {"usr": usr})
        if single:
            return {"nodes": [_node_to_cyto(single[0]["n"])], "edges": []}
        return {"nodes": [], "edges": []}

    nodes, edges = set(), []
    for row in rows:
        for n in row.get("allNodes", []):
            nodes.add(_node_to_cyto_tuple(n))
        for e in row.get("allEdges", []):
            edges.append(e)

    return {
        "nodes": [_tuple_to_cyto(t) for t in nodes],
        "edges": [_edge_to_cyto(e) for e in edges],
    }


@app.get("/api/callchain")
def get_callchain(
    usr: str = Query(..., description="Function USR"),
    direction: str = Query(default="both", description="callers|callees|both"),
    depth: int = Query(default=3, ge=1, le=10, description="Chain depth"),
):
    """Get caller/callee chain for a function."""
    nodes_set, edges_list = set(), []

    # Get the center node
    center = query_neo4j("MATCH (n {usr: $usr}) RETURN n", {"usr": usr})
    if center:
        nodes_set.add(_node_to_cyto_tuple(center[0]["n"]))

    if direction in ("callers", "both"):
        rows = query_neo4j("""
            MATCH path = (caller)-[:CALLS*1..{depth}]->(target {{usr: $usr}})
            UNWIND nodes(path) AS n
            UNWIND relationships(path) AS r
            RETURN collect(DISTINCT n) AS nodes,
                   collect(DISTINCT {{source: startNode(r).usr, target: endNode(r).usr, rel: 'CALLS'}}) AS edges
        """.replace("{depth}", str(depth)), {"usr": usr})
        for row in rows:
            for n in row.get("nodes", []):
                nodes_set.add(_node_to_cyto_tuple(n))
            edges_list.extend(row.get("edges", []))

    if direction in ("callees", "both"):
        rows = query_neo4j("""
            MATCH path = (source {{usr: $usr}})-[:CALLS*1..{depth}]->(callee)
            UNWIND nodes(path) AS n
            UNWIND relationships(path) AS r
            RETURN collect(DISTINCT n) AS nodes,
                   collect(DISTINCT {{source: startNode(r).usr, target: endNode(r).usr, rel: 'CALLS'}}) AS edges
        """.replace("{depth}", str(depth)), {"usr": usr})
        for row in rows:
            for n in row.get("nodes", []):
                nodes_set.add(_node_to_cyto_tuple(n))
            edges_list.extend(row.get("edges", []))

    return {
        "nodes": [_tuple_to_cyto(t) for t in nodes_set],
        "edges": [_edge_to_cyto(e) for e in edges_list],
    }


@app.get("/api/inheritance")
def get_inheritance(usr: str = Query(..., description="Class USR")):
    """Get full inheritance tree for a class (up and down)."""
    rows = query_neo4j("""
        MATCH path = (child)-[:INHERITS_FROM*0..10]->(ancestor)
        WHERE child.usr = $usr OR ancestor.usr = $usr
        UNWIND nodes(path) AS n
        UNWIND relationships(path) AS r
        RETURN collect(DISTINCT n) AS nodes,
               collect(DISTINCT {source: startNode(r).usr, target: endNode(r).usr, rel: 'INHERITS_FROM'}) AS edges
    """, {"usr": usr})

    nodes_set, edges_list = set(), []
    for row in rows:
        for n in row.get("nodes", []):
            nodes_set.add(_node_to_cyto_tuple(n))
        edges_list.extend(row.get("edges", []))

    return {
        "nodes": [_tuple_to_cyto(t) for t in nodes_set],
        "edges": [_edge_to_cyto(e) for e in edges_list],
    }


@app.get("/api/search")
def search_nodes(
    q: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(default=30, ge=1, le=100),
):
    """Search nodes by name or FQN (case-insensitive contains)."""
    rows = query_neo4j("""
        MATCH (n)
        WHERE toLower(n.name) CONTAINS toLower($q)
           OR toLower(n.fqn) CONTAINS toLower($q)
        RETURN n, labels(n)[0] AS label
        LIMIT $limit
    """, {"q": q, "limit": limit})
    return [
        {**_node_to_dict(r["n"]), "label": r["label"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_to_dict(node) -> dict:
    return dict(node)


def _node_to_cyto(node) -> dict:
    d = dict(node)
    label_type = "Function" if d.get("signature") is not None or d.get("kind") in ("function", "method", "constructor", "lambda") else "Class"
    return {
        "data": {
            "id": d.get("usr", ""),
            "label": d.get("name", ""),
            "fqn": d.get("fqn", ""),
            "file": d.get("file", ""),
            "line": d.get("line", 0),
            "language": d.get("language", ""),
            "kind": d.get("kind", ""),
            "type": label_type,
            "namespace": d.get("namespace", ""),
            "parent_fqn": d.get("parent_fqn", ""),
        }
    }


def _node_to_cyto_tuple(node) -> tuple:
    d = dict(node)
    return (
        d.get("usr", ""),
        d.get("name", ""),
        d.get("fqn", ""),
        d.get("file", ""),
        d.get("line", 0),
        d.get("language", ""),
        d.get("kind", ""),
        d.get("namespace", ""),
        d.get("parent_fqn", ""),
    )


def _tuple_to_cyto(t: tuple) -> dict:
    usr, name, fqn, file, line, lang, kind, ns, parent = t
    label_type = "Function" if kind in ("function", "method", "constructor", "lambda") else "Class"
    return {
        "data": {
            "id": usr, "label": name, "fqn": fqn,
            "file": file, "line": line, "language": lang,
            "kind": kind, "type": label_type,
            "namespace": ns, "parent_fqn": parent,
        }
    }


def _edge_to_cyto(e: dict) -> dict:
    return {
        "data": {
            "id": f"{e['source']}-{e['rel']}-{e['target']}",
            "source": e["source"],
            "target": e["target"],
            "rel": e.get("rel", "CALLS"),
            "file": e.get("file", ""),
            "line": e.get("line", 0),
        }
    }


# ---------------------------------------------------------------------------
# Static files + entry point
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("Starting OmniGraph Visualizer at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
