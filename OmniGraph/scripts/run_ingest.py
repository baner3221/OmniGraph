#!/usr/bin/env python3
"""
OmniGraph — One-Click Ingestion Entry Point

Usage:
    python scripts/run_ingest.py --source-root /path/to/codebase
    python scripts/run_ingest.py --source-root /path --incremental --workers 4
    python scripts/run_ingest.py --source-root /path --clean --languages cpp,java
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.orchestrator import Orchestrator, OrchestratorConfig


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with appropriate level and format."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy loggers
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_build_context(config_path: str) -> dict:
    """Load build context configuration."""
    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OmniGraph — High-Fidelity Knowledge Graph Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full index of a codebase
  python scripts/run_ingest.py --source-root /path/to/code

  # Incremental re-index with 4 workers
  python scripts/run_ingest.py --source-root /path --incremental --workers 4

  # Clean re-index, Java only
  python scripts/run_ingest.py --source-root /path --clean --languages java

  # With include paths and compiler flags
  python scripts/run_ingest.py --source-root /path \\
      --include-flags /path/to/include /path/to/other/include \\
      --compile-args -std=c++17 -DANDROID
        """,
    )

    parser.add_argument(
        "--source-root",
        required=True,
        help="Root directory of the codebase to index",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        default=False,
        help="Skip unchanged files (uses SHA-256 hash cache)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of parallel parser workers (default: cpu_count, max 8)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
        help="Neo4j UNWIND batch size (default: 10000)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="Wipe Neo4j DB and all caches before indexing",
    )
    parser.add_argument(
        "--languages",
        type=str,
        default="cpp,java",
        help="Comma-separated list of languages to parse (default: cpp,java)",
    )
    parser.add_argument(
        "--include-flags",
        nargs="*",
        default=None,
        help="Header search paths (e.g., /path/to/include). -I prefix added automatically",
    )
    parser.add_argument(
        "--compile-args",
        nargs="*",
        default=None,
        help="Compiler flags (e.g., -std=c++17 -DANDROID -DLOG_TAG=\\\"MyTag\\\")",
    )
    parser.add_argument(
        "--db-config",
        type=str,
        default="configs/db_config.yaml",
        help="Path to Neo4j config YAML",
    )
    parser.add_argument(
        "--build-context",
        type=str,
        default="configs/build_context.json",
        help="Path to build context JSON",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose (DEBUG) logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Validate source root
    source_root = Path(args.source_root).resolve()
    if not source_root.exists():
        print(f"ERROR: Source root does not exist: {source_root}", file=sys.stderr)
        return 1

    # Load build context (file-based defaults)
    build_ctx = load_build_context(args.build_context)
    cpp_ctx = build_ctx.get("cpp", {})

    # CLI flags override build_context.json values
    include_flags = args.include_flags if args.include_flags is not None else cpp_ctx.get("include_flags", [])
    compile_args = args.compile_args if args.compile_args is not None else cpp_ctx.get("compile_args", ["-std=c++17"])

    # Parse languages
    languages = [lang.strip() for lang in args.languages.split(",")]

    # Build config
    import multiprocessing as mp
    config = OrchestratorConfig(
        source_root=str(source_root),
        workers=args.workers or min(mp.cpu_count(), 8),
        incremental=args.incremental,
        batch_size=args.batch_size,
        languages=languages,
        cpp_include_flags=include_flags,
        cpp_compile_args=compile_args,
        db_config_path=args.db_config,
        clean=args.clean,
    )

    # Run pipeline
    orchestrator = Orchestrator(config)
    summary = orchestrator.run()

    # Exit code based on errors
    if summary.get("errors"):
        return 2 if summary.get("parsed_files", 0) > 0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
