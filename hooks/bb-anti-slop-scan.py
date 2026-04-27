#!/usr/bin/env python3
"""
Anti-slop scan hook for Claude Code.

PostToolUse hook. When Claude edits/writes a source file, scan it against
the per-language anti-slop pattern catalog (anti-slop-patterns.json). If
any known-bad pattern is detected, inject a system reminder citing the
relevant review rule and the fix.

This is the machine-checked half of the AI-slop defense. The self-report
at turn start (skill-enforcement.py UserPromptSubmit injection) is the
intention side; this hook is the verification side — it sees what was
actually written, not what was planned.

Fails open: any exception exits 0 so the session is never bricked.
"""

import json
import os
import re
import sys
from pathlib import Path

PATTERNS_PATH = Path.home() / ".claude" / "hooks" / "bb-anti-slop-patterns.json"
PATTERNS_DROPIN_DIR = (
    Path.home() / ".claude" / "hooks" / "bb-anti-slop-patterns.d"
)

# Only scan files that were just edited/written.
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}

# Max file size to scan (bytes). Larger files bail out — the grep would
# be noisy and the LLM didn't author the whole file.
MAX_SCAN_BYTES = 200_000

# How many lines above and below a match to search for an exception marker.
EXCEPTION_WINDOW = 2

# Rationale markers — ephemeral dev-time notes of the form
#   // §§ <text>             (C-family)
#   # §§ <text>              (Python / Elixir / shell)
#   <!-- §§ <text> -->       (HTML / XML)
# Or multi-line blocks delimited by §§ at the start and end of the
# block body:
#   /* §§                    (opens block)
#   §§ */                    (closes block)
#   <!-- §§                  (opens block)
#   §§ -->                   (closes block)
# These are NOT real documentation — they are scaffolding recording the
# skill section / decision-table row / BAD-GOOD pair applied at this
# site. The bb-sweep-rationale-markers.sh script removes them cleanly
# before ship. Anti-slop scanning skips matches inside marker lines /
# blocks so the citations Claude deliberately writes there don't get
# re-flagged as "planning-citation in source".
#
# The §§ prefix replaces an earlier six-underscore (______) sentinel
# that was technically valid as an identifier in every supported
# language and could collide with section-divider comments like
# `# _________________________________`. §§ is unambiguous: the
# section sign is rare in code, doubling it makes it essentially
# unique, and it semantically extends our existing single-§ skill-
# section citation convention (`rust-implementing §0`).
SINGLE_LINE_MARKER_RE = re.compile(r"^\s*(?://|#|<!--)\s+§§(?=\s|$)")
BLOCK_OPEN_RE = re.compile(r"(?://|/\*|<!--|#)\s+§§\s*$")
BLOCK_CLOSE_RE = re.compile(r"^\s*§§\s*(?:\*/|-->|#)?")

# Marker recognised on any comment line near a match. Formats:
#   // RULE-EXCEPTION: <check-id-or-all> — <reason>
#   # RULE-EXCEPTION: <check-id-or-all> — <reason>
#   <!-- RULE-EXCEPTION: <check-id-or-all> — <reason> -->
# The check-id is optional; if omitted, the marker applies to every check
# on the annotated line. Separator between id and reason must be em-dash,
# en-dash, or ASCII double-hyphen — a single hyphen would collide with
# hyphenated check ids like `silent-unwrap-fallback`.
EXCEPTION_MARKER_RE = re.compile(
    r"RULE-EXCEPTION"
    r"(?:\s*:\s*(?P<id>\S+))?"
    r"\s+(?:[—–]|--)\s+"
    r"(?P<reason>\S.{3,})",
)


