#!/usr/bin/env python3
"""
Skill-enforcement hook for Claude Code.

Dispatches on `hook_event_name`:
  UserPromptSubmit - if the prompt contains [use-skills], scan the text
    against the keyword map and inject additionalContext listing the
    skills that MUST be invoked for this turn.
  PreToolUse - if enforcement is active (marker in latest user prompt
    OR in the currently-approved plan file), block non-exempt tool
    calls until a Skill invocation has happened since the latest user
    message. Read-only tools are exempt.

Opt-out: [no-skills] in the latest user prompt disables enforcement.

Fails open: any exception exits 0 so the session is never bricked.
"""

import json
import os
import re
import sys
from pathlib import Path

TRIGGERS_PATH = Path.home() / ".claude" / "hooks" / "bb-skill-triggers.json"
TRIGGERS_DROPIN_DIR = (
    Path.home() / ".claude" / "hooks" / "bb-skill-triggers.d"
)

# Tools that do not require a prior Skill invocation.
EXEMPT_TOOLS = {
    "Read", "Glob", "Grep",
    "Skill",
    "TaskCreate", "TaskList", "TaskGet",
    "TaskUpdate", "TaskStop", "TaskOutput",
    "TodoWrite",
    "AskUserQuestion",
    "WebFetch", "WebSearch",
    "ToolSearch",
    "ScheduleWakeup",
    "ExitPlanMode", "EnterPlanMode",
    "Monitor",
    "CronList", "CronCreate", "CronDelete",
}

USE_MARKER = "[use-skills]"
NO_MARKER = "[no-skills]"

# ── Bash orientation exemption ───────────────────────────────────────
# Commands that only look at state (no writes, no side effects beyond
# stdout) should not require a Skill invocation. Skill enforcement is
# meant to gate "about to change something," not "about to look at
# something."
#
# The classification splits on shell separators (`;`, `&&`, `||`, `|`)
# and demands EVERY segment be in the read-only allow-list. A single
# mutating segment poisons the whole chain.
ORIENTATION_BIN_ALLOWLIST = {
    "ls", "pwd", "which", "find", "tree", "cat", "head", "tail",
    "wc", "file", "stat", "echo", "printf", "true", "false",
    "basename", "dirname", "realpath", "readlink", "test", "[",
    "env", "whoami", "id", "uname", "date", "hostname",
    "grep", "egrep", "fgrep", "rg", "less", "more", "type",
    "df", "du",   # bounded; report-only
}
# git subcommands that are read-only (no working-tree or history mutation).
ORIENTATION_GIT_SUBCMDS = {
    "status", "log", "diff", "show", "branch", "tag",
    "remote", "rev-parse", "rev-list", "config",  # config without args is read
    "describe", "blame", "ls-files", "ls-tree", "cat-file",
    "shortlog", "reflog", "for-each-ref", "name-rev",
    "merge-base", "symbolic-ref",
}
SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
REDIRECT_RE = re.compile(r"(?<![<>])(?:>|>>|<>|<<|<<<|2>|&>|tee\b)")


def bash_command_is_orientation(command):
    """True iff every shell segment is a read-only orientation command.
    Empty / unparseable / containing redirects → False (default-deny so
    a malformed parse never short-circuits enforcement)."""
    if not isinstance(command, str) or not command.strip():
        return False
    if REDIRECT_RE.search(command):
        return False
    segments = SHELL_SPLIT_RE.split(command.strip())
    if not segments:
        return False
    for seg in segments:
        seg = seg.strip()
        if not seg:
            return False
        # Strip leading env-var assignments (FOO=bar cmd ...)
        while re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S+\s+", seg):
            seg = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=\S+\s+", "", seg)
        tokens = seg.split()
        if not tokens:
            return False
        cmd = tokens[0]
        # Strip leading `./` or absolute path: still want the basename
        cmd = cmd.rsplit("/", 1)[-1]
        if cmd == "git":
            if len(tokens) < 2:
                return False  # bare `git` prints help; safe-but-unusual → gate
            sub = tokens[1]
            if sub not in ORIENTATION_GIT_SUBCMDS:
                return False
            continue
        if cmd not in ORIENTATION_BIN_ALLOWLIST:
            return False
    return True


