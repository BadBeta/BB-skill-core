#!/usr/bin/env python3
"""
Milestone skill-report enforcement hook.

PreToolUse(Edit|Write|NotebookEdit). For projects organised around
M-milestones (PLAN.md with `M\\d+:` markers), this hook blocks the
LLM from editing project files until it has written a brief skill-
section summary into milestone_skill_report.md for the current
milestone — making the "I considered which skills before starting"
discipline visible and enforced.

Always allows edits to PLAN.md and milestone_skill_report.md so the
report can be created in the first place.

Bypass: include `[no-skills-report]` in any prompt; the marker stays
in effect for the rest of the session.

Fails open: any exception → exit 0.
"""
import json
import re
import sys
from pathlib import Path

NO_SKILLS_REPORT_MARKER = "[no-skills-report]"
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
PROJECT_MARKERS = (
    "PLAN.md", "Cargo.toml", "mix.exs", "pyproject.toml",
    "package.json", "go.mod", ".git",
)
# The body under a milestone heading must contain at least this many
# non-whitespace characters of free text to count as a real entry.
MIN_BODY_CHARS = 50

# Match an M-milestone line in PLAN.md that has NOT been marked done.
PLAN_MILESTONE_RE = re.compile(
    r"(?m)^(?:#{1,6}\s+|[-*]\s+(?:\*\*)?)M(\d+)(?:\*\*)?\s*"  # M-prefix + space
    r"(?P<rest>.*)$"
)
DONE_MARKERS = re.compile(r"\bDONE\b|✓|✅|\[x\]|\[X\]")

# Match a heading or bullet for a specific milestone in
# milestone_skill_report.md.
def _milestone_entry_re(label):
    return re.compile(
        rf"(?m)^(?:#{{1,6}}\s+|[-*]\s+(?:\*\*)?)"   # md heading or bullet
        rf"{re.escape(label)}(?:\*\*)?\b"
    )


def project_root(file_path):
    """Walk up from the file until we find a project marker; return that
    directory or None."""
    p = Path(file_path).resolve()
    if p.is_file():
        p = p.parent
    elif not p.exists():
        # Path may be about to be created — start from its parent
        p = p.parent
    seen = set()
    cur = p
    while cur not in seen:
        seen.add(cur)
        for marker in PROJECT_MARKERS:
            if (cur / marker).exists():
                return cur
        if cur == cur.parent:
            return None
        cur = cur.parent
    return None


def active_milestone(project_dir):
    """Return the label (e.g. 'M3') of the lowest-numbered M-milestone
    in PLAN.md that has no DONE marker. Return None if PLAN.md is
    missing or all milestones are done."""
    plan = Path(project_dir) / "PLAN.md"
    if not plan.is_file():
        return None
    try:
        text = plan.read_text(errors="replace")
    except OSError:
        return None
    open_milestones = []
    for m in PLAN_MILESTONE_RE.finditer(text):
        rest = m.group("rest")
        # Done markers may be on the same line or, more loosely, in
        # the heading. We check the captured rest plus the whole line.
        line = m.group(0)
        if DONE_MARKERS.search(line) or DONE_MARKERS.search(rest):
            continue
        open_milestones.append(int(m.group(1)))
    if not open_milestones:
        return None
    return f"M{min(open_milestones)}"


def has_report_entry(project_dir, milestone_label):
    """True iff milestone_skill_report.md has a heading or bullet for
    `milestone_label` AND at least MIN_BODY_CHARS of body content
    follows it (before the next milestone heading or EOF)."""
    report = Path(project_dir) / "milestone_skill_report.md"
    if not report.is_file():
        return False
    try:
        text = report.read_text(errors="replace")
    except OSError:
        return False
    pat = _milestone_entry_re(milestone_label)
    m = pat.search(text)
    if not m:
        return False
    # Body = text from end of match to next milestone marker or EOF.
    start = m.end()
    # Find next M-heading-or-bullet
    next_pat = re.compile(
        r"(?m)^(?:#{1,6}\s+|[-*]\s+(?:\*\*)?)"
        r"M\d+(?:\*\*)?\b"
    )
    nxt = next_pat.search(text, start)
    body = text[start: nxt.start() if nxt else len(text)]
    # Collapse whitespace; require a minimum of free-text chars.
    body_chars = re.sub(r"\s", "", body)
    if len(body_chars) < MIN_BODY_CHARS:
        return False
    # Reject "TODO"/"WIP"/"…" placeholder bodies even if length-passing
    body_lower = body.strip().lower()
    if body_lower in ("todo", "wip", "tbd"):
        return False
    return True


def is_bypass_marker_in_transcript(transcript_path):
    """[no-skills-report] anywhere in a user prompt cancels enforcement
    for the rest of the session."""
    if not transcript_path:
        return False
    p = Path(transcript_path)
    if not p.is_file():
        return False
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "user":
                    continue
                msg = rec.get("message")
                content = msg.get("content") if isinstance(msg, dict) else rec.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    text = "\n".join(parts)
                if NO_SKILLS_REPORT_MARKER in text:
                    return True
    except OSError:
        return False
    return False


def handle(data):
    if data.get("tool_name") not in EDIT_TOOLS:
        return None
    tool_input = data.get("tool_input") or {}
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or ""
    )
    if not file_path:
        return None
    # Always allow edits to PLAN.md and milestone_skill_report.md so the
    # gate is unblockable — the user (or LLM) needs a way to write the
    # report in the first place.
    name = Path(file_path).name
    if name in ("PLAN.md", "milestone_skill_report.md"):
        return None
    if is_bypass_marker_in_transcript(data.get("transcript_path")):
        return None
    proj = project_root(file_path)
    if proj is None:
        return None
    milestone = active_milestone(str(proj))
    if milestone is None:
        return None
    if has_report_entry(str(proj), milestone):
        return None
    reason = (
        f"Milestone-skill-report enforcement: project '{proj}' has an "
        f"active milestone {milestone} (lowest unfinished in PLAN.md), "
        f"but {proj}/milestone_skill_report.md has no entry for "
        f"{milestone} (or the entry is shorter than {MIN_BODY_CHARS} "
        f"chars of body content).\n\n"
        f"Before editing project files, write a brief entry under a "
        f"`{milestone}` heading or bullet in milestone_skill_report.md "
        f"listing the skill sections you reviewed. Example:\n\n"
        f"## {milestone} — short title\n\n"
        f"Skills considered before starting:\n"
        f"- skill-name §SectionA — relevance\n"
        f"- skill-name §SectionB — relevance\n\n"
        f"Bypass: include `[no-skills-report]` in your next prompt to "
        f"silence this gate for the session."
    )
    return {
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("hook_event_name") != "PreToolUse":
        return 0
    try:
        result = handle(data)
    except Exception as e:
        sys.stderr.write(f"milestone-skill-report hook error: {e}\n")
        return 0
    if not result:
        return 0
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": result["permissionDecision"],
            "permissionDecisionReason": result["permissionDecisionReason"],
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"milestone-skill-report hook error: {e}\n")
        sys.exit(0)
