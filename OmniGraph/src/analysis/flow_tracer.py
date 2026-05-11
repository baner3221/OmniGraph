"""
OmniGraph Flow Tracer

Recursive call-chain logic for execution narratives.
Traverses CALLS edges in Neo4j to build caller/callee chains.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)


@dataclass
class CallChainNode:
    """A single node in a call chain."""
    usr: str
    name: str
    fqn: str
    file: str
    line: int
    depth: int


@dataclass
class CallChain:
    """A complete call chain (path through the graph)."""
    root_usr: str
    direction: str  # "callers" or "callees"
    nodes: list[CallChainNode] = field(default_factory=list)


class FlowTracer:
    """
    Recursive call-chain tracer using Neo4j graph queries.

    Provides upstream (callers) and downstream (callees) traversal
    with configurable depth limits.
    """

    def __init__(self, driver, database: str = "neo4j"):
        self.driver = driver
        self.database = database

    def trace_callers(self, usr: str, depth: int = 5) -> list[CallChain]:
        """Walk CALLS edges backwards: who calls this function?"""
        query = """
        MATCH path = (caller)-[:CALLS*1..{depth}]->(target {{usr: $usr}})
        RETURN [n IN nodes(path) | {{
            usr: n.usr, name: n.name, fqn: n.fqn,
            file: n.file, line: n.line
        }}] AS chain
        LIMIT 100
        """.replace("{depth}", str(depth))

        chains = []
        with self.driver.session(database=self.database) as session:
            result = session.run(query, usr=usr)
            for record in result:
                chain = CallChain(root_usr=usr, direction="callers")
                for i, node_data in enumerate(record["chain"]):
                    chain.nodes.append(CallChainNode(
                        depth=i, **{k: v for k, v in node_data.items() if v is not None}
                    ))
                chains.append(chain)
        return chains

    def trace_callees(self, usr: str, depth: int = 5) -> list[CallChain]:
        """Walk CALLS edges forward: what does this function call?"""
        query = """
        MATCH path = (source {{usr: $usr}})-[:CALLS*1..{depth}]->(callee)
        RETURN [n IN nodes(path) | {{
            usr: n.usr, name: n.name, fqn: n.fqn,
            file: n.file, line: n.line
        }}] AS chain
        LIMIT 100
        """.replace("{depth}", str(depth))

        chains = []
        with self.driver.session(database=self.database) as session:
            result = session.run(query, usr=usr)
            for record in result:
                chain = CallChain(root_usr=usr, direction="callees")
                for i, node_data in enumerate(record["chain"]):
                    chain.nodes.append(CallChainNode(
                        depth=i, **{k: v for k, v in node_data.items() if v is not None}
                    ))
                chains.append(chain)
        return chains

    def get_narrative(self, usr: str, depth: int = 3) -> str:
        """Generate a human-readable call chain narrative."""
        callers = self.trace_callers(usr, depth)
        callees = self.trace_callees(usr, depth)

        # Get target info
        target_info = self._get_node_info(usr)
        target_name = target_info.get("fqn", usr) if target_info else usr

        lines = [f"=== Call Flow Narrative for {target_name} ===\n"]

        if callers:
            lines.append("UPSTREAM (Who calls this?):")
            for chain in callers[:10]:
                path_str = " → ".join(n.name for n in chain.nodes)
                lines.append(f"  {path_str}")
        else:
            lines.append("UPSTREAM: No callers found (entry point or unused)")

        lines.append("")

        if callees:
            lines.append("DOWNSTREAM (What does this call?):")
            for chain in callees[:10]:
                path_str = " → ".join(n.name for n in chain.nodes)
                lines.append(f"  {path_str}")
        else:
            lines.append("DOWNSTREAM: No callees found (leaf function)")

        return "\n".join(lines)

    def _get_node_info(self, usr: str) -> Optional[dict]:
        """Fetch a single node's info by USR."""
        query = "MATCH (n {usr: $usr}) RETURN n LIMIT 1"
        with self.driver.session(database=self.database) as session:
            result = session.run(query, usr=usr)
            record = result.single()
            if record:
                return dict(record["n"])
        return None