def load_triggers():
    """
    Load the base trigger map (bb-skill-triggers.json) and merge any
    plug-in fragments under bb-skill-triggers.d/*.json. Each fragment
    has the same shape — `{"keywords": {keyword: [skill, ...]}}`.
    When two files contribute the same keyword, the skill lists are
    concatenated and deduplicated (order preserved). This is how
    language packs ship their per-language keyword maps without
    modifying the core file.
    """
    base = {"keywords": {}}
    try:
        with open(TRIGGERS_PATH) as f:
            loaded = json.load(f) or {}
            if isinstance(loaded, dict):
                base = loaded
    except Exception:
        pass
    base.setdefault("keywords", {})

    if TRIGGERS_DROPIN_DIR.is_dir():
        for path in sorted(TRIGGERS_DROPIN_DIR.glob("*.json")):
            try:
                with open(path) as f:
                    fragment = json.load(f)
            except Exception:
                continue
            if not isinstance(fragment, dict):
                continue
            for keyword, skills in (fragment.get("keywords") or {}).items():
                if not isinstance(skills, list):
                    skills = [skills]
                existing = base["keywords"].get(keyword, [])
                if not isinstance(existing, list):
                    existing = [existing]
                merged = list(existing)
                for s in skills:
                    if s not in merged:
                        merged.append(s)
                base["keywords"][keyword] = merged
    return base


