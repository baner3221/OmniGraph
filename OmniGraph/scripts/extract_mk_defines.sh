#!/usr/bin/env bash
# =============================================================================
# OmniGraph — Extract Preprocessor Defines from Android Build Files
# =============================================================================
#
# Scans a directory tree for Android.mk and Android.bp files and extracts
# preprocessor defines (-D flags). Outputs clean define names suitable for
# the `defines` array in ndk_config.json.
#
# Usage:
#   bash scripts/extract_mk_defines.sh /path/to/source/tree
#   bash scripts/extract_mk_defines.sh /path/to/source/tree --json

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
SEARCH_DIR=""
JSON_MODE=false

# ── Parse arguments ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            JSON_MODE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 <source_dir> [--json]"
            echo ""
            echo "Scans Android.mk and Android.bp files to extract preprocessor defines."
            echo ""
            echo "Options:"
            echo "  --json   Output in JSON array format for ndk_config.json"
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
    echo "Usage: $0 <source_dir> [--json]" >&2
    exit 1
fi

if [[ ! -d "$SEARCH_DIR" ]]; then
    echo "ERROR: Directory does not exist: $SEARCH_DIR" >&2
    exit 1
fi

SEARCH_DIR="$(cd "$SEARCH_DIR" && pwd)"

# ── Collect all defines ──────────────────────────────────────────────────────
DEFINES=()

# --- Android.mk: Extract -D flags from LOCAL_CFLAGS, LOCAL_CPPFLAGS ---
while IFS= read -r mkfile; do
    # Join continuation lines, then extract flag lines
    joined=$(sed -e ':a' -e '/\\$/{N;s/\\\n//;ba;}' "$mkfile")

    while IFS= read -r line; do
        # Extract all -D flags from the line
        for token in $line; do
            if [[ "$token" == -D* ]]; then
                # Remove the -D prefix
                define="${token#-D}"
                # Skip empty
                [[ -z "$define" ]] && continue
                # Remove surrounding quotes if present
                define="${define%\"}"
                define="${define#\"}"
                define="${define%\'}"
                define="${define#\'}"
                DEFINES+=("$define")
            fi
        done
    done <<< "$(echo "$joined" | grep -E 'LOCAL_C(PP)?FLAGS\s*[:+]?=' || true)"

done < <(find "$SEARCH_DIR" -name "Android.mk" -type f 2>/dev/null)

# --- Android.bp: Extract -D flags from cflags, cppflags ---
while IFS= read -r bpfile; do
    in_flags_block=false

    while IFS= read -r line; do
        # Detect start of cflags/cppflags block
        if echo "$line" | grep -qE '(cflags|cppflags|conlyflags)\s*:'; then
            in_flags_block=true
        fi

        if $in_flags_block; then
            # Extract all quoted strings
            while [[ "$line" =~ \"([^\"]+)\" ]]; do
                flag="${BASH_REMATCH[1]}"
                line="${line#*\"${BASH_REMATCH[1]}\"}"

                if [[ "$flag" == -D* ]]; then
                    define="${flag#-D}"
                    [[ -z "$define" ]] && continue
                    DEFINES+=("$define")
                fi
            done

            # Detect end of array
            if echo "$line" | grep -q ']'; then
                in_flags_block=false
            fi
        fi
    done < "$bpfile"

done < <(find "$SEARCH_DIR" -name "Android.bp" -type f 2>/dev/null)

# ── Deduplicate and sort ─────────────────────────────────────────────────────
if [[ ${#DEFINES[@]} -eq 0 ]]; then
    echo "# No preprocessor defines found in $SEARCH_DIR" >&2
    exit 0
fi

SORTED_DEFINES=($(printf '%s\n' "${DEFINES[@]}" | sort -u))

# ── Output ───────────────────────────────────────────────────────────────────
echo "# ──────────────────────────────────────────────────────────────"
echo "# Preprocessor defines extracted from: $SEARCH_DIR"
echo "# Found ${#SORTED_DEFINES[@]} unique defines"
echo "# ──────────────────────────────────────────────────────────────"

if $JSON_MODE; then
    echo ""
    echo "// Paste this into ndk_config.json under defines:"
    echo '"defines": ['
    last_idx=$(( ${#SORTED_DEFINES[@]} - 1 ))
    for i in "${!SORTED_DEFINES[@]}"; do
        if [[ $i -eq $last_idx ]]; then
            echo "  \"${SORTED_DEFINES[$i]}\""
        else
            echo "  \"${SORTED_DEFINES[$i]}\","
        fi
    done
    echo ']'
else
    echo ""
    for def in "${SORTED_DEFINES[@]}"; do
        echo "$def"
    done
fi
