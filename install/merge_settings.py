#!/usr/bin/env python3
"""
Idempotently merge a settings fragment into ~/.claude/settings.json.

Usage:
    merge_settings.py merge   <settings.json> <fragment.json>
    merge_settings.py unmerge <settings.json> <fragment.json>

Merge rules:
  * For each event (UserPromptSubmit, PreToolUse, PostToolUse, Stop, ...),
    iterate over fragment entries. Each entry has shape
    { "matcher"?: str, "hooks": [{"type":..,"command":..,"timeout":..}, ...] }.
  * Match an existing entry by `matcher` (treat absent matcher == "").
    If found, merge each fragment hook by `command` string (skip duplicates).
    If not found, append the entry.
  * Idempotent: re-running merge is a no-op.

Unmerge rules:
  * For each event, find existing entries with matching matcher.
    Remove hooks whose `command` appears in the fragment entry's hooks.
    If the entry's hooks list becomes empty, drop the entire entry.
  * If `hooks` field becomes empty, leave it as `{}` (caller decides).

Top-level non-`hooks` keys in the fragment are ignored (they're metadata).

The script writes to stdout and exits 0; the caller redirects.
Failures (bad JSON, etc.) print to stderr and exit non-zero.
"""
import json
import sys
from pathlib import Path


def _matcher_key(entry):
    return entry.get("matcher", "")


def _hook_cmd(h):
    return (h.get("command") or "").strip()


def merge(existing, fragment):
    out = dict(existing)
    out.setdefault("hooks", {})
    frag_hooks = fragment.get("hooks") or {}

    for event, entries in frag_hooks.items():
        existing_event = out["hooks"].setdefault(event, [])
        for entry in entries:
            mk = _matcher_key(entry)
            target = next(
                (e for e in existing_event if _matcher_key(e) == mk), None
            )
            if target is None:
                # Append a clean copy
                out["hooks"][event].append({
                    **({"matcher": mk} if mk else {}),
                    "hooks": [dict(h) for h in (entry.get("hooks") or [])],
                })
                continue
            target_cmds = {_hook_cmd(h) for h in target.get("hooks", [])}
            target.setdefault("hooks", [])
            for h in entry.get("hooks") or []:
                if _hook_cmd(h) and _hook_cmd(h) not in target_cmds:
                    target["hooks"].append(dict(h))
                    target_cmds.add(_hook_cmd(h))
    return out


def unmerge(existing, fragment):
    out = dict(existing)
    out.setdefault("hooks", {})
    frag_hooks = fragment.get("hooks") or {}

    for event, entries in frag_hooks.items():
        if event not in out["hooks"]:
            continue
        for entry in entries:
            mk = _matcher_key(entry)
            cmds_to_remove = {
                _hook_cmd(h) for h in (entry.get("hooks") or []) if _hook_cmd(h)
            }
            if not cmds_to_remove:
                continue
            for target in out["hooks"][event]:
                if _matcher_key(target) != mk:
                    continue
                target["hooks"] = [
                    h for h in (target.get("hooks") or [])
                    if _hook_cmd(h) not in cmds_to_remove
                ]
        # Drop entries whose hooks list is now empty
        out["hooks"][event] = [
            e for e in out["hooks"][event] if e.get("hooks")
        ]
        if not out["hooks"][event]:
            del out["hooks"][event]
    return out


def main():
    if len(sys.argv) != 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    op, settings_path, fragment_path = sys.argv[1], sys.argv[2], sys.argv[3]
    if op not in ("merge", "unmerge"):
        print(f"unknown op: {op}", file=sys.stderr)
        return 2
    settings = {}
    p = Path(settings_path)
    if p.exists():
        try:
            settings = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError as e:
            print(f"settings file is not valid JSON: {e}", file=sys.stderr)
            return 1
    try:
        fragment = json.loads(Path(fragment_path).read_text())
    except json.JSONDecodeError as e:
        print(f"fragment file is not valid JSON: {e}", file=sys.stderr)
        return 1
    result = merge(settings, fragment) if op == "merge" else unmerge(settings, fragment)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
