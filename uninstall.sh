#!/usr/bin/env bash
# BB-skill-core uninstaller.
#
# Refuses to run if a language pack is detected, since the language packs
# depend on core. Remove them first with their own uninstall.sh scripts.
#
# What this script does:
#   - Removes core hook files from $CLAUDE_HOME/hooks/
#   - Removes the bb-anti-slop-patterns.d/ and bb-skill-triggers.d/ directories
#     ONLY if they are empty (so a language pack's drop-ins are never lost)
#   - Removes the core hook entries from $CLAUDE_HOME/settings.json
#   - Leaves $CLAUDE_HOME itself in place

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-${HOME}/.claude}"
HOOKS_DIR="${CLAUDE_HOME}/hooks"
SETTINGS="${CLAUDE_HOME}/settings.json"
FRAGMENT="${SCRIPT_DIR}/settings-fragment.json"
MERGE="${SCRIPT_DIR}/install/merge_settings.py"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

# Detect language packs by their drop-in fingerprints + their own hook files
detected=()
for f in \
    "${HOOKS_DIR}/bb-rationale-marker-rust.py" \
    "${HOOKS_DIR}/bb-no-std-build-check.py" ; do
    [ -e "$f" ] && detected+=("rust-phase-skills (${f##*/})")
done
for f in "${HOOKS_DIR}/bb-rationale-marker-elixir.py"; do
    [ -e "$f" ] && detected+=("elixir-phase-skills (${f##*/})")
done

if [ "${#detected[@]}" -gt 0 ]; then
    echo "Refusing to uninstall: language pack(s) still installed:" >&2
    for d in "${detected[@]}"; do echo "  - $d" >&2; done
    echo "Run their uninstall.sh first, then re-run this script." >&2
    exit 2
fi

rm -f "${HOOKS_DIR}/bb-skill-triggers.d/third-party-skills.json"

CORE_FILES=(
    bb-anti-slop-scan.py
    bb-anti-slop-patterns.json
    bb-skill-enforcement.py
    bb-skill-triggers.json
    bb-tdd-state-hook.py
    bb-stop-review-check.py
    bb-milestone-commit-check.py
    bb-sweep-rationale-markers.sh
    bb-post-generator-scan.py
    bb-milestone-skill-report.py
)

echo "BB-skill-core uninstall"
echo "  removing core hook files from ${HOOKS_DIR}"
for name in "${CORE_FILES[@]}"; do
    rm -f "${HOOKS_DIR}/${name}"
done

# Drop-in dirs: only remove if empty
for d in bb-anti-slop-patterns.d bb-skill-triggers.d bb-post-generator-patterns.d; do
    if [ -d "${HOOKS_DIR}/${d}" ] && [ -z "$(ls -A "${HOOKS_DIR}/${d}" 2>/dev/null)" ]; then
        rmdir "${HOOKS_DIR}/${d}"
    fi
done

# Settings
if [ -f "${SETTINGS}" ]; then
    cp -p "${SETTINGS}" "${SETTINGS}.bak.$(date +%Y%m%d-%H%M%S)"
    tmp="$(mktemp)"
    python3 "${MERGE}" unmerge "${SETTINGS}" "${FRAGMENT}" > "${tmp}"
    mv "${tmp}" "${SETTINGS}"
    echo "  cleaned settings.json"
fi

rm -f "${CLAUDE_HOME}/BB-skill-core.VERSION"
echo "done."
