#!/usr/bin/env python3
"""
Convert ndk-build V=1 -n output to compile_commands.json

Usage:
    python3 tools/gen_compdb.py build_commands.txt /path/to/project/root

The script:
  1. Reads the ndk-build verbose dry-run output
  2. Extracts lines that invoke clang/clang++ with -c (compilation, not linking)
  3. Parses out the source file and all compiler arguments
  4. Writes a standard compile_commands.json

The resulting compile_commands.json can be used by OmniGraph, clangd, clang-tidy,
or any tool that supports the JSON compilation database format.
"""

import json
import os
import re
import sys


# Source file extensions we care about
SOURCE_EXTS = {".cpp", ".cc", ".cxx", ".c"}


def parse_build_log(log_path: str, project_dir: str) -> list[dict]:
    """Parse ndk-build V=1 -n output into compile_commands.json entries."""
    entries = []
    seen = set()  # deduplicate by source file

    with open(log_path, errors="replace") as f:
        for line in f:
            line = line.strip()

            # Skip empty lines and non-clang lines
            if not line:
                continue
            if not re.search(r"clang(\+\+)?\s", line):
                continue

            # Must be a compilation command (has -c flag), not linking
            if " -c " not in line:
                continue

            parts = line.split()

            # Find the source file: the argument immediately after -c
            src_file = None
            for i, part in enumerate(parts):
                if part == "-c" and i + 1 < len(parts):
                    candidate = parts[i + 1]
                    ext = os.path.splitext(candidate)[1].lower()
                    if ext in SOURCE_EXTS:
                        src_file = candidate
                    break

            if not src_file:
                # Fallback: find any source file in the args
                for part in parts:
                    ext = os.path.splitext(part)[1].lower()
                    if ext in SOURCE_EXTS and not part.startswith("-"):
                        src_file = part
                        break

            if not src_file:
                continue

            # Make source path absolute
            if not os.path.isabs(src_file):
                src_file = os.path.join(project_dir, src_file)
            src_file = os.path.normpath(src_file)

            # Deduplicate (same file might appear for different build variants)
            if src_file in seen:
                continue
            seen.add(src_file)

            # Clean up arguments: remove -o and its argument (not needed for parsing)
            clean_args = []
            skip_next = False
            for part in parts:
                if skip_next:
                    skip_next = False
                    continue
                if part == "-o":
                    skip_next = True
                    continue
                clean_args.append(part)

            entries.append({
                "directory": project_dir,
                "arguments": clean_args,
                "file": src_file,
            })

    return entries


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 gen_compdb.py <build_commands.txt> [project_root]")
        print()
        print("  build_commands.txt  Output from: ndk-build V=1 -n")
        print("  project_root        Project root dir (default: current dir)")
        sys.exit(1)

    log_file = sys.argv[1]
    project_dir = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")

    if not os.path.isfile(log_file):
        print(f"Error: {log_file} not found")
        sys.exit(1)

    entries = parse_build_log(log_file, project_dir)

    output = os.path.join(project_dir, "compile_commands.json")
    with open(output, "w") as f:
        json.dump(entries, f, indent=2)

    print(f"Generated {output}")
    print(f"  Total compilation entries: {len(entries)}")
    print()

    if entries:
        # Show a preview
        print("Preview (first 5 files):")
        for e in entries[:5]:
            basename = os.path.basename(e["file"])
            num_args = len(e["arguments"])
            print(f"  {basename}: {num_args} args")
        if len(entries) > 5:
            print(f"  ... and {len(entries) - 5} more")

        # Show a sample of flags from the first entry
        print()
        print("Sample flags from first entry:")
        sample = entries[0]["arguments"]
        for arg in sample:
            if arg.startswith(("-I", "-isystem", "-D", "--target", "--sysroot", "-std")):
                print(f"  {arg}")
    else:
        print("WARNING: No compilation commands found!")
        print("Make sure build_commands.txt contains clang/clang++ lines with -c flag")


if __name__ == "__main__":
    main()
