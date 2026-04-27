#!/usr/bin/env bash
#
# Strip ephemeral "§§" rationale markers from source files.
#
# Forms recognised:
#
#   // §§ <text>             single-line C-family comment
#   # §§ <text>              single-line #-comment (Python / Elixir / shell)
#   <!-- §§ <text> -->       single-line HTML/XML marker
#
#   /* §§                   ─┐
#   ... block body ...       │  C-family block (any lines between open and
#   §§ */                   ─┘  close are removed)
#
#   <!-- §§                 ─┐
#   ... block body ...       │  HTML/XML block
#   §§ -->                  ─┘
#
# §§ replaces an earlier six-underscore (______) sentinel that could
# false-match on ASCII section-divider comments. See anti-slop-scan.py
# for the rationale.
#
# Usage:
#   bb-sweep-rationale-markers.sh [-n|--dry-run] <file> [...]
#   bb-sweep-rationale-markers.sh -r <dir>                # recurse
#
# Exit 0 always (matches rest of hook family's fail-open convention).

set -uo pipefail

DRY_RUN=0
RECURSE=0

print_usage() {
    sed -n '2,30p' "$0"
}

files=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=1; shift ;;
        -r|--recurse) RECURSE=1; shift ;;
        -h|--help) print_usage; exit 0 ;;
        --) shift; files+=("$@"); break ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) files+=("$1"); shift ;;
    esac
done

if [[ ${#files[@]} -eq 0 ]]; then
    print_usage; exit 2
fi

# If -r given, expand directories into all text-ish files under them.
expanded=()
for f in "${files[@]}"; do
    if [[ $RECURSE -eq 1 && -d "$f" ]]; then
        while IFS= read -r -d '' hit; do
            expanded+=("$hit")
        done < <(grep -rlZ --include='*.rs' --include='*.ex' \
            --include='*.exs' --include='*.c' --include='*.h' \
            --include='*.py' --include='*.ts' --include='*.tsx' \
            --include='*.js' --include='*.jsx' --include='*.go' \
            --include='*.html' --include='*.htm' --include='*.xml' \
            "§§" "$f" 2>/dev/null)
    else
        expanded+=("$f")
    fi
done

total_lines=0
total_files=0

sweep_file() {
    local f="$1"
    [[ -f "$f" ]] || return 0

    # Count how many lines would go, in a single sed pass via a scratch file.
    local tmp
    tmp=$(mktemp) || return 0

    # Order matters: delete blocks first (could contain single-line markers
    # inside), then single-line. Use sed -E for extended regex.
    sed -E \
        -e '/\/\*[[:space:]]+§§[[:space:]]*$/,/^[[:space:]]*§§[[:space:]]*\*\//d' \
        -e '/<!--[[:space:]]+§§[[:space:]]*$/,/^[[:space:]]*§§[[:space:]]*-->/d' \
        -e '/^[[:space:]]*(\/\/|#)[[:space:]]+§§/d' \
        -e '/^[[:space:]]*<!--[[:space:]]+§§.*§§[[:space:]]*-->[[:space:]]*$/d' \
        "$f" > "$tmp"

    local before after delta
    before=$(wc -l < "$f")
    after=$(wc -l < "$tmp")
    delta=$(( before - after ))

    if [[ $delta -eq 0 ]]; then
        rm -f "$tmp"
        return 0
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "would strip $delta line(s) from $f"
        rm -f "$tmp"
    else
        mv "$tmp" "$f"
        echo "stripped $delta line(s) from $f"
    fi
    total_lines=$(( total_lines + delta ))
    total_files=$(( total_files + 1 ))
}

for f in "${expanded[@]}"; do
    sweep_file "$f"
done

if [[ $total_files -gt 0 ]]; then
    prefix="stripped"
    [[ $DRY_RUN -eq 1 ]] && prefix="would strip"
    echo "$prefix $total_lines line(s) across $total_files file(s)"
fi

exit 0
