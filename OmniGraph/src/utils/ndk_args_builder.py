"""
OmniGraph — NDK Compiler Args Builder

Reads an ndk_config.json file, validates the paths, and produces
the complete list of compiler flags for libclang to parse
Android NDK-based C++ code (e.g., Camera HAL shared libraries).

Usage:
    builder = NdkArgsBuilder("configs/ndk_config.json")
    errors = builder.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}")
    else:
        args = builder.build_args()
        # args is a list like: ['--target=aarch64-linux-android29', '--sysroot=...', ...]
"""

from __future__ import annotations

import json
import logging
import os
import platform
from pathlib import Path

logger = logging.getLogger(__name__)

# ABI name → (clang target triple prefix, NDK arch dir name)
_ARCH_MAP = {
    "aarch64": ("aarch64-linux-android", "aarch64-linux-android"),
    "armv7a":  ("armv7a-linux-androideabi", "arm-linux-androideabi"),
    "x86_64":  ("x86_64-linux-android", "x86_64-linux-android"),
    "i686":    ("i686-linux-android", "i686-linux-android"),
}


class NdkArgsBuilder:
    """
    Reads ndk_config.json and produces libclang-compatible compiler args.

    Generates:
      --target=<triple><api>
      --sysroot=<ndk>/toolchains/llvm/prebuilt/<host>/sysroot
      -isystem <sysroot>/usr/include
      -isystem <sysroot>/usr/include/<triple>
      -isystem <ndk>/...libc++/include
      -I <project_include_path>  (for each)
      -D <define>                (for each)
      -std=<cpp_standard>
      <extra_compile_args>
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load(config_path)

    def _load(self, config_path: str) -> dict:
        """Load and parse the JSON config file."""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"NDK config not found: {config_path}")

        with open(path) as f:
            data = json.load(f)

        return data

    def validate(self) -> list[str]:
        """
        Validate the NDK configuration.

        Returns:
            List of error messages. Empty list means config is valid.
        """
        errors: list[str] = []
        cfg = self.config

        # Required: ndk_root
        ndk_root = cfg.get("ndk_root", "")
        if not ndk_root:
            errors.append("ndk_root is required but empty")
        elif not os.path.isdir(ndk_root):
            errors.append(f"ndk_root directory does not exist: {ndk_root}")
        else:
            # Check for expected NDK structure
            toolchains = os.path.join(ndk_root, "toolchains")
            if not os.path.isdir(toolchains):
                errors.append(
                    f"ndk_root does not look like a valid NDK "
                    f"(missing toolchains/ dir): {ndk_root}"
                )

            # Check sysroot exists
            host_tag = self._detect_host_tag()
            sysroot = os.path.join(
                ndk_root, "toolchains", "llvm", "prebuilt", host_tag, "sysroot"
            )
            if not os.path.isdir(sysroot):
                errors.append(
                    f"NDK sysroot not found at expected path: {sysroot}. "
                    f"Detected host tag: {host_tag}"
                )

        # Required: target_arch
        arch = cfg.get("target_arch", "aarch64")
        if arch not in _ARCH_MAP:
            errors.append(
                f"Invalid target_arch: '{arch}'. "
                f"Must be one of: {', '.join(_ARCH_MAP.keys())}"
            )

        # Required: api_level
        api_level = cfg.get("api_level", 21)
        if not isinstance(api_level, int) or api_level < 16:
            errors.append(
                f"Invalid api_level: {api_level}. Must be an integer >= 16"
            )

        # Optional: validate project_include_paths exist
        for path in cfg.get("project_include_paths") or []:
            if not os.path.isdir(path):
                logger.warning("project_include_path does not exist: %s", path)

        # Optional: validate extra_system_includes exist
        for path in cfg.get("extra_system_includes") or []:
            if not os.path.isdir(path):
                logger.warning("extra_system_include does not exist: %s", path)

        return errors

    def build_args(self) -> list[str]:
        """
        Build the complete list of compiler flags for libclang.

        Returns:
            List of compiler flags.
        """
        cfg = self.config
        args: list[str] = []

        # ── Target and sysroot ───────────────────────────────────────────
        args.extend(self._target_flags())

        # ── NDK system includes ──────────────────────────────────────────
        args.extend(self._system_includes())

        # ── C++ standard ─────────────────────────────────────────────────
        cpp_std = cfg.get("cpp_standard", "c++17")
        args.append(f"-std={cpp_std}")

        # ── Project includes (-I) ────────────────────────────────────────
        args.extend(self._project_includes())

        # ── Extra system includes (-isystem) ─────────────────────────────
        args.extend(self._extra_system_includes())

        # ── Preprocessor defines (-D) ────────────────────────────────────
        args.extend(self._defines())

        # ── Extra raw compiler flags ─────────────────────────────────────
        args.extend(self._extra_args())

        return args

    def _target_flags(self) -> list[str]:
        """Generate --target and --sysroot flags."""
        cfg = self.config
        ndk_root = cfg.get("ndk_root", "")
        arch = cfg.get("target_arch", "aarch64")
        api_level = cfg.get("api_level", 21)

        triple_prefix, _ = _ARCH_MAP.get(arch, ("aarch64-linux-android", "aarch64-linux-android"))
        host_tag = self._detect_host_tag()
        sysroot = os.path.join(
            ndk_root, "toolchains", "llvm", "prebuilt", host_tag, "sysroot"
        )

        return [
            f"--target={triple_prefix}{api_level}",
            f"--sysroot={sysroot}",
        ]

    def _system_includes(self) -> list[str]:
        """Generate -isystem flags for NDK sysroot headers and libc++."""
        cfg = self.config
        ndk_root = cfg.get("ndk_root", "")
        arch = cfg.get("target_arch", "aarch64")

        _, arch_dir = _ARCH_MAP.get(arch, ("aarch64-linux-android", "aarch64-linux-android"))
        host_tag = self._detect_host_tag()
        sysroot = os.path.join(
            ndk_root, "toolchains", "llvm", "prebuilt", host_tag, "sysroot"
        )

        args: list[str] = []

        # Generic sysroot includes
        usr_include = os.path.join(sysroot, "usr", "include")
        if os.path.isdir(usr_include):
            args.extend(["-isystem", usr_include])

        # Architecture-specific includes
        arch_include = os.path.join(usr_include, arch_dir)
        if os.path.isdir(arch_include):
            args.extend(["-isystem", arch_include])

        # libc++ headers — try multiple known locations
        libcxx_candidates = [
            # NDK r23+ layout
            os.path.join(
                ndk_root, "toolchains", "llvm", "prebuilt", host_tag,
                "sysroot", "usr", "include", "c++", "v1"
            ),
            # Older NDK layout
            os.path.join(ndk_root, "sources", "cxx-stl", "llvm-libc++", "include"),
        ]
        for libcxx in libcxx_candidates:
            if os.path.isdir(libcxx):
                args.extend(["-isystem", libcxx])
                break

        return args

    def _project_includes(self) -> list[str]:
        """Generate -I flags for project include paths."""
        args: list[str] = []
        for path in self.config.get("project_include_paths") or []:
            args.extend(["-I", path])
        return args

    def _extra_system_includes(self) -> list[str]:
        """Generate -isystem flags for extra system include paths."""
        args: list[str] = []
        for path in self.config.get("extra_system_includes") or []:
            args.extend(["-isystem", path])
        return args

    def _defines(self) -> list[str]:
        """Generate -D flags for preprocessor defines."""
        args: list[str] = []
        for define in self.config.get("defines") or []:
            args.append(f"-D{define}")
        return args

    def _extra_args(self) -> list[str]:
        """Pass through any extra raw compiler flags."""
        return list(self.config.get("extra_compile_args") or [])

    @staticmethod
    def _detect_host_tag() -> str:
        """
        Detect the NDK host tag based on the current OS and architecture.

        Returns:
            Host tag string, e.g., "linux-x86_64".
        """
        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "linux":
            os_tag = "linux"
        elif system == "darwin":
            os_tag = "darwin"
        elif system == "windows":
            os_tag = "windows"
        else:
            os_tag = system

        # Normalize architecture
        if machine in ("x86_64", "amd64"):
            arch_tag = "x86_64"
        elif machine in ("arm64", "aarch64"):
            arch_tag = "aarch64"
        else:
            arch_tag = machine

        return f"{os_tag}-{arch_tag}"

    def summary(self) -> str:
        """Return a human-readable summary of the NDK configuration."""
        cfg = self.config
        lines = [
            f"NDK Root:     {cfg.get('ndk_root', '(not set)')}",
            f"API Level:    {cfg.get('api_level', 21)}",
            f"Target Arch:  {cfg.get('target_arch', 'aarch64')}",
            f"C++ Standard: {cfg.get('cpp_standard', 'c++17')}",
            f"Project Includes: {len(cfg.get('project_include_paths') or [])}",
            f"System Includes:  {len(cfg.get('extra_system_includes') or [])}",
            f"Defines:          {len(cfg.get('defines') or [])}",
        ]
        return "\n".join(lines)
