#!/usr/bin/env python3
"""
OmniGraph — Auto-Generate ndk_config.json from NDK Build Logs

Parses verbose Gradle/CMake/ndk-build output to extract compiler flags
and auto-generates a complete ndk_config.json.

Usage:
    # 1. Capture a verbose build log:
    ./gradlew assembleDeviceCameraRelease --info 2>&1 | tee build.log

    # 2. Generate config from the log:
    python scripts/parse_build_log.py build.log -o configs/ndk_config.json

    # Or pipe directly:
    ./gradlew assembleDeviceCameraRelease --info 2>&1 | python scripts/parse_build_log.py - -o configs/ndk_config.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path


def find_clang_commands(lines: list[str]) -> list[list[str]]:
    """
    Find all clang/clang++ compiler invocation lines in the build log.

    Handles:
      - Direct clang++ invocations
      - NDK toolchain paths like .../bin/clang++
      - Lines split across multiple lines (continuation)
    """
    commands: list[list[str]] = []

    # Pattern to match clang/clang++ invocations
    # Matches: clang++, clang, /full/path/to/clang++, etc.
    clang_pattern = re.compile(
        r'(?:^|\s|/)(clang\+\+|clang)\s+'
        r'.*(?:--target=|--sysroot=|-c\s)',
        re.MULTILINE,
    )

    full_text = "\n".join(lines)

    # Also try to find lines that contain the full NDK clang path
    ndk_clang_pattern = re.compile(
        r'(/[^\s]*?/toolchains/llvm/prebuilt/[^\s]*/bin/clang\+?\+?)\s+(.+)',
    )

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check for NDK clang path (most reliable match)
        ndk_match = ndk_clang_pattern.search(line)
        if ndk_match:
            try:
                full_cmd = ndk_match.group(1) + " " + ndk_match.group(2)
                tokens = shlex.split(full_cmd)
                commands.append(tokens)
            except ValueError:
                # shlex couldn't parse — try basic split
                commands.append(line.split())
            continue

        # Check for generic clang invocation with compile flags
        if clang_pattern.search(line):
            try:
                tokens = shlex.split(line)
                # Find where clang/clang++ starts
                for i, tok in enumerate(tokens):
                    if tok.endswith("clang") or tok.endswith("clang++"):
                        commands.append(tokens[i:])
                        break
            except ValueError:
                pass

    return commands


def extract_flags(commands: list[list[str]]) -> dict:
    """
    Extract and deduplicate compiler flags from clang command lines.

    Returns a dict matching the ndk_config.json schema.
    """
    targets: set[str] = set()
    sysroots: set[str] = set()
    include_paths: set[str] = set()
    system_includes: set[str] = set()
    defines: set[str] = set()
    standards: set[str] = set()
    extra_args: set[str] = set()
    ndk_roots: set[str] = set()

    for tokens in commands:
        i = 0
        while i < len(tokens):
            tok = tokens[i]

            # --target=aarch64-linux-android29
            if tok.startswith("--target="):
                targets.add(tok.split("=", 1)[1])

            # --sysroot=/path/to/sysroot
            elif tok.startswith("--sysroot="):
                sysroot = tok.split("=", 1)[1]
                sysroots.add(sysroot)
                # Derive NDK root from sysroot path
                # sysroot = <ndk>/toolchains/llvm/prebuilt/<host>/sysroot
                ndk_root = _derive_ndk_root(sysroot)
                if ndk_root:
                    ndk_roots.add(ndk_root)
            elif tok == "--sysroot" and i + 1 < len(tokens):
                sysroot = tokens[i + 1]
                sysroots.add(sysroot)
                ndk_root = _derive_ndk_root(sysroot)
                if ndk_root:
                    ndk_roots.add(ndk_root)
                i += 1

            # -I /path or -I/path
            elif tok == "-I" and i + 1 < len(tokens):
                include_paths.add(tokens[i + 1])
                i += 1
            elif tok.startswith("-I") and len(tok) > 2:
                include_paths.add(tok[2:])

            # -isystem /path or -isystem/path
            elif tok == "-isystem" and i + 1 < len(tokens):
                system_includes.add(tokens[i + 1])
                i += 1
            elif tok.startswith("-isystem") and len(tok) > 8:
                system_includes.add(tok[8:])

            # -D DEFINE or -DDEFINE
            elif tok == "-D" and i + 1 < len(tokens):
                defines.add(tokens[i + 1])
                i += 1
            elif tok.startswith("-D") and len(tok) > 2:
                defines.add(tok[2:])

            # -std=c++17
            elif tok.startswith("-std="):
                standards.add(tok.split("=", 1)[1])

            # --gcc-toolchain (skip, not needed for libclang)
            elif tok.startswith("--gcc-toolchain"):
                pass

            # -f flags that might matter
            elif tok in ("-fno-exceptions", "-fno-rtti", "-fPIC"):
                extra_args.add(tok)

            i += 1

    # Parse target triple to extract arch and API level
    arch = "aarch64"
    api_level = 21
    if targets:
        target = next(iter(targets))
        arch, api_level = _parse_target_triple(target)

    # Separate project includes from sysroot includes
    # Paths inside sysroot or NDK are "system", others are "project"
    project_includes: list[str] = []
    extra_system: list[str] = []

    ndk_root_str = next(iter(ndk_roots), "")
    for path in sorted(include_paths):
        if ndk_root_str and path.startswith(ndk_root_str):
            extra_system.append(path)
        else:
            project_includes.append(path)

    for path in sorted(system_includes):
        if ndk_root_str and path.startswith(ndk_root_str):
            # Skip NDK sysroot paths — NdkArgsBuilder adds these automatically
            continue
        extra_system.append(path)

    return {
        "ndk_root": ndk_root_str,
        "api_level": api_level,
        "target_arch": arch,
        "cpp_standard": next(iter(standards), "c++17"),
        "project_include_paths": project_includes,
        "extra_system_includes": sorted(set(extra_system)),
        "defines": sorted(defines),
        "extra_compile_args": sorted(extra_args),
    }


def _derive_ndk_root(sysroot: str) -> str | None:
    """
    Derive NDK root from sysroot path.

    sysroot format: <ndk>/toolchains/llvm/prebuilt/<host>/sysroot
    """
    # Walk up from sysroot to find the NDK root
    parts = sysroot.split(os.sep)
    try:
        tc_idx = parts.index("toolchains")
        return os.sep.join(parts[:tc_idx])
    except ValueError:
        return None


def _parse_target_triple(target: str) -> tuple[str, int]:
    """
    Parse a clang target triple like 'aarch64-linux-android29'.

    Returns (arch_key, api_level).
    """
    # Extract arch
    arch_map = {
        "aarch64": "aarch64",
        "armv7a": "armv7a",
        "arm": "armv7a",
        "x86_64": "x86_64",
        "i686": "i686",
        "i386": "i686",
    }

    arch = "aarch64"
    for prefix, key in arch_map.items():
        if target.startswith(prefix):
            arch = key
            break

    # Extract API level (trailing digits)
    api_match = re.search(r'(\d+)$', target)
    api_level = int(api_match.group(1)) if api_match else 21

    return arch, api_level


def main():
    parser = argparse.ArgumentParser(
        description="Auto-generate ndk_config.json from NDK build logs.",
        epilog=(
            "Example:\n"
            "  ./gradlew assembleDeviceCameraRelease --info 2>&1 | tee build.log\n"
            "  python scripts/parse_build_log.py build.log -o configs/ndk_config.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "log_file",
        help="Path to build log file, or '-' for stdin",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output path for ndk_config.json (default: stdout)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show extracted flags without writing config",
    )

    args = parser.parse_args()

    # Read input
    if args.log_file == "-":
        lines = sys.stdin.readlines()
    else:
        log_path = Path(args.log_file)
        if not log_path.exists():
            print(f"ERROR: Build log not found: {args.log_file}", file=sys.stderr)
            return 1
        with open(log_path) as f:
            lines = f.readlines()

    # Find clang commands
    commands = find_clang_commands(lines)

    if not commands:
        print(
            "ERROR: No clang/clang++ compiler commands found in the build log.\n"
            "\n"
            "Make sure you captured a verbose build:\n"
            "  ./gradlew assembleDeviceCameraRelease --info 2>&1 | tee build.log\n"
            "\n"
            "Or for CMake builds:\n"
            "  cmake --build . -- VERBOSE=1 2>&1 | tee build.log\n"
            "\n"
            "Or for ndk-build:\n"
            "  ndk-build V=1 2>&1 | tee build.log",
            file=sys.stderr,
        )
        return 1

    print(f"Found {len(commands)} clang command(s) in build log", file=sys.stderr)

    # Extract flags
    config = extract_flags(commands)

    if args.dry_run:
        print("\n=== Extracted Configuration ===", file=sys.stderr)
        print(f"NDK Root:      {config['ndk_root']}", file=sys.stderr)
        print(f"API Level:     {config['api_level']}", file=sys.stderr)
        print(f"Target Arch:   {config['target_arch']}", file=sys.stderr)
        print(f"C++ Standard:  {config['cpp_standard']}", file=sys.stderr)
        print(f"Project Includes: {len(config['project_include_paths'])}", file=sys.stderr)
        print(f"System Includes:  {len(config['extra_system_includes'])}", file=sys.stderr)
        print(f"Defines:          {len(config['defines'])}", file=sys.stderr)
        print(f"Extra Args:       {len(config['extra_compile_args'])}", file=sys.stderr)
        print("\n=== Generated Config ===", file=sys.stderr)

    # Output
    config_json = json.dumps(config, indent=4)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            f.write(config_json + "\n")
        print(f"Config written to: {args.output}", file=sys.stderr)
    else:
        print(config_json)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