def load_patterns():
    """
    Load the base catalog (bb-anti-slop-patterns.json) and merge any
    plug-in fragments under bb-anti-slop-patterns.d/*.json. Each fragment
    file has the same shape as the base file (`{group_name: {extensions:
    [...], checks: [...]}}`). When two files contribute the same group,
    `extensions` lists are merged (union, preserving order) and `checks`
    lists are concatenated. Language packs (rust, elixir) ship their
    rules in this directory so the core repo stays language-agnostic.
    """
    db = {}
    try:
        with open(PATTERNS_PATH) as f:
            db = json.load(f) or {}
    except Exception:
        db = {}
    if not isinstance(db, dict):
        db = {}

    if PATTERNS_DROPIN_DIR.is_dir():
        for path in sorted(PATTERNS_DROPIN_DIR.glob("*.json")):
            try:
                with open(path) as f:
                    fragment = json.load(f)
            except Exception:
                continue
            if not isinstance(fragment, dict):
                continue
            for group_name, group in fragment.items():
                if group_name.startswith("_") or not isinstance(group, dict):
                    continue
                existing = db.get(group_name)
                if not isinstance(existing, dict):
                    db[group_name] = group
                    continue
                # Merge: union of extensions (preserve order), concat checks.
                ext_existing = list(existing.get("extensions") or [])
                for ext in (group.get("extensions") or []):
                    if ext not in ext_existing:
                        ext_existing.append(ext)
                existing["extensions"] = ext_existing
                existing.setdefault("checks", [])
                existing["checks"].extend(group.get("checks") or [])

    # Pack-driven universal extensions: the universal group's extension
    # list is the union of every language group's extensions (after the
    # .d/ merge). With no language packs installed, the union is empty
    # and the universal checks scan nothing — that's the right behaviour
    # for a core-only install.
    universal = db.get("universal")
    if isinstance(universal, dict):
        union = []
        for group_name, group in db.items():
            if group_name.startswith("_") or group_name == "universal":
                continue
            if not isinstance(group, dict):
                continue
            for ext in (group.get("extensions") or []):
                if ext not in union:
                    union.append(ext)
        universal["extensions"] = union
    return db


def file_matches_extension(path, extensions):
    p = str(path).lower()
    return any(p.endswith(ext.lower()) for ext in extensions)


def compute_marker_lines(lines):
    """
    Return a set of 1-indexed line numbers that are inside a rationale
    marker — either single-line ( // §§ ... ), block-open
    ( /* §§ ), block-body between open and close, or block-close
    ( §§ */ ).
    """
    inside = set()
    in_block = False
    for i, line in enumerate(lines, start=1):
        if in_block:
            inside.add(i)
            if BLOCK_CLOSE_RE.search(line):
                in_block = False
            continue
        if SINGLE_LINE_MARKER_RE.match(line):
            inside.add(i)
            continue
        if BLOCK_OPEN_RE.search(line):
            inside.add(i)
            in_block = True
    return inside


def exception_applies(lines, line_no, check_id, cite):
    """
    Return (True, reason) if a RULE-EXCEPTION marker in the window around
    `line_no` covers this check, else (False, None).

    A marker covers the check if its id field is empty, `all`, or matches
    either the check_id or a significant fragment of the citation.
    """
    lo = max(1, line_no - EXCEPTION_WINDOW)
    hi = min(len(lines), line_no + EXCEPTION_WINDOW)
    check_id_l = (check_id or "").lower()
    cite_l = (cite or "").lower()
    for n in range(lo, hi + 1):
        text = lines[n - 1]
        for m in EXCEPTION_MARKER_RE.finditer(text):
            raw = (m.group("id") or "").strip().lower()
            reason = m.group("reason").strip()
            if not raw or raw == "all":
                return True, reason
            if check_id_l and raw == check_id_l:
                return True, reason
            # Loose match against citation: marker might say a partial
            # reference and still cover the check. Substring match either
            # direction counts.
            # RULE-EXCEPTION: planning-citation-in-source — this function's
            # docstring deliberately includes example citations.
            if cite_l and (raw in cite_l or cite_l in raw):
                return True, reason
    return False, None


