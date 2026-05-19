#!/usr/bin/env bash
# =============================================================================
# OmniGraph — Extract Include Directories from Android Build Files
# =============================================================================
#
# Scans a directory tree for Android.mk and Android.bp files and extracts
# all include directories. Outputs absolute paths suitable for pasting
# directly into ndk_config.json (project_include_paths / extra_system_includes).
#
# Usage:
#   bash scripts/extract_mk_includes.sh /path/to/source/tree
#   bash scripts/extract_mk_includes.sh /path/to/source/tree --json
#   bash scripts/extract_mk_includes.sh /path/to/source/tree --resolve /path/to/aosp_root
#
# Options:
#   --json      Output in JSON array format (ready for ndk_config.json)
#   --resolve   Resolve $(TOP) and relative paths against an AOSP root
#   --bp-only   Only scan Android.bp files
#   --mk-only   Only scan Android.mk files

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
SEARCH_DIR=""
JSON_MODE=false
AOSP_ROOT=""
SCAN_MK=true
SCAN_BP=true

# ── Parse arguments ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            JSON_MODE=true
            shift
            ;;
        --resolve)
            AOSP_ROOT="$2"
            shift 2
            ;;
        --bp-only)
            SCAN_MK=false
            shift
            ;;
        --mk-only)
            SCAN_BP=false
            shift
            ;;
        -h|--help)
            echo "Usage: $0 <source_dir> [--json] [--resolve <aosp_root>] [--mk-only|--bp-only]"
            echo ""
            echo "Scans Android.mk and Android.bp files to extract include directories."
            echo ""
            echo "Options:"
            echo "  --json       Output in JSON array format for ndk_config.json"
            echo "  --resolve    Resolve \$(TOP) and relative paths against AOSP root"
            echo "  --bp-only    Only scan Android.bp files"
            echo "  --mk-only    Only scan Android.mk files"
            exit 0
            ;;
        *)
            if [[ -z "$SEARCH_DIR" ]]; then
                SEARCH_DIR="$1"
            else
                echo "ERROR: Unexpected argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$SEARCH_DIR" ]]; then
    echo "ERROR: Please provide a source directory to scan." >&2
    echo "Usage: $0 <source_dir> [--json] [--resolve <aosp_root>]" >&2
    exit 1
fi

if [[ ! -d "$SEARCH_DIR" ]]; then
    echo "ERROR: Directory does not exist: $SEARCH_DIR" >&2
    exit 1
fi

SEARCH_DIR="$(cd "$SEARCH_DIR" && pwd)"

# ── Collect all include paths ────────────────────────────────────────────────
INCLUDES=()

