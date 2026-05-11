"""
OmniGraph Java Two-Pass FQN Solver

Pass 2 of the Java resolver pipeline:
  After all files have been parsed (Pass 1) and their symbols
  registered in the Global Symbol Table, this module resolves
  queued MethodInvocations to their target FQNs.

Resolution strategy:
  1. Explicit qualifier → resolve qualifier type → lookup method in GST
  2. No qualifier (implicit `this`) → resolve against enclosing class
  3. Static import → direct FQN resolution
  4. Unresolvable → emit as `unresolved:qualifier.method` with warning
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.models import Edge, EdgeType, Triple, generate_usr
from src.utils.symbols import GlobalSymbolTable

logger = logging.getLogger(__name__)


class JavaSolver:
    """
    Two-pass FQN resolver for Java MethodInvocations.

    Consumes the `unresolved_calls` list produced by JavaParser
    and emits CALLS edge triples using the Global Symbol Table
    for target resolution.
    """

    def __init__(self, gst: GlobalSymbolTable):
        self.gst = gst
        self._type_cache: dict[str, str] = {}  # local_name -> resolved_fqn

    def resolve_calls(
        self,
        unresolved_calls: list[dict],
        shard_path: str,
    ) -> dict:
        """
        Resolve a batch of queued MethodInvocations (Pass 2).

        Args:
            unresolved_calls: List of invocation dicts from JavaParser.
            shard_path: Path to append resolved CALLS edge triples.

        Returns:
            Stats dict: {resolved: int, unresolved: int, errors: list}
        """
        stats = {"resolved": 0, "unresolved": 0, "errors": []}

        with open(shard_path, "a") as shard_file:
            for call in unresolved_calls:
                try:
                    edge = self._resolve_single_call(call)
                    if edge:
                        shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
                        stats["resolved"] += 1
                    else:
                        stats["unresolved"] += 1
                except Exception as e:
                    stats["errors"].append(
                        f"Error resolving {call.get('member', '?')} in {call.get('file', '?')}: {e}"
                    )
                    stats["unresolved"] += 1

        logger.info(
            "Java Pass 2: %d resolved, %d unresolved, %d errors",
            stats["resolved"],
            stats["unresolved"],
            len(stats["errors"]),
        )
        return stats

    def _resolve_single_call(self, call: dict) -> Optional[Edge]:
        """
        Attempt to resolve a single MethodInvocation to a CALLS edge.

        Args:
            call: Invocation data dict from JavaParser.

        Returns:
            Edge if resolved, None otherwise.
        """
        qualifier = call.get("qualifier")
        member = call.get("member", "")
        caller_usr = call.get("caller_usr", "")
        class_fqn = call.get("class_fqn", "")
        package = call.get("package", "")
        imports = call.get("imports", [])
        arg_count = call.get("arguments_count", 0)
        is_constructor = call.get("is_constructor", False)
        file = call.get("file", "")
        line = call.get("line", 0)

        target_usr = None

        if is_constructor:
            # Constructor call: new ClassName(...)
            target_usr = self._resolve_constructor(member, package, imports, arg_count)

        elif qualifier is None or qualifier == "this":
            # No qualifier or explicit `this` — resolve against enclosing class
            target_usr = self._resolve_this_method(class_fqn, member, arg_count)

        elif qualifier == "super":
            # super.method() — need to find parent class
            target_usr = self._resolve_super_method(class_fqn, member, arg_count)

        elif isinstance(qualifier, str):
            # Explicit qualifier: qualifier.method()
            target_usr = self._resolve_qualified_method(
                qualifier, member, class_fqn, package, imports, arg_count
            )

        if target_usr:
            return Edge(
                source_usr=caller_usr,
                target_usr=target_usr,
                relationship=EdgeType.CALLS,
                file=file,
                line=line,
            )

        # Emit unresolved edge with synthetic USR
        qualifier_str = qualifier if qualifier else "this"
        unresolved_usr = f"unresolved:{qualifier_str}.{member}"
        logger.debug("Unresolved call: %s.%s in %s:%d", qualifier_str, member, file, line)

        return Edge(
            source_usr=caller_usr,
            target_usr=unresolved_usr,
            relationship=EdgeType.CALLS,
            file=file,
            line=line,
        )

    def _resolve_this_method(
        self,
        class_fqn: str,
        method_name: str,
        arg_count: int,
    ) -> Optional[str]:
        """Resolve a method call on `this` (implicit or explicit)."""
        result = self.gst.resolve_method(class_fqn, method_name, arg_count)
        if result:
            return result["usr"]

        # Check parent classes (inheritance chain)
        # Look for INHERITS_FROM edges in the GST
        parent_classes = self._get_parent_classes(class_fqn)
        for parent_fqn in parent_classes:
            result = self.gst.resolve_method(parent_fqn, method_name, arg_count)
            if result:
                return result["usr"]

        return None

    def _resolve_super_method(
        self,
        class_fqn: str,
        method_name: str,
        arg_count: int,
    ) -> Optional[str]:
        """Resolve a super.method() call."""
        parent_classes = self._get_parent_classes(class_fqn)
        for parent_fqn in parent_classes:
            result = self.gst.resolve_method(parent_fqn, method_name, arg_count)
            if result:
                return result["usr"]
        return None

    def _resolve_qualified_method(
        self,
        qualifier: str,
        method_name: str,
        class_fqn: str,
        package: str,
        imports: list[str],
        arg_count: int,
    ) -> Optional[str]:
        """
        Resolve qualifier.method() where qualifier is a variable name,
        class name, or field name.
        """
        # 1. Check if qualifier is itself a class FQN
        qualifier_class = self.gst.resolve(qualifier, class_fqn, imports)
        if qualifier_class and qualifier_class.get("kind") in ("class", "interface"):
            # Static method call on a class
            result = self.gst.resolve_method(qualifier_class["fqn"], method_name, arg_count)
            if result:
                return result["usr"]

        # 2. Try to resolve the qualifier as a type name
        resolved_type = self._resolve_type(qualifier, package, imports)
        if resolved_type:
            result = self.gst.resolve_method(resolved_type, method_name, arg_count)
            if result:
                return result["usr"]

        # 3. Check if qualifier.method matches any FQN suffix
        candidate = f"{qualifier}.{method_name}"
        result = self.gst.resolve(candidate, class_fqn, imports)
        if result:
            return result["usr"]

        return None

    def _resolve_constructor(
        self,
        class_name: str,
        package: str,
        imports: list[str],
        arg_count: int,
    ) -> Optional[str]:
        """Resolve a constructor call (new ClassName(...))."""
        # Resolve class name to FQN
        resolved_type = self._resolve_type(class_name, package, imports)
        if resolved_type:
            result = self.gst.resolve_method(resolved_type, class_name, arg_count)
            if result:
                return result["usr"]
            # Maybe the constructor is registered as ClassName(...)
            result = self.gst.resolve_method(
                resolved_type, resolved_type.rsplit(".", 1)[-1], arg_count
            )
            if result:
                return result["usr"]
        return None

    def _resolve_type(
        self,
        type_name: str,
        package: str,
        imports: list[str],
    ) -> Optional[str]:
        """Resolve a simple type name to an FQN using imports and package."""
        # Check cache
        cache_key = f"{type_name}::{package}"
        if cache_key in self._type_cache:
            return self._type_cache[cache_key]

        # Already fully qualified
        if "." in type_name:
            self._type_cache[cache_key] = type_name
            return type_name

        # Check explicit imports
        for imp in imports:
            if imp.endswith(f".{type_name}"):
                self._type_cache[cache_key] = imp
                return imp

        # Check wildcard imports
        for imp in imports:
            if imp.endswith(".*"):
                candidate = f"{imp[:-2]}.{type_name}"
                if self.gst.lookup_fqn(candidate):
                    self._type_cache[cache_key] = candidate
                    return candidate

        # Same package
        if package:
            candidate = f"{package}.{type_name}"
            if self.gst.lookup_fqn(candidate):
                self._type_cache[cache_key] = candidate
                return candidate

        # java.lang implicit import
        candidate = f"java.lang.{type_name}"
        if self.gst.lookup_fqn(candidate):
            self._type_cache[cache_key] = candidate
            return candidate

        return None

    def _get_parent_classes(self, class_fqn: str) -> list[str]:
        """
        Get parent class FQNs for a given class.

        Looks at the GST for symbols with kind='class' whose FQN
        matches the parent of the given class (via inheritance edges
        that were registered during Pass 1).

        For simplicity, we use a heuristic: check for common Java patterns.
        Full resolution would require the inheritance edge data from shards.
        """
        # This is a simplified lookup — the orchestrator can enhance this
        # by pre-building an inheritance map from the shard edges
        parents = []

        # Check GST for classes that the given class might inherit from
        class_info = self.gst.lookup_fqn(class_fqn)
        if class_info and class_info.get("parent_fqn"):
            parent_info = self.gst.lookup_fqn(class_info["parent_fqn"])
            if parent_info and parent_info.get("kind") in ("class", "interface"):
                parents.append(parent_info["fqn"])

        return parents