def scan_file(file_path, patterns_db, edit_window=None):
    """
    Return a list of (check_id, cite, severity, line_number, line_text,
    message) matches.

    When `edit_window` is provided (a `(start_line, end_line)` 1-indexed
    inclusive tuple covering the lines the current edit touched), pattern
    matches outside that window are silently dropped. File-level gates
    (`requires_missing_in_file`, `skip_if_in_file`, etc.) still consult
    the entire file. This is the diff-aware mode — pre-existing matches
    elsewhere in the file are not re-reported on every edit, only newly-
    introduced or newly-touched ones.

    When `edit_window` is `None`, every match is reported (Write-tool
    semantics, or fallback when the edit window can't be computed).
    """
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return []
    if size > MAX_SCAN_BYTES:
        return []

    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return []

    matches = []
    applicable_groups = []

    # Collect every language group whose extensions match this file.
    # "universal" group applies if its extensions include the file's ext.
    for group_name, group in patterns_db.items():
        if not isinstance(group, dict):
            continue
        extensions = group.get("extensions") or []
        if file_matches_extension(file_path, extensions):
            applicable_groups.append((group_name, group))

    if not applicable_groups:
        return []

    # Split into lines once for line-number reporting.
    lines = content.splitlines()
    marker_lines = compute_marker_lines(lines)

    for group_name, group in applicable_groups:
        for check in group.get("checks") or []:
            if not isinstance(check, dict):
                continue
            regex_src = check.get("regex")
            if not regex_src:
                continue
            try:
                regex = re.compile(regex_src, re.MULTILINE)
            except re.error:
                continue

            # File-level gates.
            required_missing = check.get("requires_missing_in_file")
            if required_missing:
                try:
                    if re.search(required_missing, content):
                        continue  # condition NOT met — skip this check
                except re.error:
                    pass

            skip_if = check.get("skip_if_in_file")
            if skip_if:
                try:
                    if re.search(skip_if, content):
                        continue  # file is exempt
                except re.error:
                    pass

            # Path-based exemption: a check that's meaningful in business
            # logic may be legitimate in policy / config files where the
            # value is the source of truth by design.
            skip_if_path = check.get("skip_if_path_matches")
            if skip_if_path:
                try:
                    if re.search(skip_if_path, str(file_path)):
                        continue  # path is exempt
                except re.error:
                    pass

            # Path-based restriction: a check may only make sense in a
            # specific class of files (e.g. Process.sleep in test files
            # only). If set, skip the check unless the path matches.
            only_if_path = check.get("only_if_path_matches")
            if only_if_path:
                try:
                    if not re.search(only_if_path, str(file_path)):
                        continue  # path is out-of-scope for this check
                except re.error:
                    pass

            # Dedupe: a single check should fire at most once per line, even
            # if the regex matches multiple sub-strings there.
            seen_lines = set()
            check_id = check.get("id", "unknown")
            cite = check.get("cite", "")
            for m in regex.finditer(content):
                start = m.start()
                line_no = content.count("\n", 0, start) + 1
                if line_no in seen_lines:
                    continue
                seen_lines.add(line_no)
                # Diff-aware mode: drop matches outside the current
                # edit window so pre-existing patterns elsewhere in the
                # file aren't re-reported on every edit.
                if edit_window is not None:
                    win_lo, win_hi = edit_window
                    if line_no < win_lo or line_no > win_hi:
                        continue
                # Rationale marker line? Explicitly scaffolding, not slop.
                if line_no in marker_lines:
                    continue
                # Exception marker nearby? Author has acknowledged the
                # deviation — skip the finding silently.
                exempted, _reason = exception_applies(
                    lines, line_no, check_id, cite
                )
                if exempted:
                    continue
                line_text = (
                    lines[line_no - 1] if line_no - 1 < len(lines) else ""
                )
                matches.append({
                    "check_id": check_id,
                    "cite": cite,
                    "severity": check.get("severity", "warn"),
                    "line_no": line_no,
                    "line_text": line_text.strip()[:120],
                    "message": check.get("message", ""),
                    "group": group_name,
                })

    return matches


# ── Per-session dedupe ──────────────────────────────────────────────
# Each (check_id, file_path) pair fires at most once per session. This
# stops the "same warning hammered N times for the same line" failure
# mode (Phoenix runtime.exs review feedback).
DEDUPE_DIR = Path.home() / ".claude" / "cache" / "anti-slop-seen"


def _seen_file(session_id):
    DEDUPE_DIR.mkdir(parents=True, exist_ok=True)
    return DEDUPE_DIR / f"{session_id or 'unknown'}.json"


def _load_seen(session_id):
    f = _seen_file(session_id)
    if not f.is_file():
        return set()
    try:
        return set(tuple(x) for x in json.loads(f.read_text() or "[]"))
    except Exception:
        return set()


