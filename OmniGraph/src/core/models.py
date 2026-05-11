"""
OmniGraph Core Data Models

Pydantic v2 schemas for the graph data model: nodes, edges, and triples.
All models serialize to JSON for JSONL shard emission.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EdgeType(str, Enum):
    """Relationship types in the knowledge graph."""
    CALLS = "CALLS"
    INHERITS_FROM = "INHERITS_FROM"
    IMPLEMENTS = "IMPLEMENTS"
    DEFINES = "DEFINES"
    OVERRIDES = "OVERRIDES"
    VIRTUAL_DISPATCH = "VIRTUAL_DISPATCH"


class NodeKind(str, Enum):
    """Node type discriminator."""
    FUNCTION = "function"
    CLASS = "class"
    INTERFACE = "interface"
    LAMBDA = "lambda"
    CONSTRUCTOR = "constructor"


# ---------------------------------------------------------------------------
# Node Models
# ---------------------------------------------------------------------------

class FunctionNode(BaseModel):
    """Represents a function or method vertex in the knowledge graph."""
    usr: str = Field(..., description="Unified Symbol Resolution key (C++) or generated hash (Java)")
    name: str = Field(..., description="Simple function/method name")
    fqn: str = Field(..., description="Fully Qualified Name")
    file: str = Field(..., description="Source file path")
    line: int = Field(..., ge=0, description="Line number of declaration")
    language: Literal["cpp", "java"] = Field(..., description="Source language")
    kind: NodeKind = Field(default=NodeKind.FUNCTION, description="Node sub-type")
    signature: Optional[str] = Field(default=None, description="Parameter type signature for overload disambiguation")
    parent_fqn: Optional[str] = Field(default=None, description="Enclosing class FQN")
    is_virtual: bool = Field(default=False, description="C++: virtual method (can be overridden)")
    is_pure_virtual: bool = Field(default=False, description="C++: pure virtual (= 0), forces subclass override")
    is_static: bool = Field(default=False, description="Static method — cannot be virtually dispatched")
    is_const: bool = Field(default=False, description="C++: const-qualified method (distinct overload from non-const)")

    @computed_field
    @property
    def node_label(self) -> str:
        return "Function"


class ClassNode(BaseModel):
    """Represents a class, struct, or interface vertex in the knowledge graph."""
    usr: str = Field(..., description="Unified Symbol Resolution key (C++) or generated hash (Java)")
    fqn: str = Field(..., description="Fully Qualified Name")
    name: str = Field(..., description="Simple class name")
    namespace: str = Field(default="", description="Enclosing namespace or package")
    file: str = Field(..., description="Source file path")
    line: int = Field(..., ge=0, description="Line number of declaration")
    language: Literal["cpp", "java"] = Field(..., description="Source language")
    kind: NodeKind = Field(default=NodeKind.CLASS, description="Node sub-type (class/interface)")
    is_abstract: bool = Field(default=False, description="Class has at least one pure virtual / abstract method")

    @computed_field
    @property
    def node_label(self) -> str:
        return "Class"


# ---------------------------------------------------------------------------
# Edge Model
# ---------------------------------------------------------------------------

class Edge(BaseModel):
    """Represents a directed relationship between two nodes."""
    source_usr: str = Field(..., description="USR of the source node")
    target_usr: str = Field(..., description="USR of the target node")
    relationship: EdgeType = Field(..., description="Type of relationship")
    file: str = Field(default="", description="File where the relationship is expressed")
    line: int = Field(default=0, ge=0, description="Line number where the relationship is expressed")


# ---------------------------------------------------------------------------
# Triple (the atomic shard unit)
# ---------------------------------------------------------------------------

class Triple(BaseModel):
    """
    The atomic unit written to JSONL shards.

    Encodes a complete subject-predicate-object triple:
    (subject_node) -[relationship]-> (object_usr)

    The subject is fully described; the object is referenced by USR only
    (it will be resolved during ingestion via MERGE).
    """
    triple_type: Literal["node", "edge"] = Field(..., description="Whether this triple creates a node or an edge")

    # For node triples
    node_data: Optional[dict] = Field(default=None, description="Serialized FunctionNode or ClassNode")
    node_label: Optional[str] = Field(default=None, description="'Function' or 'Class'")

    # For edge triples
    edge_data: Optional[dict] = Field(default=None, description="Serialized Edge")

    @classmethod
    def from_function(cls, node: FunctionNode) -> Triple:
        """Create a node triple from a FunctionNode."""
        return cls(
            triple_type="node",
            node_data=node.model_dump(exclude={"node_label"}),
            node_label="Function",
        )

    @classmethod
    def from_class(cls, node: ClassNode) -> Triple:
        """Create a node triple from a ClassNode."""
        return cls(
            triple_type="node",
            node_data=node.model_dump(exclude={"node_label"}),
            node_label="Class",
        )

    @classmethod
    def from_edge(cls, edge: Edge) -> Triple:
        """Create an edge triple from an Edge."""
        return cls(
            triple_type="edge",
            edge_data=edge.model_dump(),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_usr(fqn: str, file: str, line: int) -> str:
    """
    Generate a synthetic USR for languages without native USR support (Java).
    Uses SHA-256 of (FQN + file + line) for deterministic, collision-resistant IDs.
    """
    content = f"{fqn}::{file}::{line}"
    return f"java@{hashlib.sha256(content.encode()).hexdigest()[:24]}"