def extract_text(content):
    """Best-effort plain-text view of a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_use":
                # Include tool input in case marker is embedded there
                try:
                    parts.append(json.dumps(block.get("input", {})))
                except Exception:
                    pass
        return "\n".join(parts)
    return ""


def read_transcript(path):
    records = []
    if not path or not os.path.exists(path):
        return records
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return records


SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>",
    re.DOTALL,
)


def _strip_system_reminders(text):
    """Remove <system-reminder>...</system-reminder> blocks. Returns the
    remaining non-reminder text, stripped."""
    return SYSTEM_REMINDER_RE.sub("", text or "").strip()


def is_typed_user_message(rec):
    """True if this record is a real user-typed message (not a tool_result,
    and not a turn whose only text is system-reminder envelopes)."""
    if rec.get("type") != "user":
        return False
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(_strip_system_reminders(content))
    if isinstance(content, list):
        # Collect every text block; if all that's left after stripping
        # reminder envelopes is empty, this turn carried no real prompt
        # — Skill enforcement should look further back.
        joined = "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return bool(_strip_system_reminders(joined))
    return False


def latest_user_index(records):
    for i in range(len(records) - 1, -1, -1):
        if is_typed_user_message(records[i]):
            return i
    return -1


def latest_user_text(records):
    idx = latest_user_index(records)
    if idx < 0:
        return ""
    return extract_text(records[idx].get("message", {}).get("content"))


def skill_used_after(records, start_idx):
    for rec in records[start_idx + 1:]:
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Skill"
            ):
                return True
    return False


# How many user-message-boundaries back to look when checking if a Skill was
# already invoked. The original behaviour was 1 (only since the LAST user
# message). For sustained work in one domain, a single Skill invocation
# would suffice for many turns — re-invoking the same skill produces a
# 14k-token re-render of identical content for no benefit. The window is
# tunable via env; 0 disables (returns to original per-turn behaviour).
SKILL_RECENT_WINDOW = max(0, int(os.environ.get("BB_SKILL_RECENT_WINDOW", "5")))

# Slash commands like `/rust-planning` load the skill content directly into
# the user message; the assistant doesn't issue a Skill tool call. Treat
# such invocations as Skill-equivalent for enforcement purposes.
SLASH_COMMAND_RE = re.compile(
    r"<command-name>\s*/?(?P<name>[a-zA-Z0-9_-]+)\s*</command-name>"
)

# ── Per-citation enforcement ─────────────────────────────────────────
# The base "≥1 skill invoked" gate accepts any one Skill call as
# satisfying the turn. That lets the assistant cite phoenix in an
# applicability list without ever loading it ("citing without loading"
# — the failure mode that motivated this stricter check).
#
# Two detection patterns:
#
#   STRICT  — any line `<skill>: §<sec>...` (the documented format from
#             PHASE_REPORTING_INSTRUCTION). Optional bold/bullet decoration.
#   BULLET  — markdown bold-bullet `- **<skill>**: ...` (catches
#             malformed citations that omit the §, like the real-world
#             slip the user reported).
#
# Citations are filtered against `known_skills()` (built from the trigger
# map) so non-skill bold bullets like `- **mix.exs**: ...` don't deny.
# `<skill>: n/a — ...` is excluded — explicit non-applicability is fine.
SKILL_CITATION_STRICT_RE = re.compile(
    r"(?m)^[\s\->*]*(?:\*\*)?(?P<skill>[a-z][a-z0-9_-]*[a-z0-9])(?:\*\*)?:\s+(?!n/a\b)[^\n]*§"
)
SKILL_CITATION_BULLET_RE = re.compile(
    r"(?m)^[\s>]*[-*]\s+\*\*(?P<skill>[a-z][a-z0-9_-]*[a-z0-9])\*\*:\s*(?!n/a\b)\S"
)


def known_skills():
    """Set of skill names declared in bb-skill-triggers.json + drop-ins.
    Used as a whitelist when scanning assistant text for citations, so
    non-skill bold bullets (e.g. `- **mix.exs**: ...`) don't trigger
    enforcement."""
    triggers = load_triggers()
    skills = set()
    for _kw, vals in (triggers.get("keywords") or {}).items():
        if isinstance(vals, list):
            skills.update(v for v in vals if isinstance(v, str))
        elif isinstance(vals, str):
            skills.add(vals)
    return skills


def cited_skills_since(records, start_idx, allowed):
    """Skills the assistant has named in an applicability-style citation
    since `start_idx`. Filtered against `allowed` (the known-skills
    whitelist) to avoid false positives on prose bold-bullets."""
    cited = set()
    for rec in records[start_idx + 1:]:
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "text"):
                continue
            text = block.get("text", "")
            if not text:
                continue
            for pat in (SKILL_CITATION_STRICT_RE, SKILL_CITATION_BULLET_RE):
                for m in pat.finditer(text):
                    name = m.group("skill")
                    if name in allowed:
                        cited.add(name)
    return cited


def invoked_skills_recently(records, current_user_idx, window=None):
    """Set of skill names invoked via the Skill tool OR a slash command
    in the recent window. Pairs with `cited_skills_since` to verify each
    cited skill was actually loaded."""
    if window is None:
        window = SKILL_RECENT_WINDOW
    invoked = set()
    user_indices = _user_indices(records)
    if not user_indices:
        return invoked
    try:
        current_pos = user_indices.index(current_user_idx)
    except ValueError:
        return invoked
    if window <= 0:
        start_idx = current_user_idx
    else:
        start_pos = max(0, current_pos - window + 1)
        start_idx = user_indices[start_pos]
    for rec in records[start_idx:]:
        rec_type = rec.get("type")
        if rec_type == "assistant":
            content = (rec.get("message") or {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Skill"
                ):
                    skill_input = block.get("input") or {}
                    name = skill_input.get("skill")
                    if isinstance(name, str) and name:
                        invoked.add(name)
        elif rec_type == "user":
            text = extract_text((rec.get("message") or {}).get("content"))
            if text:
                for m in SLASH_COMMAND_RE.finditer(text):
                    invoked.add(m.group("name"))
    return invoked


def _user_indices(records):
    return [i for i, rec in enumerate(records) if is_typed_user_message(rec)]


def skill_used_recently(records, current_user_idx, window=None):
    """True if a Skill tool was invoked OR a /skill slash command was used
    within the last `window` user-message boundaries (inclusive of current).

    The original `skill_used_after` only looked since the latest user
    message — which forced re-invocation of the same skill on every turn.
    This recent-window variant lets a single Skill invocation cover the
    next N turns in the same domain. Slash-command-loaded skills also
    count, since they put the same content into the conversation.

    With `window <= 0`, falls back to per-turn behaviour (unchanged).
    """
    if window is None:
        window = SKILL_RECENT_WINDOW
    if window <= 0:
        return skill_used_after(records, current_user_idx)
    user_indices = _user_indices(records)
    if not user_indices:
        return False
    try:
        current_pos = user_indices.index(current_user_idx)
    except ValueError:
        return False
    start_pos = max(0, current_pos - window + 1)
    start_idx = user_indices[start_pos]
    for rec in records[start_idx:]:
        rec_type = rec.get("type")
        if rec_type == "assistant":
            content = (rec.get("message") or {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "Skill"
                    ):
                        return True
        elif rec_type == "user":
            text = extract_text((rec.get("message") or {}).get("content"))
            if text and SLASH_COMMAND_RE.search(text):
                return True
    return False


PLAN_PHRASE_RE = re.compile(
    r"plan file exists[^\n]*?(/[^\s`'\"]+\.md)", re.IGNORECASE
)
PLAN_GENERIC_RE = re.compile(r"(/[^\s`'\"]+/\.claude/plans/[^\s`'\"]+\.md)")


def latest_plan_file_content(records):
    """
    Scan user records (back to front) for a system reminder mentioning a
    plan file path; return that file's content if it still exists.
    """
    for rec in reversed(records):
        if rec.get("type") != "user":
            continue
        text = extract_text((rec.get("message") or {}).get("content"))
        if not text:
            continue
        m = PLAN_PHRASE_RE.search(text) or PLAN_GENERIC_RE.search(text)
        if not m:
            continue
        path = m.group(1)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return f.read()
            except Exception:
                return ""
    return ""


def enforcement_state(records):
    """Returns (active: bool, sources: list[str])."""
    latest = latest_user_text(records)
    if NO_MARKER in latest:
        return (False, [])
    sources = []
    if USE_MARKER in latest:
        sources.append("latest prompt")
    plan = latest_plan_file_content(records)
    if USE_MARKER in plan:
        sources.append("approved plan")
    return (bool(sources), sources)


def scan_keywords(text, triggers):
    text_lower = text.lower()
    matched = set()
    for keyword, skills in (triggers.get("keywords") or {}).items():
        if not isinstance(keyword, str) or not keyword:
            continue
        if keyword.lower() in text_lower:
            if isinstance(skills, list):
                matched.update(skills)
            elif isinstance(skills, str):
                matched.add(skills)
    return sorted(matched)


PHASE_REPORTING_INSTRUCTION = (
    "REPORTING REQUIREMENT — before any Edit/Write:\n"
    "  For each APPLICABLE skill, emit a one-line applicability note "
    "using a short section reference:\n"
    "    <skill>: §<sec>[, §<sec>] — <one-line why it applies>\n"
    "  Citations MUST include at least one §<section> marker. Bare "
    "claims like `phoenix: applies` or `- **phoenix**: relevant` are "
    "treated as malformed citations — the PreToolUse hook verifies "
    "that EACH cited skill was actually invoked via the `Skill` tool "
    "in the recent window. Citing a skill without loading it is the "
    "exact failure mode this check exists to catch.\n"
    "  Irrelevant keyword matches (e.g. skill-authoring triggering on "
    "a Rust coding task) may be omitted entirely, or noted once as "
    "`<skill>: n/a — <one-line why it's a false positive>` if you "
    "want to record that you checked. The `n/a` form does NOT require "
    "an invocation.\n\n"
    "  Example:\n"
    "    rust-planning: §5.2, §16 #2 — Stage 2 lib+bin; hexagonal "
    "ports/adapters split drives trait placement.\n"
    "    rust-implementing: §Decision Table Error Handling — typed "
    "enum + `?`, not `Box<dyn Error>`.\n"
    "    skill-authoring: n/a — Rust coding task, not a skill edit.\n\n"
    "  The act of scanning each applicable skill for which sections "
    "apply IS the point. Force-fitting a citation is worse than "
    "omitting — if a skill genuinely doesn't apply, skip it (or `n/a` "
    "it). If you cite it, you must have loaded it.\n\n"
    "INLINE RATIONALE MARKERS — required at decision sites:\n"
    "  When you apply a specific skill rule / decision-table row / "
    "BAD-GOOD pair to drive a non-obvious code choice, you MUST leave "
    "a `§§` marker at the site. The marker goes inside a normal "
    "comment, immediately after the opener, as a doubled section-sign "
    "sentinel:\n\n"
    "    Rust/C/Go/JS/TS:  // §§ <skill>: §<sec> — <why here>\n"
    "    Python/Elixir:    # §§ <skill>: §<sec> — <why here>\n"
    "    HTML/XML:         <!-- §§ <skill>: §<sec> — <why here> -->\n\n"
    "    Multi-line form (C-family):\n"
    "      /* §§\n"
    "       * <rationale paragraph>\n"
    "       §§ */\n\n"
    "  WHEN TO MARK (required):\n"
    "    * The choice was driven by a named decision-table row or "
    "BAD/GOOD pair (e.g. multi-clause head over `case`, behaviour over "
    "protocol, `with` chain over nested `case`).\n"
    "    * The choice rules out a tempting alternative the next reader "
    "would reach for (e.g. `Repo.insert!` → `Repo.insert`; raw JWT lib "
    "→ Guardian; hand-rolled plug → library plug).\n"
    "    * The choice encodes a non-obvious constraint (e.g. constant-"
    "time compare for a secret; bcrypt rounds lowered in test config; "
    "citext for case-insensitive uniqueness).\n"
    "    * Each cited skill in your applicability notes should "
    "correspond to AT LEAST ONE marker in the diff (or a one-line note "
    "in the response why the skill drove a global structural choice "
    "that doesn't fit a single line).\n\n"
    "  WHEN TO SKIP (don't mark):\n"
    "    * Trivial idiomatic code with no decision (e.g. `Enum.map`, "
    "string interpolation).\n"
    "    * Pure boilerplate (module headers, `use` directives, child "
    "specs that the framework dictates).\n"
    "    * Tests — markers belong on the production code that the test "
    "is exercising, not on the test itself.\n\n"
    "  These are NOT documentation — they are dev-time scaffolding that "
    "records which skill fragment drove the decision. The anti-slop "
    "scanner skips them (they are explicitly labelled as ephemeral). "
    "The sweep tool `~/.claude/hooks/bb-sweep-rationale-markers.sh` "
    "removes them cleanly before ship. `grep -rn '§§' src/` finds "
    "them all — use it to audit your own diff before stopping.\n\n"
    "AFTER writing code: if any [BLOCK] or [warn] anti-slop reminder "
    "fires (PostToolUse hook), address it before stopping. Do not "
    "justify slop with 'this is fine' — fix, mark with a "
    "`RULE-EXCEPTION:` comment (same line or within 2 lines of the "
    "violation, naming the check id or `all` and the reason), or "
    "explain why the rule genuinely doesn't apply."
)


def handle_user_prompt_submit(data):
    prompt = data.get("prompt") or ""
    if USE_MARKER not in prompt:
        return 0, None
    triggers = load_triggers()
    skills = scan_keywords(prompt, triggers)
    if skills:
        bullets = "\n".join(f"  - {s}" for s in skills)
        msg = (
            "SKILL ENFORCEMENT ACTIVE ([use-skills] marker detected).\n\n"
            "Based on a keyword scan of the prompt, these skills are "
            "SUGGESTED for this turn:\n"
            f"{bullets}\n\n"
            "Requirements:\n"
            "  1. Invoke AT LEAST ONE applicable skill via the Skill "
            "tool BEFORE any Edit/Write/Bash/MCP call. A PreToolUse "
            "hook enforces this.\n"
            "  2. For each APPLICABLE skill, walk its decision tables "
            "and anti-pattern pairs at code sites — do not rely on "
            "context recall.\n"
            "  3. Skills that don't apply to this task can be omitted "
            "from the reporting step — only cite `skill-x: n/a` when "
            "the keyword match was a genuine false positive worth "
            "noting for yourself.\n"
            "  4. Read, Glob, Grep, Task*, TodoWrite, Skill are exempt "
            "from the PreToolUse block.\n"
            "  5. Use [no-skills] in a later prompt to opt out.\n\n"
            f"{PHASE_REPORTING_INSTRUCTION}"
        )
    else:
        msg = (
            "SKILL ENFORCEMENT ACTIVE ([use-skills] marker detected) but "
            "no keywords matched the trigger map. Invoke any skill that "
            "applies to this task before writing code. A PreToolUse hook "
            "will block non-exempt tool calls until a Skill invocation "
            "has happened.\n\n"
            f"{PHASE_REPORTING_INSTRUCTION}"
        )
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }
    }
    return 0, out


def handle_pre_tool_use(data):
    tool_name = data.get("tool_name") or ""
    transcript_path = data.get("transcript_path") or ""
    records = read_transcript(transcript_path)
    active, sources = enforcement_state(records)
    if not active:
        return 0, None
    if tool_name in EXEMPT_TOOLS:
        return 0, None
    # Read-only Bash commands (ls, pwd, git status, etc.) are exempt —
    # skill enforcement is for mutation, not orientation.
    if tool_name == "Bash":
        cmd = (data.get("tool_input") or {}).get("command", "")
        if bash_command_is_orientation(cmd):
            return 0, None
    idx = latest_user_index(records)

    # Per-citation enforcement runs FIRST. If the assistant cited a skill
    # in this turn but never invoked it, deny — even if some OTHER skill
    # was invoked. "Citing without loading" is the failure mode this
    # check exists for.
    if idx >= 0:
        allowed = known_skills()
        cited = cited_skills_since(records, idx, allowed)
        if cited:
            invoked = invoked_skills_recently(records, idx)
            uncovered = sorted(cited - invoked)
            if uncovered:
                names = ", ".join(uncovered)
                reason = (
                    f"Skill enforcement (source: {', '.join(sources)}). "
                    f"You cited skill(s) without invoking them: {names}. "
                    f"Citing a skill in an applicability note claims you "
                    f"walked its decision tables; the hook verifies that "
                    f"by requiring a matching `Skill` invocation in the "
                    f"recent window. Invoke each cited skill via the "
                    f"`Skill` tool, then retry `{tool_name}`. To explicitly "
                    f"mark a skill as not applicable, write "
                    f"`<skill>: n/a — <one-line why>` instead. Citation "
                    f"format: `<skill>: §<sec> — <reason>` or "
                    f"`- **<skill>**: ...` (markdown bullet)."
                )
                out = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
                return 0, out

    # Fallback: at least one Skill invocation in the recent window.
    if idx >= 0 and skill_used_recently(records, idx):
        return 0, None
    window_hint = (
        f" (window: last {SKILL_RECENT_WINDOW} user message(s); "
        f"slash commands like /rust-planning also count)"
        if SKILL_RECENT_WINDOW > 0
        else ""
    )
    reason = (
        f"Skill enforcement is active (source: {', '.join(sources)}). "
        f"No Skill invocation found in the recent window{window_hint}. "
        f"Invoke the relevant skill(s) first, then retry `{tool_name}`. "
        f"Exempt tools: Read, Glob, Grep, Task*, TodoWrite, Skill, "
        f"WebFetch, WebSearch. Opt out by passing [no-skills] in a "
        f"prompt; tune the window via `BB_SKILL_RECENT_WINDOW` env var."
    )
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    return 0, out


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    event = data.get("hook_event_name") or ""
    try:
        if event == "UserPromptSubmit":
            code, out = handle_user_prompt_submit(data)
        elif event == "PreToolUse":
            code, out = handle_pre_tool_use(data)
        else:
            return 0
    except Exception as e:
        sys.stderr.write(f"skill-enforcement hook error: {e}\n")
        return 0
    if out is not None:
        print(json.dumps(out))
    return code


if __name__ == "__main__":
    sys.exit(main())
