#!/usr/bin/env python3
"""
Post-generator scanner for Claude Code.

PostToolUse(Bash) hook. When the LLM runs a project generator
(`mix phx.new`, `mix igniter.new`, `cargo new`, `cargo init`,
`cargo generate`), and the command exits 0, this hook scans the new
project against a drop-in catalog of known-bad generator output
patterns, then emits an `additionalContext` reminder so the LLM sees
the issues immediately — while it still remembers which files it
has not yet read.

Catalog: ~/.claude/hooks/bb-post-generator-patterns.d/*.json
Each fragment is `{"checks": [<check>...]}`. Each check has:
  id              — kebab-case rule id
  file_glob       — Path.glob() pattern relative to project root
                    (supports `**` for recursive match)
  regex           — pattern that fires the check on a matched file
  skip_if_in_file — optional regex; if it matches anywhere in file,
                    the check is silenced for that file
  cite            — skill section pointer (e.g. "phoenix §Configuration")
  severity        — block | warn
  message         — remediation text shown to the LLM

Fails open: any exception → exit 0.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS_DROPIN_DIR = (
    Path.home() / ".claude" / "hooks" / "bb-post-generator-patterns.d"
)
MAX_SCAN_BYTES = 200_000


# ── Generator detection ──────────────────────────────────────────────
# Each generator is (name, command-regex, project-dir-extractor). The
# regex captures the project name in group 1 when applicable; the
# extractor turns (regex_match, cwd) into the project root path.
GENERATORS = [
    ("mix-phx-new",
     re.compile(r"^\s*mix\s+phx\.new\s+(\S+)"),
     lambda m, cwd: f"{cwd.rstrip('/')}/{m.group(1)}"),
    ("mix-igniter-new",
     re.compile(r"^\s*mix\s+igniter\.new\s+(\S+)"),
     lambda m, cwd: f"{cwd.rstrip('/')}/{m.group(1)}"),
    ("cargo-new",
     re.compile(r"^\s*cargo\s+new\s+(\S+)"),
     lambda m, cwd: f"{cwd.rstrip('/')}/{m.group(1)}"),
    ("cargo-init",
     re.compile(r"^\s*cargo\s+init\b"),
     lambda m, cwd: cwd),
    ("cargo-generate",
     re.compile(r"^\s*cargo\s+generate\b"),
     lambda m, cwd: cwd),
]


def detect_generator(command, cwd):
    """Return {'name': str, 'project_dir': str} if `command` is a
    generator we recognise, else None."""
    if not isinstance(command, str) or not command.strip():
        return None
    for name, pat, extractor in GENERATORS:
        m = pat.match(command)
        if m:
            return {"name": name, "project_dir": extractor(m, cwd or ".")}
    return None


# ── Catalog load ─────────────────────────────────────────────────────

def load_catalog():
    """Concatenate `checks` from every *.json fragment under the
    drop-in directory. Returns a list of check dicts."""
    checks = []
    if not PATTERNS_DROPIN_DIR.is_dir():
        return checks
    for path in sorted(PATTERNS_DROPIN_DIR.glob("*.json")):
        try:
            doc = json.loads(path.read_text() or "{}")
        except Exception:
            continue
        if isinstance(doc, dict):
            for c in (doc.get("checks") or []):
                if isinstance(c, dict):
                    checks.append(c)
    return checks


# ── Scan ─────────────────────────────────────────────────────────────

def _matches(check, file_path):
    """Return True if the check fires on `file_path`. Reads the file
    once, caps at MAX_SCAN_BYTES. Honours skip_if_in_file."""
    try:
        if file_path.stat().st_size > MAX_SCAN_BYTES:
            return False
    except OSError:
        return False
    try:
        text = file_path.read_text(errors="replace")
    except OSError:
        return False
    skip_in = check.get("skip_if_in_file")
    if skip_in:
        try:
            if re.search(skip_in, text):
                return False
        except re.error:
            pass
    regex = check.get("regex")
    if not regex:
        return False
    try:
        return bool(re.search(regex, text, re.MULTILINE))
    except re.error:
        return False


def scan_project(project_dir):
    """Walk `project_dir` against every catalog check; return list of
    findings. Each finding: {check_id, file, cite, severity, message}."""
    proj = Path(project_dir)
    if not proj.is_dir():
        return []
    catalog = load_catalog()
    findings = []
    for check in catalog:
        glob = check.get("file_glob")
        if not glob:
            continue
        try:
            paths = list(proj.glob(glob))
        except Exception:
            continue
        for p in paths:
            if not p.is_file():
                continue
            if _matches(check, p):
                findings.append({
                    "check_id": check.get("id", "unknown"),
                    "file": str(p),
                    "cite": check.get("cite", ""),
                    "severity": check.get("severity", "warn"),
                    "message": check.get("message", ""),
                })
    return findings


# ── Format reminder ──────────────────────────────────────────────────

def format_findings(generator_name, findings):
    if not findings:
        return None
    block = [f for f in findings if f["severity"] == "block"]
    warn = [f for f in findings if f["severity"] != "block"]
    lines = [
        f"Post-generator scan ({generator_name}) found "
        f"{len(block)} block / {len(warn)} warn item(s):",
        "",
    ]
    for f in block + warn:
        prefix = "[BLOCK]" if f["severity"] == "block" else "[warn] "
        lines.append(
            f"{prefix} {f['cite']}\n"
            f"        file:   {f['file']}\n"
            f"        rule:   {f['message']}"
        )
        lines.append("")
    lines.append(
        "Read each cited skill section and fix [BLOCK] items before "
        "moving on. The generator output is ready to ship modulo these "
        "issues — surfacing them now while you still remember which "
        "files you have not yet read."
    )
    return "\n".join(lines)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("hook_event_name") != "PostToolUse":
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    # Skip on failed generator runs; the response shape varies by harness
    # version, so be permissive — only skip if we can confirm a non-zero
    # exit code, otherwise assume success.
    response = data.get("tool_response") or {}
    exit_code = (
        response.get("exit_code")
        if isinstance(response, dict)
        else None
    )
    if exit_code not in (None, 0):
        return 0
    cwd = data.get("cwd") or os.getcwd()
    gen = detect_generator(cmd, cwd)
    if gen is None:
        return 0
    findings = scan_project(gen["project_dir"])
    msg = format_findings(gen["name"], findings)
    if not msg:
        return 0
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"post-generator-scan hook error: {e}\n")
        sys.exit(0)
