"""
OmniGraph Orchestrator

Multi-processing task distribution engine. Three-phase pipeline:
  Phase 1: Parallel file parsing (C++ and Java) via Pool.imap_unordered
  Phase 2: Java Pass 2 resolution (sequential, requires full GST)
  Phase 3: Neo4j batch ingestion from JSONL shards

Memory-efficient: streams results, processes file-by-file,
releases resources immediately.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from src.core.database import Neo4jIngester
from src.parsers.cpp.parser import CppParser
from src.parsers.cpp.resolver import CppResolver
from src.parsers.java.parser import JavaParser
from src.parsers.java.solver import JavaSolver
from src.utils.hasher import FileHasher
from src.utils.symbols import GlobalSymbolTable

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestration pipeline."""
    source_root: str
    workers: int = field(default_factory=lambda: min(mp.cpu_count(), 8))
    incremental: bool = False
    batch_size: int = 10000
    languages: list[str] = field(default_factory=lambda: ["cpp", "java"])
    cpp_include_flags: list[str] = field(default_factory=list)
    cpp_compile_args: list[str] = field(default_factory=lambda: ["-std=c++17"])
    shard_dir: str = "data/shards"
    cache_dir: str = "data/cache"
    db_config_path: str = "configs/db_config.json"
    clean: bool = False
    cpp_extensions: list[str] = field(
        default_factory=lambda: [".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"]
    )
    java_extensions: list[str] = field(default_factory=lambda: [".java"])
    auto_system_includes: bool = True
    ndk_config_path: str = ""
    compile_commands_path: str = ""

# ---- Clang resource directory detection ----

def _find_clang_resource_include(compiler_path: str) -> str | None:
    """Find the Clang resource include directory from a compiler binary path.

    The resource dir contains compiler builtins (stddef.h, stdarg.h, etc.)
    that the real compiler finds implicitly but libclang does not.

    Typical NDK layout:
        .../toolchains/llvm/prebuilt/<host>/bin/clang++
        .../toolchains/llvm/prebuilt/<host>/lib/clang/<ver>/include/stddef.h
        or .../lib64/clang/<ver>/include/stddef.h
    """
    import glob

    # Resolve symlinks to get the real path
    compiler_path = os.path.realpath(compiler_path)
    bin_dir = os.path.dirname(compiler_path)
    toolchain_root = os.path.dirname(bin_dir)  # parent of bin/

    # Search for resource include dir in known locations
    search_patterns = [
        os.path.join(toolchain_root, "lib", "clang", "*", "include"),
        os.path.join(toolchain_root, "lib64", "clang", "*", "include"),
    ]

    for pattern in search_patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            # Use the latest version (last in sorted order)
            candidate = matches[-1]
            if os.path.isfile(os.path.join(candidate, "stddef.h")):
                return candidate

    return None


# ---- Top-level worker functions (must be picklable) ----

def _parse_cpp_file(args: tuple) -> dict:
    """Worker: parse a single C++ file."""
    filepath, shard_path, compile_args, auto_system_includes, source_root = args
    try:
        parser = CppParser(compile_args=compile_args, auto_system_includes=auto_system_includes)
        return parser.parse_file(filepath, shard_path, source_root=source_root)
    except Exception as e:
        return {"nodes": 0, "edges": 0, "symbols": [],
                "errors": [f"Worker crash on {filepath}: {e}"]}


def _parse_java_file(args: tuple) -> dict:
    """Worker: parse a single Java file."""
    filepath, shard_path = args
    try:
        parser = JavaParser()
        return parser.parse_file(filepath, shard_path)
    except Exception as e:
        return {"nodes": 0, "edges": 0, "symbols": [],
                "unresolved_calls": [],
                "errors": [f"Worker crash on {filepath}: {e}"]}


