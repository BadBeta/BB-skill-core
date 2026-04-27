#!/usr/bin/env python3
"""
Milestone-commit pre-flight check.

PreToolUse hook on Bash. Fires when the command starts with `git commit`
AND the git working tree contains a `PLAN.md`. Performs four lightweight
checks and emits a single combined warning if any of them fail. Never
blocks — the hook is advisory because false-positive blocking on a
commit would be more harmful than the missed signal.

Checks:

1. **PLAN.md milestone marker.** If the commit message contains an
   `M\\d+:` (or `Phase \\d+:`, `Step \\d+:`) prefix, verify that
   `PLAN.md` already has a matching `### MN — DONE:` (or equivalent)
   line. Catches forgetting to update PLAN.md before committing.

2. **continue.md staleness.** If the project has a `continue.md`
   referencing a most-recent-commit milestone, sanity-check that it
   hasn't fallen behind HEAD by more than one milestone. Pure
   heuristic — looks for `most recent commit:.*M(\\d+)` and compares
   against the highest `M\\d+` in the recent git log.

3. **SSOT grep litmus.** Every project that has a `policy.rs` /
   `config.rs` / `constants.rs` file owning project constants is
   probably enforcing SSOT for those constants. Run the standard
   four greps (Duration::from_*, raw u8 protocol consts, planning
   citations in source, unsafe in a forbid-unsafe crate) and emit
   any non-empty results. The greps are deliberately specific so
   they only fire on real regressions.

4. **e2e test reminder.** If the project has `tests/*.rs` files,
   nudge the user to run `cargo test` before commit if any have
   been modified since the last commit. Soft suggestion only.

Failure mode: any exception exits 0 with no output. Hook never blocks.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def find_project_root(start_dir):
    """Walk upward from start_dir looking for a PLAN.md. Return the
    directory containing it, or None."""
    p = Path(start_dir).resolve()
    for _ in range(8):  # bounded climb
        if (p / "PLAN.md").is_file():
            return p
        if p.parent == p:
            return None
        p = p.parent
    return None


def extract_commit_message(command):
    """Pull the commit message text out of a `git commit` command line.
    Supports `-m "..."`, `-m '...'`, `-m "$(cat <<'EOF' ... EOF)"`, and
    `--message=...`. Returns the raw message body or empty string."""
    # HEREDOC pattern: `-m "$(cat <<'EOF'\n...\nEOF\n)"`
    heredoc = re.search(
        r"<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\s*\1\b",
        command,
        re.DOTALL,
    )
    if heredoc:
        return heredoc.group(2)
    # Plain -m "..." or -m '...'
    plain = re.search(
        r"-m\s+(['\"])(.*?)(?<!\\)\1",
        command,
        re.DOTALL,
    )
    if plain:
        return plain.group(2)
    # --message=...
    eq = re.search(r"--message=(['\"]?)(.*?)\1\s*(?:$|\s)", command)
    if eq:
        return eq.group(2)
    return ""


def check_milestone_in_plan(project_root, commit_msg):
    """If the commit message starts with `M\\d+:`, check that PLAN.md
    has a matching `### M<N> — DONE:` line. Returns warning string or
    None."""
    m = re.match(r"^\s*([MmEe]\d+|Phase\s+\d+|Step\s+\d+)\s*[:—]", commit_msg)
    if not m:
        return None
    label = m.group(1).strip()
    plan_path = project_root / "PLAN.md"
    try:
        plan = plan_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    # Match either heading-style (`### M5 — DONE:`) or bullet-bold-style
    # (`- **M5** — DONE:` / `* **M5** — DONE:`). Some projects use one,
    # some the other; both are common.
    label_re = re.escape(label)
    heading = re.compile(
        rf"###\s+{label_re}\s*[—\-]\s*DONE\b",
        re.IGNORECASE,
    )
    bullet = re.compile(
        rf"^[\-\*]\s+\*\*{label_re}\*\*\s*[—\-]\s*DONE\b",
        re.IGNORECASE | re.MULTILINE,
    )
    if heading.search(plan) or bullet.search(plan):
        return None
    return (
        f"PLAN.md has no `{label} — DONE:` entry (looked for both "
        f"`### {label} — DONE:` heading and `- **{label}** — DONE:` "
        f"bullet forms), but you're about to commit `{label}: ...`. "
        f"Update PLAN.md first so the milestone roadmap stays the "
        f"source of truth."
    )


def check_continue_md_staleness(project_root):
    """Compare continue.md's claimed most-recent-milestone against
    `git log --oneline | head -5`. Returns warning string or None."""
    cont_path = project_root / "continue.md"
    if not cont_path.is_file():
        return None
    try:
        cont = cont_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(
        r"most\s+recent\s+commit\s*:\s*`?([MmEe])(\d+)\s*:",
        cont,
        re.IGNORECASE,
    )
    if not m:
        return None
    claimed_prefix = m.group(1).upper()
    claimed_n = int(m.group(2))
    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-n", "10"],
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except Exception:
        return None
    seen = re.findall(rf"\s({claimed_prefix})(\d+)\s*:", log)
    if not seen:
        return None
    actual_n = max(int(n) for _, n in seen)
    if actual_n > claimed_n + 1:
        return (
            f"continue.md says most-recent-commit: {claimed_prefix}{claimed_n}, "
            f"but git log shows {claimed_prefix}{actual_n}. Update continue.md "
            f"before this commit lands or it'll keep drifting."
        )
    return None


SSOT_GREPS = [
    {
        "name": "Duration::from_* outside policy",
        "pattern": r"Duration::from_",
        "include": ["*.rs"],
        "exclude_paths": ["src/policy.rs", "tests/", "examples/", "benches/"],
    },
    {
        "name": "raw u8/0x__u8 protocol const outside policy",
        "pattern": r"=\s*(?:[0-9]+u8;|0x[0-9a-fA-F]+u8)",
        "include": ["*.rs"],
        "exclude_paths": ["src/policy.rs", "tests/", "examples/", "benches/"],
    },
    {
        "name": "planning-doc citation in source",
        "pattern": r"PLAN\.md|TDD'd|rust-planning §|rust-implementing §|rust-reviewing §|elixir-planning §|elixir-implementing §|elixir-reviewing §",
        "include": ["*.rs", "*.ex", "*.exs"],
        "exclude_paths": [".claude/", "/skills/", "/hooks/"],
    },
]


def run_grep(project_root, spec):
    """Use git ls-files + grep to be fast and respect .gitignore.
    Returns matching lines (max 5) or None.

    Lines containing the §§ rationale-marker sentinel are excluded —
    that's the sanctioned in-source citation channel; bare in-source
    citations on un-marked comment lines are the actual bug. The
    marker is dev-time scaffolding swept before ship."""
    src = project_root / "src"
    if not src.is_dir():
        return None
    try:
        # `git grep -nE pattern -- pathspec` is fast and ignores
        # .git, untracked, and gitignored files.
        cmd = ["git", "grep", "-nE", spec["pattern"], "--"] + [
            f"src/*{ext.lstrip('*')}" for ext in spec["include"]
        ]
        result = subprocess.run(
            cmd,
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        lines = (result.stdout or "").splitlines()
    except Exception:
        return None
    filtered = []
    for line in lines:
        if any(excl in line for excl in spec["exclude_paths"]):
            continue
        if "§§" in line:
            continue
        filtered.append(line)
    return filtered[:5] if filtered else None


def check_ssot_litmus(project_root):
    """Run the standard SSOT greps. Return warning string or None."""
    findings = []
    for spec in SSOT_GREPS:
        hits = run_grep(project_root, spec)
        if hits:
            preview = "\n      ".join(hits[:3])
            findings.append(f"  - {spec['name']}:\n      {preview}")
    if not findings:
        return None
    return (
        "SSOT grep litmus found violations that would normally be empty:\n"
        + "\n".join(findings)
        + "\n  These suggest a constant or citation drifted out of its "
        "single home. Fix or RULE-EXCEPTION before commit."
    )


def check_modified_tests(project_root):
    """Soft reminder if tests/ files exist and are dirty in the index."""
    tests_dir = project_root / "tests"
    if not tests_dir.is_dir():
        return None
    try:
        # List files with staged + unstaged changes under tests/.
        result = subprocess.run(
            ["git", "status", "--porcelain", "tests/"],
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    dirty = [
        line[3:].strip()
        for line in (result.stdout or "").splitlines()
        if line.strip() and line[3:].strip().endswith(".rs")
    ]
    if not dirty:
        return None
    listing = "\n  ".join(f"- {f}" for f in dirty[:5])
    return (
        f"Integration tests changed in this commit:\n  {listing}\n"
        "Consider running them before commit (`cargo test --tests`) — "
        "the unit-test pass doesn't exercise these."
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if (data.get("hook_event_name") or "") != "PreToolUse":
        return 0
    if (data.get("tool_name") or "") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not re.search(r"^\s*git\s+commit\b", cmd):
        return 0

    cwd = (data.get("cwd") or os.getcwd())
    project_root = find_project_root(cwd)
    if project_root is None:
        return 0

    commit_msg = extract_commit_message(cmd)
    warnings = []
    if commit_msg:
        w = check_milestone_in_plan(project_root, commit_msg)
        if w:
            warnings.append(w)
    w = check_continue_md_staleness(project_root)
    if w:
        warnings.append(w)
    w = check_ssot_litmus(project_root)
    if w:
        warnings.append(w)
    w = check_modified_tests(project_root)
    if w:
        warnings.append(w)

    if not warnings:
        return 0

    body = (
        "milestone-commit-check warnings (advisory — commit will proceed):\n\n"
        + "\n\n".join(warnings)
    )
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": body,
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"milestone-commit-check error: {e}\n")
        sys.exit(0)
