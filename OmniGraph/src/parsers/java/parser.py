"""
OmniGraph Java Parser Engine

javalang-based AST extraction for Java source files.
Implements Pass 1 of the two-pass resolver:
  - Extract all Class/Method FQNs
  - Register symbols in the Global Symbol Table
  - Queue unresolved MethodInvocations for Pass 2

Emits structural triples (nodes + inheritance edges) immediately.
Call edges are deferred to the solver (Pass 2).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import javalang

from src.core.models import (
    ClassNode,
    Edge,
    EdgeType,
    FunctionNode,
    NodeKind,
    Triple,
    generate_usr,
)

logger = logging.getLogger(__name__)


class JavaParser:
    """
    Java AST parser using javalang.

    Pass 1: Extracts all declarations and emits node triples.
    Queues MethodInvocations for Pass 2 resolution by the solver.
    """

    def parse_file(self, filepath: str, shard_path: str) -> dict:
        """
        Parse a single Java source file (Pass 1).

        Args:
            filepath: Absolute path to the .java file.
            shard_path: Path to the JSONL shard file for output.

        Returns:
            Stats dict containing:
            - nodes: count of nodes emitted
            - edges: count of edges emitted
            - symbols: list of symbol dicts for GST registration
            - unresolved_calls: list of MethodInvocation data for Pass 2
            - errors: list of error strings
        """
        stats = {
            "nodes": 0,
            "edges": 0,
            "symbols": [],
            "unresolved_calls": [],
            "errors": [],
        }

        filepath = os.path.abspath(filepath)

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except (OSError, IOError) as e:
            stats["errors"].append(f"Failed to read {filepath}: {e}")
            logger.error("Failed to read %s: %s", filepath, e)
            return stats

        try:
            tree = javalang.parse.parse(source)
        except javalang.parser.JavaSyntaxError as e:
            stats["errors"].append(f"Syntax error in {filepath}: {e}")
            logger.warning("Syntax error in %s: %s", filepath, e)
            return stats
        except Exception as e:
            stats["errors"].append(f"Parse error in {filepath}: {e}")
            logger.error("Parse error in %s: %s", filepath, e)
            return stats

        # Extract package name
        package = tree.package.name if tree.package else ""

        # Extract imports for Pass 2 resolution
        imports = []
        for imp in tree.imports or []:
            import_path = imp.path
            if imp.wildcard:
                import_path += ".*"
            imports.append(import_path)

        # Open shard file for writing
        with open(shard_path, "a") as shard_file:
            self._process_tree(tree, filepath, package, imports, shard_file, stats)

        return stats

    def _process_tree(
        self,
        tree: javalang.tree.CompilationUnit,
        filepath: str,
        package: str,
        imports: list[str],
        shard_file,
        stats: dict,
    ) -> None:
        """Walk the AST tree and extract all declarations."""

        for path, node in tree.filter(javalang.tree.TypeDeclaration):
            self._handle_type_declaration(
                node, filepath, package, "", imports, shard_file, stats
            )

    def _handle_type_declaration(
        self,
        node: Any,
        filepath: str,
        package: str,
        enclosing_class_fqn: str,
        imports: list[str],
        shard_file,
        stats: dict,
    ) -> None:
        """Process a class, interface, or enum declaration."""

        if not hasattr(node, "name") or not node.name:
            return

        # Build FQN
        if enclosing_class_fqn:
            class_fqn = f"{enclosing_class_fqn}.{node.name}"
        elif package:
            class_fqn = f"{package}.{node.name}"
        else:
            class_fqn = node.name

        usr = generate_usr(class_fqn, filepath, getattr(node, "position", None) and node.position.line or 0)
        line_num = node.position.line if hasattr(node, "position") and node.position else 0

        # Determine kind
        is_abstract = False
        if isinstance(node, javalang.tree.InterfaceDeclaration):
            kind = NodeKind.INTERFACE
            is_abstract = True
        else:
            kind = NodeKind.CLASS
            # Check for abstract modifier
            if hasattr(node, "modifiers") and node.modifiers:
                if "abstract" in node.modifiers:
                    is_abstract = True

        class_node = ClassNode(
            usr=usr,
            fqn=class_fqn,
            name=node.name,
            namespace=package,
            file=filepath,
            line=line_num,
            language="java",
            kind=kind,
            is_abstract=is_abstract,
        )

        # Emit node triple
        shard_file.write(Triple.from_class(class_node).model_dump_json() + "\n")
        stats["nodes"] += 1

        # Register symbol
        stats["symbols"].append({
            "fqn": class_fqn,
            "usr": usr,
            "kind": kind.value,
            "file": filepath,
            "line": line_num,
            "language": "java",
            "signature": None,
            "parent_fqn": enclosing_class_fqn or package or None,
        })

        # Handle inheritance: extends
        if hasattr(node, "extends") and node.extends:
            extends_list = node.extends if isinstance(node.extends, list) else [node.extends]
            for base in extends_list:
                base_name = self._type_to_name(base)
                base_fqn = self._resolve_type_name(base_name, package, imports)
                base_usr = generate_usr(base_fqn, "", 0)

                edge = Edge(
                    source_usr=usr,
                    target_usr=base_usr,
                    relationship=EdgeType.INHERITS_FROM,
                    file=filepath,
                    line=line_num,
                )
                shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
                stats["edges"] += 1

        # Handle interfaces: implements (distinct IMPLEMENTS edge)
        if hasattr(node, "implements") and node.implements:
            for iface in node.implements:
                iface_name = self._type_to_name(iface)
                iface_fqn = self._resolve_type_name(iface_name, package, imports)
                iface_usr = generate_usr(iface_fqn, "", 0)

                edge = Edge(
                    source_usr=usr,
                    target_usr=iface_usr,
                    relationship=EdgeType.IMPLEMENTS,
                    file=filepath,
                    line=line_num,
                )
                shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
                stats["edges"] += 1

        # Process methods
        class_usr = usr
        if hasattr(node, "methods"):
            for method in (node.methods or []):
                self._handle_method(
                    method, filepath, class_fqn, class_usr, package, imports, shard_file, stats
                )

        # Process constructors
        if hasattr(node, "constructors"):
            for constructor in (node.constructors or []):
                self._handle_constructor(
                    constructor, filepath, class_fqn, class_usr, package, imports, shard_file, stats
                )

        # Process inner classes
        if hasattr(node, "body"):
            for member in (node.body or []):
                if isinstance(member, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration)):
                    self._handle_type_declaration(
                        member, filepath, package, class_fqn, imports, shard_file, stats
                    )

    def _handle_method(
        self,
        method: Any,
        filepath: str,
        class_fqn: str,
        class_usr: str,
        package: str,
        imports: list[str],
        shard_file,
        stats: dict,
    ) -> None:
        """Process a method declaration."""
        if not hasattr(method, "name") or not method.name:
            return

        # Build parameter signature
        param_types = []
        if hasattr(method, "parameters") and method.parameters:
            for param in method.parameters:
                ptype = self._type_to_name(param.type) if hasattr(param, "type") and param.type else "Object"
                param_types.append(ptype)

        signature = ", ".join(param_types)
        method_fqn = f"{class_fqn}.{method.name}({signature})"
        line_num = method.position.line if hasattr(method, "position") and method.position else 0
        usr = generate_usr(method_fqn, filepath, line_num)

        # Extract modifiers
        modifiers = set(method.modifiers) if hasattr(method, "modifiers") and method.modifiers else set()
        is_static = "static" in modifiers
        is_abstract = "abstract" in modifiers

        # Detect @Override annotation
        has_override = False
        if hasattr(method, "annotations") and method.annotations:
            for annotation in method.annotations:
                ann_name = annotation.name if hasattr(annotation, "name") else str(annotation)
                if ann_name == "Override":
                    has_override = True
                    break

        func_node = FunctionNode(
            usr=usr,
            name=method.name,
            fqn=method_fqn,
            file=filepath,
            line=line_num,
            language="java",
            kind=NodeKind.FUNCTION,
            signature=signature,
            parent_fqn=class_fqn,
            is_static=is_static,
            is_virtual=not is_static and not ("final" in modifiers or "private" in modifiers),
            is_pure_virtual=is_abstract,
        )

        # Emit node triple
        shard_file.write(Triple.from_function(func_node).model_dump_json() + "\n")
        stats["nodes"] += 1

        # Emit DEFINES edge
        edge = Edge(
            source_usr=class_usr,
            target_usr=usr,
            relationship=EdgeType.DEFINES,
            file=filepath,
            line=line_num,
        )
        shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
        stats["edges"] += 1

        # Emit OVERRIDES edge if @Override is present
        if has_override:
            overridden_usr = self._find_overridden_method(
                class_fqn, method.name, signature, package, imports, stats
            )
            if overridden_usr:
                override_edge = Edge(
                    source_usr=usr,
                    target_usr=overridden_usr,
                    relationship=EdgeType.OVERRIDES,
                    file=filepath,
                    line=line_num,
                )
                shard_file.write(Triple.from_edge(override_edge).model_dump_json() + "\n")
                stats["edges"] += 1

        # Register symbol
        stats["symbols"].append({
            "fqn": method_fqn,
            "usr": usr,
            "kind": "method",
            "file": filepath,
            "line": line_num,
            "language": "java",
            "signature": signature,
            "parent_fqn": class_fqn,
        })

        # Scan method body for MethodInvocations (queue for Pass 2)
        if hasattr(method, "body") and method.body:
            self._collect_invocations(
                method.body, filepath, method_fqn, usr, class_fqn, package, imports, shard_file, stats
            )

    def _handle_constructor(
        self,
        constructor: Any,
        filepath: str,
        class_fqn: str,
        class_usr: str,
        package: str,
        imports: list[str],
        shard_file,
        stats: dict,
    ) -> None:
        """Process a constructor declaration."""
        name = constructor.name if hasattr(constructor, "name") and constructor.name else class_fqn.rsplit(".", 1)[-1]

        param_types = []
        if hasattr(constructor, "parameters") and constructor.parameters:
            for param in constructor.parameters:
                ptype = self._type_to_name(param.type) if hasattr(param, "type") and param.type else "Object"
                param_types.append(ptype)

        signature = ", ".join(param_types)
        ctor_fqn = f"{class_fqn}.{name}({signature})"
        line_num = constructor.position.line if hasattr(constructor, "position") and constructor.position else 0
        usr = generate_usr(ctor_fqn, filepath, line_num)

        func_node = FunctionNode(
            usr=usr,
            name=name,
            fqn=ctor_fqn,
            file=filepath,
            line=line_num,
            language="java",
            kind=NodeKind.CONSTRUCTOR,
            signature=signature,
            parent_fqn=class_fqn,
        )

        shard_file.write(Triple.from_function(func_node).model_dump_json() + "\n")
        stats["nodes"] += 1

        edge = Edge(
            source_usr=class_usr,
            target_usr=usr,
            relationship=EdgeType.DEFINES,
            file=filepath,
            line=line_num,
        )
        shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
        stats["edges"] += 1

        stats["symbols"].append({
            "fqn": ctor_fqn,
            "usr": usr,
            "kind": "constructor",
            "file": filepath,
            "line": line_num,
            "language": "java",
            "signature": signature,
            "parent_fqn": class_fqn,
        })

        # Scan constructor body for invocations
        if hasattr(constructor, "body") and constructor.body:
            self._collect_invocations(
                constructor.body, filepath, ctor_fqn, usr, class_fqn, package, imports, shard_file, stats
            )

    def _collect_invocations(
        self,
        body: Any,
        filepath: str,
        method_fqn: str,
        method_usr: str,
        class_fqn: str,
        package: str,
        imports: list[str],
        shard_file,
        stats: dict,
    ) -> None:
        """Recursively collect MethodInvocation and Lambda nodes from a method body."""
        if body is None:
            return

        nodes_to_visit = [body] if not isinstance(body, list) else list(body)
        lambda_counter = 0

        while nodes_to_visit:
            current = nodes_to_visit.pop()
            if current is None:
                continue

            if isinstance(current, javalang.tree.MethodInvocation):
                invocation_data = {
                    "qualifier": current.qualifier if hasattr(current, "qualifier") else None,
                    "member": current.member,
                    "arguments_count": len(current.arguments) if current.arguments else 0,
                    "caller_usr": method_usr,
                    "caller_fqn": method_fqn,
                    "class_fqn": class_fqn,
                    "package": package,
                    "imports": imports,
                    "file": filepath,
                    "line": current.position.line if hasattr(current, "position") and current.position else 0,
                }
                stats["unresolved_calls"].append(invocation_data)

            elif isinstance(current, javalang.tree.ClassCreator):
                # Constructor call: new ClassName(...)
                if hasattr(current, "type") and current.type:
                    type_name = self._type_to_name(current.type)
                    invocation_data = {
                        "qualifier": None,
                        "member": type_name,
                        "arguments_count": len(current.arguments) if current.arguments else 0,
                        "caller_usr": method_usr,
                        "caller_fqn": method_fqn,
                        "class_fqn": class_fqn,
                        "package": package,
                        "imports": imports,
                        "file": filepath,
                        "line": current.position.line if hasattr(current, "position") and current.position else 0,
                        "is_constructor": True,
                    }
                    stats["unresolved_calls"].append(invocation_data)

            elif isinstance(current, javalang.tree.LambdaExpression):
                # Java lambda: emit synthetic function node and collect its body invocations
                lambda_counter += 1
                line_num = current.position.line if hasattr(current, "position") and current.position else 0
                lambda_fqn = f"{method_fqn}::lambda${lambda_counter}@L{line_num}"
                lambda_usr = generate_usr(lambda_fqn, filepath, line_num)

                lambda_node = FunctionNode(
                    usr=lambda_usr,
                    name=f"lambda${lambda_counter}",
                    fqn=lambda_fqn,
                    file=filepath,
                    line=line_num,
                    language="java",
                    kind=NodeKind.LAMBDA,
                    parent_fqn=class_fqn,
                )
                shard_file.write(Triple.from_function(lambda_node).model_dump_json() + "\n")
                stats["nodes"] += 1

                # The enclosing method calls into the lambda
                call_edge = Edge(
                    source_usr=method_usr,
                    target_usr=lambda_usr,
                    relationship=EdgeType.CALLS,
                    file=filepath,
                    line=line_num,
                )
                shard_file.write(Triple.from_edge(call_edge).model_dump_json() + "\n")
                stats["edges"] += 1

                # Recursively collect invocations inside the lambda body
                if hasattr(current, "body") and current.body:
                    self._collect_invocations(
                        current.body, filepath, lambda_fqn, lambda_usr,
                        class_fqn, package, imports, shard_file, stats
                    )
                continue  # Don't re-traverse lambda children below

            # Traverse children of this AST node
            if isinstance(current, javalang.tree.Node):
                for attr_name in current.attrs:
                    attr_val = getattr(current, attr_name, None)
                    if attr_val is None:
                        continue
                    if isinstance(attr_val, javalang.tree.Node):
                        nodes_to_visit.append(attr_val)
                    elif isinstance(attr_val, list):
                        for item in attr_val:
                            if isinstance(item, javalang.tree.Node):
                                nodes_to_visit.append(item)
            elif isinstance(current, list):
                for item in current:
                    if isinstance(item, javalang.tree.Node):
                        nodes_to_visit.append(item)

    @staticmethod
    def _type_to_name(type_ref: Any) -> str:
        """Convert a javalang type reference to a simple name string."""
        if type_ref is None:
            return "void"
        if isinstance(type_ref, str):
            return type_ref
        if hasattr(type_ref, "name"):
            return type_ref.name
        if hasattr(type_ref, "value"):
            return type_ref.value
        return str(type_ref)

    @staticmethod
    def _resolve_type_name(
        type_name: str,
        package: str,
        imports: list[str],
    ) -> str:
        """
        Best-effort type name to FQN resolution (during Pass 1).

        Resolution order:
        1. If already fully qualified (contains dots), use as-is
        2. Check explicit imports
        3. Assume same package
        """
        if "." in type_name:
            return type_name

        # Check imports
        for imp in imports:
            if imp.endswith(f".{type_name}"):
                return imp
            if imp.endswith(".*"):
                # Wildcard import — assume this package
                return f"{imp[:-2]}.{type_name}"

        # Assume same package
        if package:
            return f"{package}.{type_name}"
        return type_name

    def _find_overridden_method(
        self,
        class_fqn: str,
        method_name: str,
        signature: str,
        package: str,
        imports: list[str],
        stats: dict,
    ) -> Optional[str]:
        """
        Find the USR of the parent class method that this @Override method overrides.

        Searches the symbols list (already collected during this parse session)
        for parent class methods with matching name and signature.
        Looks at INHERITS_FROM/IMPLEMENTS edges to find parent classes.
        """
        # Search accumulated symbols for parent class methods
        # Build a lookup of class FQN -> list of method symbols
        class_methods: dict[str, list[dict]] = {}
        parent_classes: list[str] = []

        for sym in stats.get("symbols", []):
            if sym.get("kind") in ("method", "function") and sym.get("parent_fqn"):
                parent = sym["parent_fqn"]
                if parent not in class_methods:
                    class_methods[parent] = []
                class_methods[parent].append(sym)

        # Find parent classes of the current class by scanning edges
        # This is a best-effort during Pass 1; full resolution happens in Pass 2
        for sym in stats.get("symbols", []):
            if sym.get("kind") in ("class", "interface"):
                sym_fqn = sym.get("fqn", "")
                parent_fqn = sym.get("parent_fqn", "")
                if sym_fqn == class_fqn and parent_fqn:
                    parent_classes.append(parent_fqn)

        # Check each parent class for a method with matching name
        for parent_fqn in parent_classes:
            for method in class_methods.get(parent_fqn, []):
                # Match by method name (extracted from FQN)
                method_fqn = method.get("fqn", "")
                # FQN format: package.Class.method(sig)
                parent_method_name = method_fqn.rsplit(".", 1)[-1].split("(")[0] if "." in method_fqn else method_fqn
                if parent_method_name == method_name:
                    return method.get("usr")

        return None
