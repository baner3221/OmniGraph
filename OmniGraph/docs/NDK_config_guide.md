# OmniGraph — NDK Configuration Guide for Camera HAL Builds

This guide walks you through configuring OmniGraph to parse an Android Camera HAL shared library codebase on Ubuntu Linux. By the end, you'll have a working `ndk_config.json` and can run a full knowledge graph ingestion of your HAL code.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Quick Start (TL;DR)](#2-quick-start)
3. [Step-by-Step Configuration](#3-step-by-step-configuration)
4. [Using the Helper Scripts](#4-using-the-helper-scripts)
5. [Common Camera HAL System Includes](#5-common-camera-hal-system-includes)
6. [Worked Example](#6-worked-example)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Prerequisites

Before configuring OmniGraph for NDK parsing, ensure you have:

| Requirement | How to check | Install if missing |
|-------------|-------------|-------------------|
| **Ubuntu Linux** | `lsb_release -a` | 22.04 or later recommended |
| **Android NDK** | `ls $ANDROID_HOME/ndk/` | [Android Studio SDK Manager](https://developer.android.com/studio) → SDK Tools → NDK |
| **Clang** (for system includes auto-detection) | `clang --version` | `sudo apt install clang` |
| **Python 3.10+** | `python3 --version` | `sudo apt install python3 python3-venv` |
| **OmniGraph dependencies** | `pip list \| grep libclang` | `pip install -r requirements.txt` |
| **Neo4j 5.x** | `neo4j version` | See README.md for apt install instructions |

> **Note:** You do NOT need to build the HAL. OmniGraph parses the source code directly using libclang — no compilation or Android build system required.

---

## 2. Quick Start

```bash
# 1. Copy the template config
cp configs/ndk_config.json configs/my_hal_config.json

# 2. Auto-extract includes and defines from your build files
bash scripts/extract_mk_includes.sh /path/to/camera_hal --json
bash scripts/extract_mk_defines.sh /path/to/camera_hal --json

# 3. Edit the config — fill in ndk_root, api_level, target_arch,
#    and paste the extracted includes/defines
nano configs/my_hal_config.json

# 4. Run OmniGraph
python scripts/run_ingest.py \
    --source-root /path/to/camera_hal \
    --ndk-config configs/my_hal_config.json \
    --languages cpp --verbose
```

---

## 3. Step-by-Step Configuration

Open `configs/ndk_config.json` and fill in each field:

```json
{
    "ndk_root": "",
    "api_level": 21,
    "target_arch": "aarch64",
    "cpp_standard": "c++17",
    "project_include_paths": [],
    "extra_system_includes": [],
    "defines": ["ANDROID"],
    "extra_compile_args": []
}
```

### 3.1 — `ndk_root`

The absolute path to your Android NDK installation.

**How to find it:**

| Method | Command / Location |
|--------|-------------------|
| Android Studio | Settings → SDK Manager → SDK Tools → NDK (Side by side) |
| Environment variable | `echo $ANDROID_NDK_HOME` or `echo $ANDROID_HOME/ndk/` |
| `local.properties` | `grep ndk.dir local.properties` |
| Manual search | `find ~/Android -name "ndk-build" -type f 2>/dev/null` |

**Typical Linux path:** `~/Android/Sdk/ndk/27.0.12077973`

**Verification:**
```bash
# This directory should contain: build, meta, prebuilt, sources, toolchains, etc.
ls /path/to/your/ndk
```

### 3.2 — `api_level`

The Android API level your HAL targets. This determines which system headers and symbols are available.

**How to find it:**

| Source | What to look for |
|--------|-----------------|
| `build.gradle` | `android { defaultConfig { minSdk 21 } }` |
| `Application.mk` | `APP_PLATFORM := android-29` → use `29` |
| `CMakeLists.txt` | `set(ANDROID_PLATFORM android-29)` → use `29` |
| `Android.bp` | `min_sdk_version: "29"` → use `29` |

> **Tip for Camera HAL:** Most Camera HAL3 implementations target API 21+ (Android 5.0 Lollipop). If unsure, use `21` as a safe default.

### 3.3 — `target_arch`

The CPU architecture your HAL is built for.

**How to find it:**

| Source | What to look for | Config value |
|--------|-----------------|--------------|
| `build.gradle` | `ndk { abiFilters "arm64-v8a" }` | `aarch64` |
| `Application.mk` | `APP_ABI := arm64-v8a` | `aarch64` |
| `Android.bp` | `compile_multilib: "64"` | `aarch64` |

**ABI → OmniGraph arch mapping:**

| ABI filter | OmniGraph `target_arch` |
|------------|------------------------|
| `arm64-v8a` | `aarch64` |
| `armeabi-v7a` | `armv7a` |
| `x86_64` | `x86_64` |
| `x86` | `i686` |

> **Tip:** For modern Camera HAL, use `aarch64` — virtually all Android devices ship 64-bit HALs.

### 3.4 — `project_include_paths`

Your HAL's own header directories. These are the paths from `LOCAL_C_INCLUDES` (Android.mk) or `include_dirs` / `local_include_dirs` (Android.bp).

**How to find them — manually:**

```bash
# From Android.mk:
grep -r "LOCAL_C_INCLUDES" /path/to/camera_hal/ --include="Android.mk"

# From Android.bp:
grep -A 10 "include_dirs\|local_include_dirs" /path/to/camera_hal/ --include="Android.bp"

# From CMakeLists.txt:
grep -r "include_directories\|target_include_directories" /path/to/camera_hal/ --include="CMakeLists.txt"
```

**How to find them — automatically (recommended):**

```bash
bash scripts/extract_mk_includes.sh /path/to/camera_hal --json
```

### 3.5 — `extra_system_includes`

System-level include directories from the AOSP tree or vendor SDKs that your HAL depends on but are NOT part of the NDK's standard sysroot.

**This is the most common source of parse errors for Camera HAL code.** See [Section 5](#5-common-camera-hal-system-includes) for the full list.

### 3.6 — `defines`

Preprocessor macros that your build system defines. Without these, `#ifdef ANDROID` blocks will be skipped, leading to missing symbols.

**How to find them:**

```bash
# Automatic extraction
bash scripts/extract_mk_defines.sh /path/to/camera_hal --json

# Or manually from Android.mk:
grep -r "LOCAL_CFLAGS\|LOCAL_CPPFLAGS" /path/to/camera_hal/ --include="Android.mk" | grep "\-D"

# Or from Android.bp:
grep -B2 -A10 "cflags:" /path/to/camera_hal/ --include="Android.bp" | grep "\-D"
```

**Common Camera HAL defines:**

```json
{
    "defines": [
        "ANDROID",
        "__ANDROID_API__=29",
        "LOG_TAG=\"YourCameraHAL\"",
        "NDEBUG"
    ]
}
```

---

## 4. Using the Helper Scripts

### `extract_mk_includes.sh`

Recursively scans a directory for `Android.mk` and `Android.bp` files, extracts all include directories, and outputs them as absolute paths.

```bash
# Basic usage — lists paths one per line
bash scripts/extract_mk_includes.sh /path/to/camera_hal

# JSON output — ready to paste into ndk_config.json
bash scripts/extract_mk_includes.sh /path/to/camera_hal --json

# Resolve $(TOP) against your AOSP root
bash scripts/extract_mk_includes.sh /path/to/camera_hal --resolve /path/to/aosp

# Only scan .mk files (skip .bp)
bash scripts/extract_mk_includes.sh /path/to/camera_hal --mk-only --json
```

### `extract_mk_defines.sh`

Recursively scans for `-D` flags in `LOCAL_CFLAGS`, `LOCAL_CPPFLAGS`, `cflags`, and `cppflags`.

```bash
# Basic usage
bash scripts/extract_mk_defines.sh /path/to/camera_hal

# JSON output
bash scripts/extract_mk_defines.sh /path/to/camera_hal --json
```

> **Tip:** Pipe the `--json` output and manually review before pasting. The scripts may pick up defines from test targets or unused modules.

---

## 5. Common Camera HAL System Includes

Camera HAL code typically depends on headers from these AOSP directories. If you have access to an AOSP source tree (or the specific vendor tree your HAL was developed against), add the relevant paths to `extra_system_includes`.

### Core AOSP Headers

These are the most commonly needed paths for any Camera HAL:

```json
{
    "extra_system_includes": [
        "/path/to/aosp/hardware/libhardware/include",
        "/path/to/aosp/system/media/camera/include",
        "/path/to/aosp/system/core/include",
        "/path/to/aosp/system/core/libutils/include",
        "/path/to/aosp/system/core/libcutils/include",
        "/path/to/aosp/system/logging/liblog/include",
        "/path/to/aosp/frameworks/native/include",
        "/path/to/aosp/frameworks/native/libs/ui/include",
        "/path/to/aosp/frameworks/native/libs/nativebase/include",
        "/path/to/aosp/hardware/interfaces/camera/common/1.0/default/include",
        "/path/to/aosp/hardware/interfaces/camera/device/3.2/default/include",
        "/path/to/aosp/hardware/interfaces/camera/provider/2.4/default/include",
        "/path/to/aosp/system/libhidl/base/include",
        "/path/to/aosp/system/libhidl/transport/include"
    ]
}
```

### Vendor-Specific Headers

Depending on your SoC vendor, you may also need (uncomment as needed):

- **Qualcomm:** `/path/to/vendor/qcom/proprietary/camera/include`, `.../mm-camera/include`
- **Samsung (Exynos):** `/path/to/vendor/samsung/hardware/camera/include`
- **MediaTek:** `/path/to/vendor/mediatek/proprietary/hardware/mtkcam/include`

> **Important:** You do NOT need all of these. Start with the **Core AOSP Headers** that your code actually includes, run OmniGraph, and add more paths based on remaining "file not found" errors in the verbose output.

---

## 6. Worked Example

Suppose you have a Camera HAL at `/home/dev/camera_hal/` with this structure:

```
camera_hal/
├── Android.mk
├── include/
│   ├── CameraHAL.h
│   └── CameraDevice.h
├── src/
│   ├── CameraHAL.cpp
│   ├── CameraDevice.cpp
│   └── CameraFactory.cpp
└── vendor_libs/
    └── include/
        └── vendor_camera_api.h
```

And your `Android.mk` contains:

```makefile
LOCAL_C_INCLUDES := \
    $(LOCAL_PATH)/include \
    $(LOCAL_PATH)/vendor_libs/include \
    $(TOP)/hardware/libhardware/include \
    $(TOP)/system/media/camera/include

LOCAL_CFLAGS := -DANDROID -DLOG_TAG=\"MyCameraHAL\" -Wall
```

### Step 1: Run the helper scripts

```bash
$ bash scripts/extract_mk_includes.sh /home/dev/camera_hal --json --resolve /home/dev/aosp

"project_include_paths": [
  "/home/dev/camera_hal/include",
  "/home/dev/camera_hal/vendor_libs/include",
  "/home/dev/aosp/hardware/libhardware/include",
  "/home/dev/aosp/system/media/camera/include"
]

$ bash scripts/extract_mk_defines.sh /home/dev/camera_hal --json

"defines": [
  "ANDROID",
  "LOG_TAG=\"MyCameraHAL\""
]
```

### Step 2: Create `ndk_config.json`

```json
{
    "ndk_root": "/home/dev/Android/Sdk/ndk/27.0.12077973",
    "api_level": 29,
    "target_arch": "aarch64",
    "cpp_standard": "c++17",
    "project_include_paths": [
        "/home/dev/camera_hal/include",
        "/home/dev/camera_hal/vendor_libs/include"
    ],
    "extra_system_includes": [
        "/home/dev/aosp/hardware/libhardware/include",
        "/home/dev/aosp/system/media/camera/include",
        "/home/dev/aosp/system/core/include",
        "/home/dev/aosp/frameworks/native/include"
    ],
    "defines": [
        "ANDROID",
        "LOG_TAG=\"MyCameraHAL\"",
        "__ANDROID_API__=29"
    ],
    "extra_compile_args": []
}
```

### Step 3: Run OmniGraph

```bash
python scripts/run_ingest.py \
    --source-root /home/dev/camera_hal \
    --ndk-config configs/ndk_config.json \
    --languages cpp \
    --verbose
```

---

## 7. Troubleshooting

### "fatal error: 'stddef.h' file not found" / "'stdint.h' file not found"

**Cause:** The Clang resource directory was not auto-detected.

**Fix:** Ensure `clang` is on your PATH:
```bash
clang --version         # Should print a version
clang -print-resource-dir  # Should print a path like /usr/lib/clang/18

# Install if missing:
sudo apt install clang
```

OmniGraph auto-detects this path and adds it to the compile flags.

### "fatal error: 'hardware/camera3.h' file not found"

**Cause:** Missing AOSP system header path.

**Fix:** Add the containing directory to `extra_system_includes` in your `ndk_config.json`:
```json
"extra_system_includes": [
    "/path/to/aosp/hardware/libhardware/include"
]
```

### "fatal error: 'cutils/log.h' file not found"

**Cause:** Missing Android system/core headers.

**Fix:** Add to `extra_system_includes`:
```json
"extra_system_includes": [
    "/path/to/aosp/system/core/include",
    "/path/to/aosp/system/logging/liblog/include"
]
```

### Many errors but OmniGraph still produces results

**This is expected.** With tolerant parsing enabled (`PARSE_KEEP_GOING`), OmniGraph continues parsing even when some headers are missing. It extracts whatever symbols, classes, and call relationships it can find. To improve coverage, add more paths to `extra_system_includes` and re-run.

### "Unresolved variable in: $(VENDOR_PATH)/..." (from helper scripts)

**Cause:** The build file uses a custom Make variable that the extraction script can't resolve.

**Fix:** Look up what the variable resolves to in your build environment:
```bash
# In your AOSP build env:
echo $VENDOR_PATH
# Then manually add the resolved path to ndk_config.json
```

### How to identify which headers are still missing

Run with `--verbose` and look for "Clang diagnostic" lines in the output:
```bash
python scripts/run_ingest.py ... --verbose 2>&1 | grep "file not found"
```

Each "file not found" error tells you exactly which header is missing and which source file tried to include it. Find the header's location and add its parent directory to `extra_system_includes`.
