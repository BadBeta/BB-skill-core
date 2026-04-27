#!/usr/bin/env bash
# BB-skill-core installer.
#
# Installs the language-independent hooks and merges the core hook
# entries into ~/.claude/settings.json. Idempotent — re-running is safe.
#
# Layout after install:
#   $HOME/.claude/hooks/
#     bb-anti-slop-scan.py            (with .d/ plug-in support)
#     bb-anti-slop-patterns.json      (universal-only catalog)
#     bb-anti-slop-patterns.d/        (created empty; language packs drop files here)
#     bb-skill-enforcement.py         (with .d/ plug-in support)
#     bb-skill-triggers.json          (language-independent triggers)
#     bb-skill-triggers.d/            (created empty)
#     bb-tdd-state-hook.py
#     bb-stop-review-check.py
#     bb-milestone-commit-check.py
#     bb-sweep-rationale-markers.sh
#   $HOME/.claude/settings.json       (core hook entries merged in)
#
# Override install root with $CLAUDE_HOME (default: $HOME/.claude).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-${HOME}/.claude}"
HOOKS_DIR="${CLAUDE_HOME}/hooks"
SETTINGS="${CLAUDE_HOME}/settings.json"
FRAGMENT="${SCRIPT_DIR}/settings-fragment.json"
MERGE="${SCRIPT_DIR}/install/merge_settings.py"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

echo "BB-skill-core install"
echo "  source:    ${SCRIPT_DIR}"
echo "  install:   ${CLAUDE_HOME}"
echo

mkdir -p "${HOOKS_DIR}/bb-anti-slop-patterns.d"
mkdir -p "${HOOKS_DIR}/bb-skill-triggers.d"

# Copy hook files (overwrites — that's the upgrade path).
echo "[1/3] copying hook files…"
for f in "${SCRIPT_DIR}/hooks"/*.py "${SCRIPT_DIR}/hooks"/*.sh "${SCRIPT_DIR}/hooks"/*.json; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    # Language packs own their .d/ files; never overwrite them
    cp -p "$f" "${HOOKS_DIR}/${base}"
done
chmod +x "${HOOKS_DIR}"/*.py "${HOOKS_DIR}"/*.sh 2>/dev/null || true

# Backup existing settings
if [ -f "${SETTINGS}" ]; then
    cp -p "${SETTINGS}" "${SETTINGS}.bak.$(date +%Y%m%d-%H%M%S)"
    echo "[2/3] merging settings (backup: $(ls -1t "${SETTINGS}".bak.* 2>/dev/null | head -1))"
else
    echo "[2/3] creating ${SETTINGS}"
    echo "{}" > "${SETTINGS}"
fi

tmp="$(mktemp)"
python3 "${MERGE}" merge "${SETTINGS}" "${FRAGMENT}" > "${tmp}"
mv "${tmp}" "${SETTINGS}"

# Install merge_settings.py to a known location so language packs can
# reuse it during their install/uninstall (no need to ship duplicates).
mkdir -p "${CLAUDE_HOME}/install"
cp -p "${MERGE}" "${CLAUDE_HOME}/install/merge_settings.py"

# Stamp version
if [ -f "${SCRIPT_DIR}/VERSION" ]; then
    cp -p "${SCRIPT_DIR}/VERSION" "${CLAUDE_HOME}/BB-skill-core.VERSION"
fi

echo "[3/3] done."
echo
echo "Installed:"
ls -1 "${HOOKS_DIR}" | sed 's/^/  /'
echo
echo "Test that it works by running a Claude Code session — the [use-skills]"
echo "marker, anti-slop scan, and TDD gate should all be active. Language packs"
echo "(rust-phase-skills, elixir-phase-skills) can be installed on top of this."