# --- Android.mk: Extract LOCAL_C_INCLUDES ---
if $SCAN_MK; then
    while IFS= read -r mkfile; do
        mkdir="$(dirname "$mkfile")"

        # Extract LOCAL_C_INCLUDES, handling multi-line continuations with \
        # Join continuation lines, then extract include paths
        joined=$(sed -e ':a' -e '/\\$/{N;s/\\\n//;ba;}' "$mkfile")

        while IFS= read -r line; do
            # Remove the assignment part
            paths="${line#*:=}"
            paths="${paths#*+=}"

            # Split on whitespace and process each path
            for path in $paths; do
                # Skip empty or comment-only
                [[ -z "$path" || "$path" == "#"* ]] && continue

                # Resolve $(LOCAL_PATH) to the mk file's directory
                path="${path//\$(LOCAL_PATH)/$mkdir}"
                path="${path//\$\(LOCAL_PATH\)/$mkdir}"

                # Resolve $(TOP) to AOSP root if provided
                if [[ -n "$AOSP_ROOT" ]]; then
                    path="${path//\$(TOP)/$AOSP_ROOT}"
                    path="${path//\$\(TOP\)/$AOSP_ROOT}"
                fi

                # Resolve $(call my-dir) to the mk file's directory
                path="${path//\$(call my-dir)/$mkdir}"

                # Skip paths that still contain unresolved variables
                if [[ "$path" == *'$('* || "$path" == *'${'* ]]; then
                    echo "# WARN: Unresolved variable in: $path (from $mkfile)" >&2
                    continue
                fi

                # Make absolute
                if [[ "$path" != /* ]]; then
                    path="$mkdir/$path"
                fi

                # Normalize
                if [[ -d "$path" ]]; then
                    path="$(cd "$path" && pwd)"
                fi

                INCLUDES+=("$path")
            done
        done <<< "$(echo "$joined" | grep -E 'LOCAL_C_INCLUDES\s*[:+]?=' || true)"

    done < <(find "$SEARCH_DIR" -name "Android.mk" -type f 2>/dev/null)
fi

# --- Android.bp: Extract include_dirs, local_include_dirs, export_include_dirs ---
if $SCAN_BP; then
    while IFS= read -r bpfile; do
        bpdir="$(dirname "$bpfile")"

        # Extract quoted strings from include_dirs / local_include_dirs / export_include_dirs arrays
        # This is a simplified parser — handles the common case of:
        #   include_dirs: [ "path/one", "path/two" ]
        # spread across multiple lines
        in_include_block=false

        while IFS= read -r line; do
            # Detect start of an include block
            if echo "$line" | grep -qE '(include_dirs|local_include_dirs|export_include_dirs)\s*:'; then
                in_include_block=true
            fi

            if $in_include_block; then
                # Extract all quoted strings from this line
                while [[ "$line" =~ \"([^\"]+)\" ]]; do
                    path="${BASH_REMATCH[1]}"
                    line="${line#*\"${BASH_REMATCH[1]}\"}"

                    # local_include_dirs are relative to the bp file
                    if [[ "$path" != /* ]]; then
                        # Check if it looks like an AOSP-root-relative path
                        if [[ "$path" == system/* || "$path" == hardware/* || "$path" == frameworks/* || "$path" == external/* ]]; then
                            if [[ -n "$AOSP_ROOT" ]]; then
                                path="$AOSP_ROOT/$path"
                            fi
                        else
                            path="$bpdir/$path"
                        fi
                    fi

                    # Normalize if directory exists
                    if [[ -d "$path" ]]; then
                        path="$(cd "$path" && pwd)"
                    fi

                    INCLUDES+=("$path")
                done

                # Detect end of array block
                if echo "$line" | grep -q ']'; then
                    in_include_block=false
                fi
            fi
        done < "$bpfile"

    done < <(find "$SEARCH_DIR" -name "Android.bp" -type f 2>/dev/null)
fi

# ── Deduplicate and sort ─────────────────────────────────────────────────────
SORTED_INCLUDES=($(printf '%s\n' "${INCLUDES[@]}" | sort -u))

# ── Output ───────────────────────────────────────────────────────────────────
if [[ ${#SORTED_INCLUDES[@]} -eq 0 ]]; then
    echo "# No include directories found in $SEARCH_DIR" >&2
    exit 0
fi

echo "# ──────────────────────────────────────────────────────────────"
echo "# Include directories extracted from: $SEARCH_DIR"
echo "# Found ${#SORTED_INCLUDES[@]} unique include paths"
echo "# ──────────────────────────────────────────────────────────────"

if $JSON_MODE; then
    echo ""
    echo "// Paste this into ndk_config.json under project_include_paths:"
    echo '"project_include_paths": ['
    last_idx=$(( ${#SORTED_INCLUDES[@]} - 1 ))
    for i in "${!SORTED_INCLUDES[@]}"; do
        if [[ $i -eq $last_idx ]]; then
            echo "  \"${SORTED_INCLUDES[$i]}\""
        else
            echo "  \"${SORTED_INCLUDES[$i]}\","
        fi
    done
    echo ']'
else
    echo ""
    for inc in "${SORTED_INCLUDES[@]}"; do
        echo "$inc"
    done
fi
