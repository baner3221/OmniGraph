"""
OmniGraph — System Include Path Auto-Detection

Detects Clang's resource directory and system include paths so that
libclang can resolve standard C/C++ types (size_t, int32_t, float_t, etc.)
without the user manually specifying system header locations.

Detection strategy (layered fallback):
  1. `clang -print-resource-dir`   → Clang built-in headers
  2. `clang -v -x c++ -E -`       → Full system include search list
  3. Probe well-known Linux paths  → Fallback when clang isn't on PATH
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SystemIncludeDetector:
    """
    Auto-detects Clang system include paths for libclang.

    Returns a list of compiler flags (e.g., ['-isystem', '/usr/lib/clang/18/include'])
    that should be prepended to the compile_args passed to libclang.
    """

    def __init__(self, clang_binary: str = "clang"):
        self._clang = clang_binary
        self._cached_result: Optional[list[str]] = None

    def detect(self) -> list[str]:
        """
        Detect system include paths using a layered fallback strategy.

        Returns:
            List of compiler flags: ['-isystem', '/path1', '-isystem', '/path2', ...]
        """
        if self._cached_result is not None:
            return list(self._cached_result)

        paths: list[str] = []

        # Priority 1: Clang resource directory (stddef.h, stdint.h, float.h, etc.)
        resource_includes = self._from_resource_dir()
        if resource_includes:
            paths.extend(resource_includes)
            logger.info("Detected Clang resource dir includes: %d paths", len(resource_includes) // 2)

        # Priority 2: Full system include search list from clang -v
        verbose_includes = self._from_clang_verbose()
        if verbose_includes:
            # Deduplicate against what we already have
            existing = set(paths[i] for i in range(1, len(paths), 2))
            for i in range(0, len(verbose_includes), 2):
                path = verbose_includes[i + 1]
                if path not in existing:
                    paths.extend([verbose_includes[i], path])
                    existing.add(path)
            logger.info("Detected system includes via clang -v: %d additional paths",
                        (len(paths) - len(resource_includes)) // 2)

        # Priority 3: Well-known fallback paths
        if not paths:
            fallback_includes = self._from_well_known()
            if fallback_includes:
                paths.extend(fallback_includes)
                logger.info("Using well-known fallback includes: %d paths", len(fallback_includes) // 2)
            else:
                logger.warning(
                    "Could not detect any system include paths. "
                    "Standard types like size_t, int32_t may not resolve. "
                    "Install clang: sudo apt install clang"
                )

        self._cached_result = paths
        return list(paths)

    def _from_resource_dir(self) -> list[str]:
        """
        Priority 1: Get Clang's resource directory via `clang -print-resource-dir`.

        The resource directory contains Clang's built-in headers:
        stddef.h, stdint.h, float.h, stdarg.h, limits.h, etc.
        """
        try:
            result = subprocess.run(
                [self._clang, "-print-resource-dir"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                resource_dir = result.stdout.strip()
                include_dir = os.path.join(resource_dir, "include")
                if os.path.isdir(include_dir):
                    # Verify it actually has the headers we need
                    if os.path.isfile(os.path.join(include_dir, "stddef.h")):
                        logger.debug("Clang resource dir: %s", include_dir)
                        return ["-isystem", include_dir]
                    else:
                        logger.debug("Resource dir exists but missing stddef.h: %s", include_dir)
        except FileNotFoundError:
            logger.debug("clang binary not found on PATH: %s", self._clang)
        except subprocess.TimeoutExpired:
            logger.debug("clang -print-resource-dir timed out")
        except Exception as e:
            logger.debug("clang -print-resource-dir failed: %s", e)

        return []

    def _from_clang_verbose(self) -> list[str]:
        """
        Priority 2: Parse `clang -v -x c++ -E -` to get the full system
        include search list.

        This captures:
        - libc++ headers (/usr/include/c++/v1)
        - Platform headers (/usr/include)
        - Architecture-specific headers
        """
        try:
            result = subprocess.run(
                [self._clang, "-v", "-x", "c++", "-E", "-"],
                input="",
                capture_output=True, text=True, timeout=15,
            )
            # clang -v prints to stderr
            output = result.stderr

            # Extract paths between "#include <...> search starts here:" and "End of search list."
            in_search_list = False
            paths: list[str] = []

            for line in output.splitlines():
                if "#include <...> search starts here:" in line:
                    in_search_list = True
                    continue
                if "End of search list." in line:
                    break
                if in_search_list:
                    # Lines are indented paths
                    path = line.strip()
                    if path and os.path.isdir(path):
                        paths.append("-isystem")
                        paths.append(path)

            return paths

        except FileNotFoundError:
            logger.debug("clang binary not found for verbose include detection")
        except subprocess.TimeoutExpired:
            logger.debug("clang -v timed out")
        except Exception as e:
            logger.debug("clang -v failed: %s", e)

        return []

    def _from_well_known(self) -> list[str]:
        """
        Priority 3: Probe well-known Linux paths as a last resort.

        This handles environments where clang is not on PATH but
        libclang is installed via pip.
        """
        paths: list[str] = []

        # Common Linux Clang paths
        candidates = [
            "/usr/lib/clang",
            "/usr/lib64/clang",
            "/usr/local/lib/clang",
        ]

        # For each candidate base, find the newest version's include dir
        for base in candidates:
            if not os.path.isdir(base):
                continue
            # List version directories and pick the newest
            try:
                versions = sorted(
                    [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))],
                    key=lambda v: [int(x) for x in re.findall(r'\d+', v)],
                    reverse=True,
                )
                for ver in versions:
                    include_dir = os.path.join(base, ver, "include")
                    if os.path.isfile(os.path.join(include_dir, "stddef.h")):
                        paths.extend(["-isystem", include_dir])
                        logger.debug("Found Clang headers via well-known path: %s", include_dir)
                        break
            except Exception:
                continue

            if paths:
                break

        # C++ standard library headers (libstdc++ on Ubuntu/Debian)
        for cxx_path in ["/usr/include/c++", "/usr/include/x86_64-linux-gnu/c++"]:
            if os.path.isdir(cxx_path):
                try:
                    versions = sorted(os.listdir(cxx_path), reverse=True)
                    if versions:
                        paths.extend(["-isystem", os.path.join(cxx_path, versions[0])])
                except Exception:
                    pass
                break

        # C headers
        if os.path.isdir("/usr/include"):
            paths.extend(["-isystem", "/usr/include"])

        return paths


def has_system_includes(compile_args: list[str]) -> bool:
    """
    Check if the compile args already contain system include paths.

    Returns True if any -isystem flag is present, indicating the
    user has already configured system includes.
    """
    return any(arg.startswith("-isystem") for arg in compile_args)