def _save_seen(session_id, seen):
    f = _seen_file(session_id)
    try:
        f.write_text(json.dumps(sorted(list(seen))))
    except OSError:
        pass


def filter_already_seen(session_id, file_path, matches):
    """Drop matches whose (check_id, file_path) tuple has already
    been emitted for this session."""
    seen = _load_seen(session_id)
    return [
        m for m in matches
        if (m.get("check_id"), file_path) not in seen
    ]


def mark_seen(session_id, file_path, matches):
    """Record (check_id, file_path) tuples so future scans in this
    session don't re-emit them."""
    seen = _load_seen(session_id)
    for m in matches:
        seen.add((m.get("check_id"), file_path))
    _save_seen(session_id, seen)


def edit_window_for_tool(tool_name, tool_input, file_path):
    """
    Compute the inclusive 1-indexed line range that the current edit
    actually touched, so the scan only reports newly-introduced
    matches. Returns None to fall back to whole-file scan semantics
    (the original behaviour).

    For Edit: locate `new_string` inside the post-edit file and
    return the line range it occupies. If the file shape is
    pathological (multiple matches, missing match), fall back.
    For Write: an empty file gets the whole-file scan; a non-empty
    file pre-existing implies wholesale replacement so still
    whole-file. Either way, None.
    For NotebookEdit: too cell-shaped to localize cleanly; None.
    """
    if tool_name != "Edit":
        return None
    new_string = tool_input.get("new_string")
    if not isinstance(new_string, str) or not new_string:
        return None
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    # Look for the new_string verbatim. If absent (e.g. the file was
    # post-processed by another hook), bail to whole-file mode.
    idx = content.find(new_string)
    if idx < 0:
        return None
    # Multiple occurrences mean we can't pinpoint THIS edit; bail.
    if content.find(new_string, idx + 1) >= 0:
        return None
    start_line = content.count("\n", 0, idx) + 1
    end_line = start_line + new_string.count("\n")
    return (start_line, end_line)


def format_reminder(file_path, matches):
    """Compose the additionalContext text."""
    block_matches = [m for m in matches if m["severity"] == "block"]
    warn_matches = [m for m in matches if m["severity"] != "block"]
    lead = (
        f"Anti-slop scan on {file_path} found "
        f"{len(block_matches)} block / {len(warn_matches)} warn item(s)."
    )

    lines = [lead, ""]

    def render(m):
        prefix = "[BLOCK]" if m["severity"] == "block" else "[warn] "
        return (
            f"{prefix} line {m['line_no']}  {m['cite']}\n"
            f"        match:  {m['line_text']}\n"
            f"        rule:   {m['message']}"
        )

    for m in block_matches:
        lines.append(render(m))
        lines.append("")
    for m in warn_matches:
        lines.append(render(m))
        lines.append("")

    lines.append(
        "Act: for each [BLOCK] item, fix it before continuing. For [warn] items, "
        "either fix or briefly justify in a reply. Pattern catalog: "
        "~/.claude/hooks/bb-anti-slop-patterns.json."
    )
    return "\n".join(lines)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    event = data.get("hook_event_name") or ""
    if event != "PostToolUse":
        return 0

    tool_name = data.get("tool_name") or ""
    if tool_name not in EDIT_TOOLS:
        return 0

    tool_input = data.get("tool_input") or {}
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or ""
    )
    if not file_path or not os.path.isfile(file_path):
        return 0

    patterns_db = load_patterns()
    if not patterns_db:
        return 0

    edit_window = edit_window_for_tool(tool_name, tool_input, file_path)
    matches = scan_file(file_path, patterns_db, edit_window=edit_window)
    if not matches:
        return 0

    # Per-session, per-(check_id, file_path) dedupe — each warning fires
    # at most once per session per file. Stops the "same warning hammered
    # N times" failure mode without losing first-fire signal.
    session_id = data.get("session_id") or "unknown"
    matches = filter_already_seen(session_id, file_path, matches)
    if not matches:
        return 0
    mark_seen(session_id, file_path, matches)

    reminder = format_reminder(file_path, matches)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reminder,
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"anti-slop-scan hook error: {e}\n")
        sys.exit(0)
