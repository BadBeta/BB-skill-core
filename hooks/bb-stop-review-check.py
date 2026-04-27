#!/usr/bin/env python3
"""
Stop-hook cross-skill review check.

When Claude tries to stop, this hook asks: did an implement-skill run
this turn? If yes, did the matching review-skill also run? If not,
block the stop with a nudge to invoke the review skill first.

The mapping lives in IMPLEMENT_TO_REVIEW below — extend with new
language pairs as they are added.

Opt-outs (highest precedence first):
  [no-review] marker in the latest user prompt → this hook is a no-op
  [no-skills] marker in the latest user prompt → this hook is a no-op
  review-skill already invoked this turn       → requirement satisfied

Fails open: any exception exits 0 so the session is never bricked.
"""

import json
import os
import sys

# Implement-skill → review-skill pairs. When the key was invoked this
# turn and the value was not, Stop is blocked.
IMPLEMENT_TO_REVIEW = {
    "rust-implementing": "rust-reviewing",
    "elixir-implementing": "elixir-reviewing",
}

NO_SKILLS_MARKER = "[no-skills]"
NO_REVIEW_MARKER = "[no-review]"

# Track state across repeated Stop firings so we don't keep blocking if
# the model has decided (after our nudge) that it's actually done.
# File holds the transcript path of the last turn we successfully blocked.
STATE_FILE = os.path.expanduser("~/.claude/hooks/.stop-review-state")


def read_transcript(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def latest_user_index(records):
    """Index of the most recent user-authored message (not a tool result)."""
    for i in range(len(records) - 1, -1, -1):
        rec = records[i]
        if rec.get("type") != "user":
            continue
        msg = rec.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return i
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "text"
            for b in content
        ):
            return i
    return -1


def skills_invoked_since(records, start_idx):
    """
    Set of skill names invoked via the Skill tool after `start_idx`.
    Normalises plugin-namespaced names (`plugin:skill`) to the bare
    skill name.
    """
    used = set()
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
                skill = (block.get("input") or {}).get("skill") or ""
                if ":" in skill:
                    skill = skill.split(":", 1)[1]
                if skill:
                    used.add(skill)
    return used


def opt_out_active(records, start_idx):
    """True if the user's current prompt disables this check."""
    if start_idx < 0:
        return False
    text = extract_text((records[start_idx].get("message") or {}).get("content"))
    return NO_SKILLS_MARKER in text or NO_REVIEW_MARKER in text


def load_state():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def save_state(transcript_path):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(transcript_path or "")
    except OSError:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    if data.get("hook_event_name") != "Stop":
        return 0

    transcript_path = data.get("transcript_path") or ""
    records = read_transcript(transcript_path)
    if not records:
        return 0

    user_idx = latest_user_index(records)
    if user_idx < 0:
        return 0

    if opt_out_active(records, user_idx):
        return 0

    invoked = skills_invoked_since(records, user_idx)

    # Find the first missing review-skill pair.
    missing = None
    for impl, review in IMPLEMENT_TO_REVIEW.items():
        if impl in invoked and review not in invoked:
            missing = (impl, review)
            break

    if missing is None:
        save_state("")  # clear any stale block marker
        # Visibility: when the cross-check actually fired and was
        # satisfied (i.e. an implement-skill ran AND its review pair
        # also ran), emit a brief positive ack so the hook's
        # contribution is visible. When no implement-skill ran this
        # turn there's nothing to say — stay silent.
        ok_pairs = [
            (impl, review)
            for impl, review in IMPLEMENT_TO_REVIEW.items()
            if impl in invoked and review in invoked
        ]
        if ok_pairs:
            pairs_text = ", ".join(
                f"{impl}→{review}" for impl, review in ok_pairs
            )
            out = {
                "systemMessage": (
                    f"stop-review-check: implement→review pair "
                    f"satisfied ({pairs_text})."
                ),
                "suppressOutput": True,
            }
            print(json.dumps(out))
        return 0

    # Don't nag twice in a row on the same transcript — the author may
    # have decided the review nudge was wrong. If we already blocked
    # for this transcript, pass through now.
    last_blocked = load_state()
    if last_blocked == transcript_path:
        save_state("")
        return 0

    impl, review = missing
    save_state(transcript_path)

    message = (
        f"Stop held: `{impl}` was invoked this turn but `{review}` was "
        f"not. Before stopping, invoke `{review}` via the Skill tool "
        f"and run its checklist against the diff you just wrote. "
        f"Report findings (or 'no findings' with a one-line reason). "
        f"This is the implement→review cross-check described in "
        f"~/.claude/hooks/Claude_work_evidence_suggestions.md §6.\n\n"
        f"Opt-outs:\n"
        f"  - include `[no-review]` in your next user prompt to "
        f"disable this hook for the session turn\n"
        f"  - re-stopping without running `{review}` will pass "
        f"(this hook nags at most once per turn)"
    )

    out = {
        "decision": "block",
        "reason": message,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"stop-review-check hook error: {e}\n")
        sys.exit(0)
