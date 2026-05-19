"""
OmniGraph Neo4j Database Driver & Batch Ingester

Singleton Neo4j driver with batch UNWIND ingestion.
Consumes JSONL shards and loads them in batches of 10,000.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)


class Neo4jIngester:
    """
    Neo4j driver wrapper with batch UNWIND ingestion.

    Reads JSONL shards from disk and ingests nodes/edges
    in configurable batch sizes using UNWIND for throughput.
    """

    # Cypher templates
    CREATE_FUNC_NODES = """
    UNWIND $batch AS row
    MERGE (f:Function {usr: row.usr})
    SET f.name = row.name, f.fqn = row.fqn, f.file = row.file,
        f.line = row.line, f.language = row.language,
        f.kind = row.kind, f.signature = row.signature,
        f.parent_fqn = row.parent_fqn
    """

    CREATE_CLASS_NODES = """
    UNWIND $batch AS row
    MERGE (c:Class {usr: row.usr})
    SET c.fqn = row.fqn, c.name = row.name, c.namespace = row.namespace,
        c.file = row.file, c.line = row.line, c.language = row.language,
        c.kind = row.kind
    """

    CREATE_CALLS_EDGES = """
    UNWIND $batch AS row
    MATCH (src {usr: row.source_usr})
    MATCH (tgt {usr: row.target_usr})
    MERGE (src)-[:CALLS {file: row.file, line: row.line}]->(tgt)
    """

    CREATE_INHERITS_EDGES = """
    UNWIND $batch AS row
    MATCH (child {usr: row.source_usr})
    MATCH (parent {usr: row.target_usr})
    MERGE (child)-[:INHERITS_FROM {file: row.file, line: row.line}]->(parent)
    """

    CREATE_DEFINES_EDGES = """
    UNWIND $batch AS row
    MATCH (cls {usr: row.source_usr})
    MATCH (member {usr: row.target_usr})
    MERGE (cls)-[:DEFINES {file: row.file, line: row.line}]->(member)
    """

    CREATE_OVERRIDES_EDGES = """
    UNWIND $batch AS row
    MATCH (derived {usr: row.source_usr})
    MATCH (base {usr: row.target_usr})
    MERGE (derived)-[:OVERRIDES {file: row.file, line: row.line}]->(base)
    """

    CONSTRAINT_QUERIES = [
        "CREATE CONSTRAINT func_usr IF NOT EXISTS FOR (f:Function) REQUIRE f.usr IS UNIQUE",
        "CREATE CONSTRAINT class_fqn IF NOT EXISTS FOR (c:Class) REQUIRE c.fqn IS UNIQUE",
        "CREATE INDEX func_name IF NOT EXISTS FOR (f:Function) ON (f.name)",
        "CREATE INDEX class_name IF NOT EXISTS FOR (c:Class) ON (c.name)",
        "CREATE INDEX func_file IF NOT EXISTS FOR (f:Function) ON (f.file)",
        "CREATE INDEX class_file IF NOT EXISTS FOR (c:Class) ON (c.file)",
    ]

    EDGE_QUERY_MAP = {
        "CALLS": CREATE_CALLS_EDGES,
        "INHERITS_FROM": CREATE_INHERITS_EDGES,
        "DEFINES": CREATE_DEFINES_EDGES,
        "OVERRIDES": CREATE_OVERRIDES_EDGES,
    }

    def __init__(self, config_path: str = "configs/db_config.json"):
        config = self._load_config(config_path)
        neo4j_cfg = config.get("neo4j", {})

        self.uri = neo4j_cfg.get("uri", "bolt://localhost:7687")
        self.user = neo4j_cfg.get("user", "neo4j")
        self.password = neo4j_cfg.get("password", "omnigraph_password")
        self.database = neo4j_cfg.get("database", "neo4j")

        self.driver = GraphDatabase.driver(
            self.uri, auth=(self.user, self.password)
        )
        # Verify connectivity
        self.driver.verify_connectivity()
        logger.info("Connected to Neo4j at %s", self.uri)

    @staticmethod
    def _load_config(path: str) -> dict:
        with open(path) as f:
            return json.load(f)

    def ensure_constraints(self) -> None:
        """Create unique constraints and indexes."""
        with self.driver.session(database=self.database) as session:
            for query in self.CONSTRAINT_QUERIES:
                try:
                    session.run(query)
                    logger.debug("Executed: %s", query[:60])
                except Exception as e:
                    logger.warning("Constraint/index creation: %s", e)

    def ingest_shards(self, shard_dir: str, batch_size: int = 10000) -> dict:
        """
        Consume all JSONL shards and ingest into Neo4j.

        Processes nodes first, then edges (nodes must exist for edge MATCH).
        """
        stats = {"nodes_created": 0, "edges_created": 0, "errors": []}
        shard_path = Path(shard_dir)

        if not shard_path.exists():
            logger.warning("Shard directory does not exist: %s", shard_dir)
            return stats

        shard_files = sorted(shard_path.glob("*.jsonl"))
        if not shard_files:
            logger.warning("No shard files found in %s", shard_dir)
            return stats

        logger.info("Ingesting %d shard files", len(shard_files))

        # Collect all triples, separated by type
        func_nodes, class_nodes = [], []
        edge_batches: dict[str, list] = {
            "CALLS": [], "INHERITS_FROM": [], "DEFINES": [], "OVERRIDES": []
        }

        for shard_file in shard_files:
            try:
                with open(shard_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            triple = json.loads(line)
                        except json.JSONDecodeError:
                            stats["errors"].append(f"Bad JSON in {shard_file.name}")
                            continue

                        if triple.get("triple_type") == "node":
                            data = triple.get("node_data", {})
                            label = triple.get("node_label", "")
                            if label == "Function":
                                func_nodes.append(data)
                            elif label == "Class":
                                class_nodes.append(data)
                        elif triple.get("triple_type") == "edge":
                            edge_data = triple.get("edge_data", {})
                            rel = edge_data.get("relationship", "")
                            if rel in edge_batches:
                                edge_batches[rel].append(edge_data)
            except Exception as e:
                stats["errors"].append(f"Error reading {shard_file.name}: {e}")

        # Ingest nodes first
        stats["nodes_created"] += self._batch_execute(
            self.CREATE_FUNC_NODES, func_nodes, batch_size, "Function nodes"
        )
        stats["nodes_created"] += self._batch_execute(
            self.CREATE_CLASS_NODES, class_nodes, batch_size, "Class nodes"
        )

        # Then edges
        for rel_type, edges in edge_batches.items():
            if edges and rel_type in self.EDGE_QUERY_MAP:
                count = self._batch_execute(
                    self.EDGE_QUERY_MAP[rel_type], edges, batch_size, f"{rel_type} edges"
                )
                stats["edges_created"] += count

        logger.info(
            "Ingestion complete: %d nodes, %d edges",
            stats["nodes_created"], stats["edges_created"],
        )
        return stats

    def _batch_execute(
        self, query: str, data: list[dict], batch_size: int, label: str
    ) -> int:
        """Execute a UNWIND query in batches."""
        if not data:
            return 0

        total = 0
        with self.driver.session(database=self.database) as session:
            for i in range(0, len(data), batch_size):
                batch = data[i : i + batch_size]
                try:
                    session.run(query, batch=batch)
                    total += len(batch)
                    logger.debug("%s: batch %d-%d OK", label, i, i + len(batch))
                except Exception as e:
                    logger.error("%s batch failed: %s", label, e)

        logger.info("%s: %d records ingested", label, total)
        return total

    def wipe_database(self) -> None:
        """Delete all nodes and relationships."""
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (n) DETACH DELETE n")
        logger.info("Database wiped")

    def close(self) -> None:
        if self.driver:
            self.driver.close()