class Orchestrator:
    """
    Main orchestration engine for OmniGraph.

    Coordinates three phases:
      1. Parse all source files in parallel
      2. Resolve Java method invocations (Pass 2)
      3. Ingest JSONL shards into Neo4j
    """

    def __init__(self, config: OrchestratorConfig):
        self.config = config
        self.gst: Optional[GlobalSymbolTable] = None
        self.hasher: Optional[FileHasher] = None
        Path(self.config.shard_dir).mkdir(parents=True, exist_ok=True)
        Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)

    def run(self) -> dict:
        """Execute the full three-phase pipeline. Returns summary stats."""
        start_time = time.time()
        summary = {
            "total_files": 0, "parsed_files": 0, "skipped_files": 0,
            "total_nodes": 0, "total_edges": 0,
            "resolved_calls": 0, "unresolved_calls": 0,
            "virtual_dispatch_edges": 0,
            "errors": [], "duration_seconds": 0,
        }

        logger.info("=" * 60)
        logger.info("OmniGraph Ingestion Pipeline")
        logger.info("  Source root : %s", self.config.source_root)
        logger.info("  Workers     : %d", self.config.workers)
        logger.info("  Incremental : %s", self.config.incremental)
        logger.info("=" * 60)

        gst_path = Path(self.config.cache_dir) / "global_symbols.db"
        hash_path = Path(self.config.cache_dir) / "file_hashes.db"

        # Clean mode: wipe everything
        if self.config.clean:
            logger.info("Clean mode: wiping caches and shards")
            for f in Path(self.config.shard_dir).glob("*.jsonl"):
                f.unlink()
            for p in [gst_path, hash_path]:
                if p.exists():
                    p.unlink()

        self.gst = GlobalSymbolTable(db_path=gst_path)
        self.hasher = FileHasher(db_path=hash_path)

        try:
            # Step 1: Discover files
            cpp_files, java_files = self._discover_files()
            summary["total_files"] = len(cpp_files) + len(java_files)
            logger.info("Discovered %d C++ + %d Java = %d files",
                        len(cpp_files), len(java_files), summary["total_files"])

            if summary["total_files"] == 0:
                logger.warning("No source files found under %s", self.config.source_root)
                return summary

            # Step 2: Incremental filter
            if self.config.incremental:
                all_files = cpp_files + java_files
                changed = set(self.hasher.get_changed_files(all_files))
                before = len(cpp_files) + len(java_files)
                cpp_files = [f for f in cpp_files if f in changed]
                java_files = [f for f in java_files if f in changed]
                summary["skipped_files"] = before - len(cpp_files) - len(java_files)
                logger.info("Incremental: parsing %d, skipping %d",
                            len(cpp_files) + len(java_files), summary["skipped_files"])

            # Phase 1: Parallel parsing
            logger.info("--- Phase 1: Parsing ---")
            p1 = self._phase1_parse(cpp_files, java_files)
            summary["parsed_files"] = p1["parsed_files"]
            summary["total_nodes"] = p1["total_nodes"]
            summary["total_edges"] = p1["total_edges"]
            summary["errors"].extend(p1["errors"])

            # Register symbols in GST
            if p1["symbols"]:
                self.gst.bulk_register(p1["symbols"])
                logger.info("Registered %d symbols in GST", len(p1["symbols"]))

            # Phase 2: Java Pass 2
            if p1.get("unresolved_calls"):
                logger.info("--- Phase 2: Java Call Resolution ---")
                r = self._phase2_resolve(p1["unresolved_calls"])
                summary["resolved_calls"] = r["resolved"]
                summary["unresolved_calls"] = r["unresolved"]
                summary["total_edges"] += r["resolved"] + r["unresolved"]
                summary["errors"].extend(r.get("errors", []))

            # Phase 2b: C++ cross-file overrides
            if "cpp" in self.config.languages and cpp_files:
                logger.info("--- Phase 2b: C++ Override Resolution ---")
                resolver = CppResolver(self.gst)
                shard = os.path.join(self.config.shard_dir, "overrides_resolved.jsonl")
                ov = resolver.resolve_cross_file_overrides(shard)
                summary["total_edges"] += ov["overrides_added"]

                # Phase 2c: Virtual dispatch resolution (requires OVERRIDES edges)
                logger.info("--- Phase 2c: C++ Virtual Dispatch Resolution ---")
                vd_shard = os.path.join(self.config.shard_dir, "virtual_dispatch.jsonl")
                vd = resolver.resolve_virtual_dispatch(vd_shard)
                summary["total_edges"] += vd["dispatch_edges_added"]
                summary["virtual_dispatch_edges"] = vd["dispatch_edges_added"]
                summary["errors"].extend(vd.get("errors", []))

            # Phase 3: Neo4j ingestion
            logger.info("--- Phase 3: Neo4j Ingestion ---")
            ing = self._phase3_ingest()
            summary["errors"].extend(ing.get("errors", []))

            # Update hashes for successfully parsed files
            if self.config.incremental:
                self.hasher.bulk_update(cpp_files + java_files)

        finally:
            if self.gst:
                self.gst.close()
            if self.hasher:
                self.hasher.close()

        summary["duration_seconds"] = round(time.time() - start_time, 2)
        self._print_summary(summary)
        return summary

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _discover_files(self) -> tuple[list[str], list[str]]:
        cpp_files, java_files = [], []
        for dirpath, _, filenames in os.walk(self.config.source_root):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                ext = os.path.splitext(fname)[1].lower()
                if "cpp" in self.config.languages and ext in self.config.cpp_extensions:
                    cpp_files.append(fpath)
                elif "java" in self.config.languages and ext in self.config.java_extensions:
                    java_files.append(fpath)
        return cpp_files, java_files

    # ------------------------------------------------------------------
    # Phase 1: Parallel parsing
    # ------------------------------------------------------------------

    def _phase1_parse(self, cpp_files: list[str], java_files: list[str]) -> dict:
        stats = {
            "parsed_files": 0, "total_nodes": 0, "total_edges": 0,
            "symbols": [], "unresolved_calls": [], "errors": [],
        }
        ts = int(time.time())

        # ── Compilation Database mode ─────────────────────────────────────
        # When a compile_commands.json is provided, use per-file flags
        # directly from the build system. This supersedes ndk_config,
        # include_flags, compile_args, and auto_system_includes.
        compdb = None
        if self.config.compile_commands_path:
            compdb = self._load_compile_commands(self.config.compile_commands_path)
            if compdb:
                logger.info(
                    "Loaded compilation database: %d entries from %s",
                    len(compdb), self.config.compile_commands_path,
                )
            else:
                logger.warning(
                    "compile_commands.json loaded but empty: %s — falling back to global flags",
                    self.config.compile_commands_path,
                )

        if compdb:
            # Build per-file work items from compdb
            # Auto system includes disabled — compdb already has all paths
            cpp_work = []
            matched, unmatched = 0, 0
            for i, fp in enumerate(cpp_files):
                fp_abs = os.path.abspath(fp)
                file_args = compdb.get(fp_abs)
                if file_args:
                    matched += 1
                    cpp_work.append((
                        fp,
                        os.path.join(self.config.shard_dir, f"cpp_{i}_{ts}.jsonl"),
                        file_args,
                        False,  # auto_system_includes = False (compdb has everything)
                        self.config.source_root,
                    ))
                else:
                    unmatched += 1
                    logger.debug("File not in compdb, skipping: %s", fp)

            if unmatched:
                logger.info(
                    "compdb: %d files matched, %d files not in compdb (skipped)",
                    matched, unmatched,
                )
        else:
            # ── Legacy mode: global flags ─────────────────────────────────
            # Merge include flags (-I paths) with compile args into a single list
            merged_cpp_args = list(self.config.cpp_compile_args)
            for inc in self.config.cpp_include_flags:
                if inc.startswith("-I"):
                    merged_cpp_args.append(inc)
                else:
                    merged_cpp_args.append(f"-I{inc}")

            # Load NDK args if ndk_config is specified
            if self.config.ndk_config_path:
                from src.utils.ndk_args_builder import NdkArgsBuilder
                try:
                    ndk_builder = NdkArgsBuilder(self.config.ndk_config_path)
                    validation_errors = ndk_builder.validate()
                    if validation_errors:
                        for err in validation_errors:
                            logger.error("NDK config error: %s", err)
                            stats["errors"].append(f"NDK config error: {err}")
                    else:
                        ndk_args = ndk_builder.build_args()
                        logger.info("NDK config loaded: %s", ndk_builder.summary().replace('\n', ', '))
                        logger.debug("NDK args: %s", ndk_args)
                        # NDK args go first (target, sysroot), then user args
                        merged_cpp_args = ndk_args + merged_cpp_args
                except Exception as e:
                    logger.error("Failed to load NDK config: %s", e)
                    stats["errors"].append(f"Failed to load NDK config: {e}")

            auto_sys = self.config.auto_system_includes

            cpp_work = [
                (fp, os.path.join(self.config.shard_dir, f"cpp_{i}_{ts}.jsonl"),
                 merged_cpp_args, auto_sys, self.config.source_root)
                for i, fp in enumerate(cpp_files)
            ]

        java_work = [
            (fp, os.path.join(self.config.shard_dir, f"java_{i}_{ts}.jsonl"))
            for i, fp in enumerate(java_files)
        ]

        # Parse C++
        if cpp_work:
            logger.info("Parsing %d C++ files (%d workers)", len(cpp_work), self.config.workers)
            with mp.Pool(processes=self.config.workers) as pool:
                for result in tqdm(
                    pool.imap_unordered(_parse_cpp_file, cpp_work),
                    total=len(cpp_work), desc="C++ Parsing", unit="file",
                ):
                    stats["parsed_files"] += 1
                    stats["total_nodes"] += result.get("nodes", 0)
                    stats["total_edges"] += result.get("edges", 0)
                    stats["symbols"].extend(result.get("symbols", []))
                    stats["errors"].extend(result.get("errors", []))

        # Parse Java
        if java_work:
            logger.info("Parsing %d Java files (%d workers)", len(java_work), self.config.workers)
            with mp.Pool(processes=self.config.workers) as pool:
                for result in tqdm(
                    pool.imap_unordered(_parse_java_file, java_work),
                    total=len(java_work), desc="Java Parsing", unit="file",
                ):
                    stats["parsed_files"] += 1
                    stats["total_nodes"] += result.get("nodes", 0)
                    stats["total_edges"] += result.get("edges", 0)
                    stats["symbols"].extend(result.get("symbols", []))
                    stats["unresolved_calls"].extend(result.get("unresolved_calls", []))
                    stats["errors"].extend(result.get("errors", []))

        return stats

    # ------------------------------------------------------------------
    # Compilation Database loader
    # ------------------------------------------------------------------

    @staticmethod
    def _load_compile_commands(path: str) -> dict[str, list[str]] | None:
        """Load compile_commands.json and return {abs_filepath: [compiler_args]}.

        Strips the compiler binary, -c, -o <output>, and source file from
        the arguments, returning only the flags relevant for libclang parsing.
        Also auto-detects the Clang resource include directory (stddef.h, stdarg.h)
        from the compiler binary path and injects it.
        """
        import glob
        import json as _json

        compdb_path = Path(path)
        if not compdb_path.exists():
            logger.error("compile_commands.json not found: %s", path)
            return None

        try:
            with open(compdb_path) as f:
                entries = _json.load(f)
        except Exception as e:
            logger.error("Failed to parse compile_commands.json: %s", e)
            return None

        if not entries:
            return None

        result: dict[str, list[str]] = {}
        compiler_binary = None

        for entry in entries:
            filepath = entry.get("file", "")
            directory = entry.get("directory", "")

            # Make filepath absolute
            if not os.path.isabs(filepath):
                filepath = os.path.join(directory, filepath)
            filepath = os.path.normpath(filepath)

            # Get arguments (prefer "arguments" list, fall back to "command" string)
            args = entry.get("arguments")
            if args is None:
                command = entry.get("command", "")
                args = command.split() if command else []

            if not args:
                continue

            # Save the compiler binary path from the first entry
            if compiler_binary is None:
                compiler_binary = args[0]

            # Strip: compiler binary (first arg), -c, -o <output>, source file
            clean_args = []
            skip_next = False
            for i, arg in enumerate(args):
                if i == 0:
                    continue  # Skip compiler binary
                if skip_next:
                    skip_next = False
                    continue
                if arg == "-c":
                    continue
                if arg == "-o":
                    skip_next = True
                    continue
                # Skip the source file itself
                if os.path.normpath(os.path.join(directory, arg)) == filepath:
                    continue
                if arg == filepath:
                    continue
                clean_args.append(arg)

            result[filepath] = clean_args

        # Auto-detect Clang resource include directory (contains stddef.h, stdarg.h).
        # The real compiler finds these implicitly, but libclang doesn't.
        resource_include = None
        if compiler_binary:
            resource_include = _find_clang_resource_include(compiler_binary)

        if resource_include:
            logger.info("Auto-detected Clang resource include: %s", resource_include)
            for filepath in result:
                result[filepath] = ["-isystem", resource_include] + result[filepath]
        else:
            logger.warning(
                "Could not detect Clang resource include dir (stddef.h, stdarg.h "
                "may not resolve). Set -isystem manually if needed."
            )

        return result

    # ------------------------------------------------------------------
    # Phase 2: Java resolution
    # ------------------------------------------------------------------

    def _phase2_resolve(self, unresolved_calls: list[dict]) -> dict:
        solver = JavaSolver(self.gst)
        shard = os.path.join(self.config.shard_dir, f"java_resolved_{int(time.time())}.jsonl")
        return solver.resolve_calls(unresolved_calls, shard)

    # ------------------------------------------------------------------
    # Phase 3: Neo4j ingestion
    # ------------------------------------------------------------------

    def _phase3_ingest(self) -> dict:
        try:
            ingester = Neo4jIngester(config_path=self.config.db_config_path)
        except Exception as e:
            logger.error("Failed to connect to Neo4j: %s", e)
            return {"errors": [f"Neo4j connection failed: {e}"]}
        try:
            ingester.ensure_constraints()
            return ingester.ingest_shards(
                shard_dir=self.config.shard_dir,
                batch_size=self.config.batch_size,
            )
        finally:
            ingester.close()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    @staticmethod
    def _print_summary(s: dict) -> None:
        print("\n" + "=" * 60)
        print("  OmniGraph Ingestion Summary")
        print("=" * 60)
        print(f"  Files discovered:      {s['total_files']}")
        print(f"  Files parsed:          {s['parsed_files']}")
        print(f"  Files skipped (cache): {s['skipped_files']}")
        print(f"  Nodes created:         {s['total_nodes']}")
        print(f"  Edges created:         {s['total_edges']}")
        print(f"  Java calls resolved:   {s['resolved_calls']}")
        print(f"  Java calls unresolved: {s['unresolved_calls']}")
        print(f"  Virtual dispatch edges:{s['virtual_dispatch_edges']}")
        print(f"  Errors:                {len(s['errors'])}")
        print(f"  Duration:              {s['duration_seconds']}s")
        print("=" * 60)

        if s["errors"]:
            log_path = Path("data/cache/parse_errors.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w") as f:
                for err in s["errors"]:
                    f.write(err + "\n")
            print(f"  Error log: {log_path}")
