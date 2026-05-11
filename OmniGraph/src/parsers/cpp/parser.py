"""
OmniGraph C++ Parser Engine

libclang-based AST traversal with USR extraction.
Uses `pip install libclang` for self-contained bindings.

Processes files one at a time, emitting JSONL triples to shard files.
Each TranslationUnit is discarded after traversal to maintain O(1) memory.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from clang import cindex

from src.core.models import (
    ClassNode,
    Edge,
    EdgeType,
    FunctionNode,
    NodeKind,
    Triple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CursorKind groups for cleaner dispatch
# ---------------------------------------------------------------------------

CLASS_KINDS = {
    cindex.CursorKind.CLASS_DECL,
    cindex.CursorKind.STRUCT_DECL,
    cindex.CursorKind.CLASS_TEMPLATE,
}

FUNCTION_KINDS = {
    cindex.CursorKind.FUNCTION_DECL,
    cindex.CursorKind.CXX_METHOD,
    cindex.CursorKind.CONSTRUCTOR,
    cindex.CursorKind.DESTRUCTOR,
    cindex.CursorKind.FUNCTION_TEMPLATE,
}

CALL_KINDS = {
    cindex.CursorKind.CALL_EXPR,
    cindex.CursorKind.MEMBER_REF_EXPR,
}


class CppParser:
    """
    C++ AST parser using libclang.

    Extracts classes, functions, call relationships, inheritance,
    and override chains using USR (Unified Symbol Resolution) as
    the primary identity key.
    """

    def __init__(
        self,
        compile_args: Optional[list[str]] = None,
    ):
        """
        Args:
            compile_args: Compiler flags including -I include paths,
                          -std=c++17, -DFOO=1, etc. Already merged
                          by the orchestrator from include_flags + compile_args.
        """
        self.index = cindex.Index.create()
        self.compile_args = compile_args or ["-std=c++17"]

    def parse_file(self, filepath: str, shard_path: str) -> dict:
        """
        Parse a single C++ source file and emit triples to a JSONL shard.

        Args:
            filepath: Absolute path to the C++ source file.
            shard_path: Path to the JSONL shard file for output.

        Returns:
            Stats dict: {nodes: int, edges: int, errors: list[str]}
        """
        stats = {"nodes": 0, "edges": 0, "errors": [], "symbols": []}
        filepath = os.path.abspath(filepath)

        try:
            tu = self.index.parse(
                filepath,
                args=self.compile_args,
                options=(
                    cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                    | cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES * 0  # We need bodies for call analysis
                ),
            )
        except Exception as e:
            stats["errors"].append(f"Failed to parse {filepath}: {e}")
            logger.error("Failed to parse %s: %s", filepath, e)
            return stats

        # Check for fatal diagnostics
        for diag in tu.diagnostics:
            if diag.severity >= cindex.Diagnostic.Error:
                stats["errors"].append(f"Clang error in {filepath}: {diag.spelling}")
                logger.warning("Clang diagnostic: %s", diag.spelling)

        # State tracking for call resolution
        context = _TraversalContext(filepath=filepath)

        # Open shard file for writing
        with open(shard_path, "a") as shard_file:
            self._traverse(tu.cursor, context, shard_file, stats)

        return stats

    def _traverse(
        self,
        cursor: cindex.Cursor,
        context: _TraversalContext,
        shard_file,
        stats: dict,
    ) -> None:
        """Recursively traverse the AST, emitting triples."""

        # Only process nodes in the main file (skip system headers)
        if cursor.location.file and cursor.location.file.name != context.filepath:
            return

        kind = cursor.kind

        # --- Class/Struct Declaration ---
        if kind in CLASS_KINDS:
            self._handle_class(cursor, context, shard_file, stats)

        # --- Function/Method Declaration ---
        elif kind in FUNCTION_KINDS:
            self._handle_function(cursor, context, shard_file, stats)

        # --- Call Expression ---
        elif kind in CALL_KINDS:
            self._handle_call(cursor, context, shard_file, stats)

        # --- Inheritance ---
        elif kind == cindex.CursorKind.CXX_BASE_SPECIFIER:
            self._handle_inheritance(cursor, context, shard_file, stats)

        # --- Lambda Expression ---
        elif kind == cindex.CursorKind.LAMBDA_EXPR:
            self._handle_lambda(cursor, context, shard_file, stats)

        # Recurse into children
        for child in cursor.get_children():
            self._traverse(child, context, shard_file, stats)

    def _handle_class(
        self,
        cursor: cindex.Cursor,
        context: _TraversalContext,
        shard_file,
        stats: dict,
    ) -> None:
        """Process a class/struct declaration."""
        usr = cursor.get_usr()
        if not usr or not cursor.spelling:
            return

        # Build fully qualified name from semantic parent chain
        fqn = self._build_fqn(cursor)
        namespace = self._get_namespace(cursor)

        # Detect abstract class: has at least one pure virtual method
        is_abstract = self._has_pure_virtual_method(cursor)

        node = ClassNode(
            usr=usr,
            fqn=fqn,
            name=cursor.spelling,
            namespace=namespace,
            file=context.filepath,
            line=cursor.location.line,
            language="cpp",
            kind=NodeKind.CLASS,
            is_abstract=is_abstract,
        )

        # Emit node triple
        triple = Triple.from_class(node)
        shard_file.write(triple.model_dump_json() + "\n")
        stats["nodes"] += 1

        # Register in symbol list for GST
        stats["symbols"].append({
            "fqn": fqn,
            "usr": usr,
            "kind": "class",
            "file": context.filepath,
            "line": cursor.location.line,
            "language": "cpp",
            "signature": None,
            "parent_fqn": namespace or None,
        })

        # Track current class context
        context.push_class(fqn, usr)

        # Process children within class context
        for child in cursor.get_children():
            self._traverse(child, context, shard_file, stats)

        context.pop_class()

    def _handle_function(
        self,
        cursor: cindex.Cursor,
        context: _TraversalContext,
        shard_file,
        stats: dict,
    ) -> None:
        """Process a function/method declaration."""
        usr = cursor.get_usr()
        if not usr or not cursor.spelling:
            return

        fqn = self._build_fqn(cursor)
        signature = self._extract_signature(cursor)
        parent_fqn = context.current_class_fqn

        # Determine node kind
        if cursor.kind == cindex.CursorKind.CONSTRUCTOR:
            node_kind = NodeKind.CONSTRUCTOR
        else:
            node_kind = NodeKind.FUNCTION

        # Extract OOP qualifiers from libclang
        is_virtual = False
        is_pure_virtual = False
        is_static = False
        is_const = False

        if cursor.kind in (cindex.CursorKind.CXX_METHOD, cindex.CursorKind.DESTRUCTOR):
            try:
                is_virtual = cursor.is_virtual_method()
            except Exception:
                pass
            try:
                is_pure_virtual = cursor.is_pure_virtual_method()
                if is_pure_virtual:
                    is_virtual = True  # Pure virtual implies virtual
            except Exception:
                pass
            try:
                is_static = cursor.is_static_method()
            except Exception:
                pass
            try:
                # const-qualified method: check if the method type has const qualifier
                method_type = cursor.type
                if method_type and method_type.is_const_qualified():
                    is_const = True
            except Exception:
                pass

        elif cursor.kind == cindex.CursorKind.FUNCTION_DECL:
            # Free functions can be static (file-scope linkage)
            try:
                if cursor.storage_class == cindex.StorageClass.STATIC:
                    is_static = True
            except Exception:
                pass

        # Append const qualifier to signature for overload disambiguation
        full_signature = signature
        if is_const:
            full_signature = f"{signature} const" if signature else "const"

        node = FunctionNode(
            usr=usr,
            name=cursor.spelling,
            fqn=fqn,
            file=context.filepath,
            line=cursor.location.line,
            language="cpp",
            kind=node_kind,
            signature=full_signature,
            parent_fqn=parent_fqn,
            is_virtual=is_virtual,
            is_pure_virtual=is_pure_virtual,
            is_static=is_static,
            is_const=is_const,
        )

        # Emit node triple
        triple = Triple.from_function(node)
        shard_file.write(triple.model_dump_json() + "\n")
        stats["nodes"] += 1

        # Register in symbol list for GST
        stats["symbols"].append({
            "fqn": fqn,
            "usr": usr,
            "kind": node_kind.value,
            "file": context.filepath,
            "line": cursor.location.line,
            "language": "cpp",
            "signature": full_signature,
            "parent_fqn": parent_fqn,
        })

        # Emit DEFINES edge (class defines method)
        if parent_fqn and context.current_class_usr:
            edge = Edge(
                source_usr=context.current_class_usr,
                target_usr=usr,
                relationship=EdgeType.DEFINES,
                file=context.filepath,
                line=cursor.location.line,
            )
            shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
            stats["edges"] += 1

        # Handle overrides (virtual method resolution)
        try:
            overridden = cursor.get_overridden_cursors()
            if overridden:
                for base_cursor in overridden:
                    base_usr = base_cursor.get_usr()
                    if base_usr:
                        edge = Edge(
                            source_usr=usr,
                            target_usr=base_usr,
                            relationship=EdgeType.OVERRIDES,
                            file=context.filepath,
                            line=cursor.location.line,
                        )
                        shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
                        stats["edges"] += 1
        except Exception as e:
            logger.debug("Override detection failed for %s: %s", fqn, e)

        # Push function context for tracking calls within this function
        prev_func = context.current_function_usr
        context.current_function_usr = usr

        # Process function body children
        for child in cursor.get_children():
            self._traverse(child, context, shard_file, stats)

        context.current_function_usr = prev_func

    def _handle_call(
        self,
        cursor: cindex.Cursor,
        context: _TraversalContext,
        shard_file,
        stats: dict,
    ) -> None:
        """Process a call expression."""
        if not context.current_function_usr:
            return

        referenced = cursor.referenced
        if referenced is None:
            return

        target_usr = referenced.get_usr()
        if not target_usr:
            return

        edge = Edge(
            source_usr=context.current_function_usr,
            target_usr=target_usr,
            relationship=EdgeType.CALLS,
            file=context.filepath,
            line=cursor.location.line,
        )
        shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
        stats["edges"] += 1

    def _handle_inheritance(
        self,
        cursor: cindex.Cursor,
        context: _TraversalContext,
        shard_file,
        stats: dict,
    ) -> None:
        """Process a base class specifier (inheritance)."""
        if not context.current_class_usr:
            return

        # The referenced cursor points to the base class definition
        referenced = cursor.referenced
        if referenced is None:
            # Try getting the type's declaration
            base_type = cursor.type
            if base_type:
                decl = base_type.get_declaration()
                if decl and decl.get_usr():
                    referenced = decl

        if referenced is None:
            return

        base_usr = referenced.get_usr()
        if not base_usr:
            return

        edge = Edge(
            source_usr=context.current_class_usr,
            target_usr=base_usr,
            relationship=EdgeType.INHERITS_FROM,
            file=context.filepath,
            line=cursor.location.line,
        )
        shard_file.write(Triple.from_edge(edge).model_dump_json() + "\n")
        stats["edges"] += 1

    def _handle_lambda(
        self,
        cursor: cindex.Cursor,
        context: _TraversalContext,
        shard_file,
        stats: dict,
    ) -> None:
        """Process a lambda expression — treated as a synthetic function."""
        usr = cursor.get_usr()
        if not usr:
            # Generate a synthetic USR for the lambda
            usr = f"lambda@{context.filepath}:{cursor.location.line}:{cursor.location.column}"

        fqn = f"{context.current_class_fqn or 'global'}::lambda@L{cursor.location.line}"

        node = FunctionNode(
            usr=usr,
            name=f"lambda@L{cursor.location.line}",
            fqn=fqn,
            file=context.filepath,
            line=cursor.location.line,
            language="cpp",
            kind=NodeKind.LAMBDA,
            parent_fqn=context.current_class_fqn,
        )

        triple = Triple.from_function(node)
        shard_file.write(triple.model_dump_json() + "\n")
        stats["nodes"] += 1

        # Process lambda body with the lambda as the current function
        prev_func = context.current_function_usr
        context.current_function_usr = usr

        for child in cursor.get_children():
            self._traverse(child, context, shard_file, stats)

        context.current_function_usr = prev_func

    # --- Utility Methods ---

    @staticmethod
    def _has_pure_virtual_method(cursor: cindex.Cursor) -> bool:
        """Check if a class has any pure virtual methods (making it abstract)."""
        for child in cursor.get_children():
            if child.kind == cindex.CursorKind.CXX_METHOD:
                try:
                    if child.is_pure_virtual_method():
                        return True
                except Exception:
                    pass
        return False

    @staticmethod
    def _build_fqn(cursor: cindex.Cursor) -> str:
        """Build a fully qualified name by walking the semantic parent chain."""
        parts = []
        c = cursor
        while c is not None and c.kind != cindex.CursorKind.TRANSLATION_UNIT:
            if c.spelling:
                parts.append(c.spelling)
            c = c.semantic_parent
        return "::".join(reversed(parts))

    @staticmethod
    def _get_namespace(cursor: cindex.Cursor) -> str:
        """Extract the namespace from the semantic parent chain."""
        parts = []
        c = cursor.semantic_parent
        while c is not None and c.kind != cindex.CursorKind.TRANSLATION_UNIT:
            if c.kind == cindex.CursorKind.NAMESPACE and c.spelling:
                parts.append(c.spelling)
            c = c.semantic_parent
        return "::".join(reversed(parts))

    @staticmethod
    def _extract_signature(cursor: cindex.Cursor) -> str:
        """Extract parameter type signature for overload disambiguation."""
        try:
            func_type = cursor.type
            if func_type.kind == cindex.TypeKind.FUNCTIONPROTO:
                arg_types = [func_type.get_argument(i).spelling for i in range(func_type.get_num_arguments())]
                return ", ".join(arg_types)
        except Exception:
            pass

        # Fallback: extract from children
        params = []
        for child in cursor.get_children():
            if child.kind == cindex.CursorKind.PARM_DECL:
                params.append(child.type.spelling if child.type else "unknown")
        return ", ".join(params)


class _TraversalContext:
    """Mutable traversal state passed through recursive AST walk."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._class_stack: list[tuple[str, str]] = []  # (fqn, usr)
        self.current_function_usr: Optional[str] = None

    @property
    def current_class_fqn(self) -> Optional[str]:
        return self._class_stack[-1][0] if self._class_stack else None

    @property
    def current_class_usr(self) -> Optional[str]:
        return self._class_stack[-1][1] if self._class_stack else None

    def push_class(self, fqn: str, usr: str) -> None:
        self._class_stack.append((fqn, usr))

    def pop_class(self) -> None:
        if self._class_stack:
            self._class_stack.pop()
