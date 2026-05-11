# OmniGraph

**High-Fidelity Knowledge Graph Engine** for indexing large-scale C++ and Java codebases into Neo4j.

Maps class hierarchies, function overloads/overrides, and caller-callee relationships across 20M+ lines of code.

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- Docker & Docker Compose

### 2. Setup

```bash
# Clone and enter
cd OmniGraph

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start Neo4j
docker compose up -d

# Wait for Neo4j to be ready (health check will report healthy)
docker compose ps
```

### 3. Configure

Edit `configs/build_context.json`:

```json
{
    "cpp": {
        "compile_commands_path": "/path/to/your/build/dir",
        "extra_args": ["-std=c++17"]
    },
    "java": {
        "source_roots": ["/path/to/java/src"]
    }
}
```

### 4. Run

```bash
# Full index
python scripts/run_ingest.py --source-root /path/to/codebase

# Incremental re-index
python scripts/run_ingest.py --source-root /path --incremental

# Java only, 4 workers
python scripts/run_ingest.py --source-root /path --languages java --workers 4

# Clean rebuild
python scripts/run_ingest.py --source-root /path --clean
```

### 5. Explore the Graph

Open Neo4j Browser at http://localhost:7474 (credentials: `neo4j` / `omnigraph_password`)

```cypher
-- Find all classes
MATCH (c:Class) RETURN c LIMIT 25;

-- Show inheritance hierarchy
MATCH (child:Class)-[:INHERITS_FROM]->(parent:Class)
RETURN child.fqn, parent.fqn LIMIT 50;

-- Trace callers of a function
MATCH (caller)-[:CALLS*1..3]->(target:Function {name: "processRequest"})
RETURN caller.fqn, target.fqn;

-- Impact analysis: what's affected by changing a function?
MATCH (f:Function {name: "handleEvent"})-[*1..3]-(affected)
RETURN DISTINCT affected.fqn, labels(affected);
```

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                    run_ingest.py (CLI)                      │
├────────────────────────────────────────────────────────────┤
│                  Orchestrator (multiprocessing)             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │  C++ Parser   │  │ Java Parser  │  │  Incremental     │ │
│  │  (libclang)   │  │ (javalang)   │  │  Hasher (SHA256) │ │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────┘ │
│         │                  │                                │
│         ▼                  ▼                                │
│  ┌────────────────────────────────────┐                    │
│  │     JSONL Shards (data/shards/)    │ ← Triple Stream    │
│  └──────────────┬─────────────────────┘                    │
│                 ▼                                           │
│  ┌────────────────────────────────────┐                    │
│  │  Neo4j Batch Ingester (UNWIND 10k) │                    │
│  └────────────────────────────────────┘                    │
├────────────────────────────────────────────────────────────┤
│  Global Symbol Table (SQLite, WAL mode)                    │
└────────────────────────────────────────────────────────────┘
```

## Graph Schema

| Node Label | Properties |
|-----------|-----------|
| `Function` | `usr`, `name`, `fqn`, `file`, `line`, `language`, `kind`, `signature`, `parent_fqn` |
| `Class` | `usr`, `fqn`, `name`, `namespace`, `file`, `line`, `language`, `kind` |

| Edge Type | Meaning |
|----------|---------|
| `CALLS` | Function A calls Function B |
| `INHERITS_FROM` | Class A extends/implements Class B |
| `DEFINES` | Class A defines Function B |
| `OVERRIDES` | Method A overrides Method B |

## Key Design Decisions

- **USR/FQN Identity**: C++ uses libclang's `cursor.get_usr()`, Java uses SHA-256 hash of FQN
- **Triple Shard Pattern**: Parsers write JSONL, never touch Neo4j directly
- **Memory O(1)**: File-by-file processing, no cross-file AST retention
- **Incremental**: SHA-256 hash cache in SQLite skips unchanged files
- **Error Tolerant**: Failed files are logged and skipped, never halt the pipeline
