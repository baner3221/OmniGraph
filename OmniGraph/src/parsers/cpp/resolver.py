"""
OmniGraph C++ Override & Polymorphism Resolver

Post-traversal pass that resolves virtual method override chains
and function pointer assignments using USR cross-referencing.

This module is called after the initial AST traversal to enrich
the graph with OVERRIDES edges that may require cross-file resolution.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from src.core.models import Edge, EdgeType, Triple
from src.utils.symbols import GlobalSymbolTable

logger = logging.getLogger(__name__)


class CppResolver:
    """
    Post-parse resolver for C++ polymorphism patterns.

    Enriches the knowledge graph with:
    1. Cross-file OVERRIDES edges (when base and derived are in different TUs)
    2. Function pointer / std::function CALLS edges
    """

    def __init__(self, gst: GlobalSymbolTable):
        self.gst = gst

    def resolve_cross_file_overrides(self, shard_path: str) -> dict:
        """
        Scan all C++ class methods and detect override relationships
        that span multiple translation units.

        This handles the case where:
        - Base class `A::foo()` is defined in `a.cpp`
        - Derived class `B::foo()` is defined in `b.cpp`
        - The initial per-file parse of `b.cpp` may have detected the override
          via `get_overridden_cursors()`, but if the base wasn't available in
          that TU, we need the GST to resolve it.

        Args:
            shard_path: Path to write additional OVERRIDES edge triples.

        Returns:
            Stats dict: {overrides_added: int, errors: list}
        """
        stats = {"overrides_added": 0, "errors": []}

        # Get all C++ classes
        classes = self.gst.get_all_classes(language="cpp")

        # Build inheritance map: child_fqn -> [parent_fqn, ...]
        # We'll need to read existing shard data for INHERITS_FROM edges
        inheritance_map: dict[str, list[str]] = {}
        for cls in classes:
            child_fqn = cls["fqn"]
            inheritance_map[child_fqn] = []

        # Scan existing shards for INHERITS_FROM edges
        shard_dir = Path(shard_path).parent
        if shard_dir.exists():
            for shard_file in shard_dir.glob("*.jsonl"):
                try:
                    with open(shard_file) as f:
                        for line in f:
                            triple = json.loads(line.strip())
                            if (
                                triple.get("triple_type") == "edge"
                                and triple.get("edge_data", {}).get("relationship") == EdgeType.INHERITS_FROM.value
                            ):
                                src_usr = triple["edge_data"]["source_usr"]
                                tgt_usr = triple["edge_data"]["target_usr"]
                                # Map USR to FQN
                                src = self.gst.lookup_usr(src_usr)
                                tgt = self.gst.lookup_usr(tgt_usr)
                                if src and tgt:
                                    child_fqn = src["fqn"]
                                    if child_fqn not in inheritance_map:
                                        inheritance_map[child_fqn] = []
                                    inheritance_map[child_fqn].append(tgt["fqn"])
                except Exception as e:
                    logger.warning("Error reading shard %s: %s", shard_file, e)

        # For each child class, compare its methods with parent methods
        with open(shard_path, "a") as out:
            for child_fqn, parent_fqns in inheritance_map.items():
                if not parent_fqns:
                    continue

                child_methods = self.gst.get_class_methods(child_fqn)

                for parent_fqn in parent_fqns:
                    parent_methods = self.gst.get_class_methods(parent_fqn)
                    parent_method_names = {
                        self._method_simple_name(m["fqn"]): m
                        for m in parent_methods
                    }

                    for child_method in child_methods:
                        child_name = self._method_simple_name(child_method["fqn"])
                        if child_name in parent_method_names:
                            parent_method = parent_method_names[child_name]

                            # Check signature compatibility
                            if self._signatures_compatible(
                                child_method.get("signature"),
                                parent_method.get("signature"),
                            ):
                                edge = Edge(
                                    source_usr=child_method["usr"],
                                    target_usr=parent_method["usr"],
                                    relationship=EdgeType.OVERRIDES,
                                    file=child_method["file"],
                                    line=child_method["line"],
                                )
                                out.write(Triple.from_edge(edge).model_dump_json() + "\n")
                                stats["overrides_added"] += 1

        logger.info(
            "Cross-file override resolution: %d overrides added",
            stats["overrides_added"],
        )
        return stats

    def resolve_virtual_dispatch(self, shard_path: str) -> dict:
        """
        Resolve virtual/polymorphic call chains by linking callers
        directly to concrete override implementations.

        For each CALLS edge where the target has OVERRIDES edges pointing
        to it, emit a VIRTUAL_DISPATCH edge from the caller to each
        concrete overrider.

        Example:
            Given:
              TestCode::initialize  -[CALLS]->  TestCode::initializeInternal
              ConcreteCode::initializeInternal  -[OVERRIDES]->  TestCode::initializeInternal

            Emits:
              TestCode::initialize  -[VIRTUAL_DISPATCH]->  ConcreteCode::initializeInternal

        Args:
            shard_path: Path to write VIRTUAL_DISPATCH edge triples.

        Returns:
            Stats dict: {dispatch_edges_added: int, virtual_targets_found: int, errors: list}
        """
        stats = {"dispatch_edges_added": 0, "virtual_targets_found": 0, "errors": []}

        # Collect all CALLS and OVERRIDES edges from existing shards
        calls_edges: list[tuple[str, str, str, int]] = []  # (source_usr, target_usr, file, line)
        # overriders_map: base_method_usr -> [(derived_method_usr, file, line), ...]
        overriders_map: dict[str, list[tuple[str, str, int]]] = {}
        # Track static methods — these cannot be virtually dispatched
        static_methods: set[str] = set()

        shard_dir = Path(shard_path).parent
        if not shard_dir.exists():
            return stats

        for shard_file in shard_dir.glob("*.jsonl"):
            try:
                with open(shard_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        triple = json.loads(line)

                        # Collect static method USRs from Function nodes
                        if triple.get("triple_type") == "node":
                            node_data = triple.get("node_data", {})
                            if node_data.get("is_static") and node_data.get("usr"):
                                static_methods.add(node_data["usr"])
                            continue

                        if triple.get("triple_type") != "edge":
                            continue

                        edge_data = triple.get("edge_data", {})
                        rel = edge_data.get("relationship")
                        src_usr = edge_data.get("source_usr", "")
                        tgt_usr = edge_data.get("target_usr", "")

                        if rel == EdgeType.CALLS.value and src_usr and tgt_usr:
                            calls_edges.append((
                                src_usr, tgt_usr,
                                edge_data.get("file", ""),
                                edge_data.get("line", 0),
                            ))
                        elif rel == EdgeType.OVERRIDES.value and src_usr and tgt_usr:
                            # OVERRIDES: source=derived, target=base
                            if tgt_usr not in overriders_map:
                                overriders_map[tgt_usr] = []
                            overriders_map[tgt_usr].append((
                                src_usr,
                                edge_data.get("file", ""),
                                edge_data.get("line", 0),
                            ))
            except Exception as e:
                stats["errors"].append(f"Error reading shard {shard_file}: {e}")
                logger.warning("Error reading shard %s: %s", shard_file, e)

        if not overriders_map:
            logger.info("Virtual dispatch: no override chains found, skipping")
            return stats

        stats["virtual_targets_found"] = len(overriders_map)

        # For each CALLS edge, check if the target is a virtual method with overrides
        # Track already-emitted edges to avoid duplicates
        emitted: set[tuple[str, str]] = set()

        with open(shard_path, "a") as out:
            for caller_usr, callee_usr, call_file, call_line in calls_edges:
                if callee_usr not in overriders_map:
                    continue

                # Static methods cannot be virtually dispatched — skip
                if callee_usr in static_methods:
                    continue

                # The callee is a base virtual method — link caller to each overrider
                for derived_usr, derived_file, derived_line in overriders_map[callee_usr]:
                    edge_key = (caller_usr, derived_usr)
                    if edge_key in emitted:
                        continue
                    emitted.add(edge_key)

                    edge = Edge(
                        source_usr=caller_usr,
                        target_usr=derived_usr,
                        relationship=EdgeType.VIRTUAL_DISPATCH,
                        file=call_file,
                        line=call_line,
                    )
                    out.write(Triple.from_edge(edge).model_dump_json() + "\n")
                    stats["dispatch_edges_added"] += 1

        logger.info(
            "Virtual dispatch resolution: %d dispatch edges added "
            "(%d virtual targets found)",
            stats["dispatch_edges_added"],
            stats["virtual_targets_found"],
        )
        return stats

    @staticmethod
    def _method_simple_name(fqn: str) -> str:
        """Extract simple method name from FQN (e.g., 'ns::Class::method' -> 'method')."""
        return fqn.rsplit("::", 1)[-1] if "::" in fqn else fqn

    @staticmethod
    def _signatures_compatible(sig1: Optional[str], sig2: Optional[str]) -> bool:
        """
        Check if two method signatures are compatible for override detection.

        A rough heuristic: if both are None/empty, assume compatible.
        If both have the same param types, compatible.
        """
        if not sig1 and not sig2:
            return True
        if sig1 and sig2:
            # Normalize whitespace and compare
            s1 = "".join(sig1.split())
            s2 = "".join(sig2.split())
            return s1 == s2
        # One has signature, other doesn't — still might be override
        return True
