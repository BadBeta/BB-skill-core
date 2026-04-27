"""
Tests for bb-milestone-skill-report.py — the PreToolUse hook that
blocks Edit/Write into a milestone-structured project until the LLM
has written a milestone_skill_report.md entry for the active
milestone listing which skill sections were considered.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-milestone-skill-report.py"


def _load(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_milestone_skill_report", None)
    spec = importlib.util.spec_from_file_location(
        "bb_milestone_skill_report", HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_proj(parent, plan_text=None, report_text=None):
    proj = parent / "proj"
    proj.mkdir()
    (proj / "lib").mkdir()
    (proj / "lib" / "x.ex").write_text("defmodule X, do: 0\n")
    if plan_text is not None:
        (proj / "PLAN.md").write_text(plan_text)
    if report_text is not None:
        (proj / "milestone_skill_report.md").write_text(report_text)
    return proj


def _ev(file_path, transcript=None, tool="Edit"):
    return {
        "session_id": "msr-test",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": str(file_path), "new_string": "x"},
        "transcript_path": str(transcript) if transcript else "",
    }


def _make_transcript(parent, msgs):
    p = parent / "transcript.jsonl"
    with open(p, "w") as f:
        for m in msgs:
            f.write(json.dumps({"type": "user", "message": {"content": m}}) + "\n")
    return p


# ── PLAN parsing ─────────────────────────────────────────────────────

def test_active_milestone_lowest_unfinished():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        plan = (
            "# Plan\n\n"
            "## M1 — DONE: setup\n"
            "## M2 — DONE: core\n"
            "## M3: payment\n"
            "## M4: notifications\n"
        )
        proj = _make_proj(home, plan_text=plan)
        m = _load(home)
        assert m.active_milestone(str(proj)) == "M3"


def test_active_milestone_recognises_check_marker():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        plan = (
            "# Plan\n"
            "- **M1** ✓ setup\n"
            "- **M2** — DONE: core\n"
            "- **M3** payment\n"
        )
        proj = _make_proj(home, plan_text=plan)
        m = _load(home)
        assert m.active_milestone(str(proj)) == "M3"


def test_no_active_milestone_when_no_plan():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        proj = _make_proj(home, plan_text=None)
        m = _load(home)
        assert m.active_milestone(str(proj)) is None


def test_no_active_when_all_done():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        plan = "## M1 — DONE: a\n## M2 — DONE: b\n"
        proj = _make_proj(home, plan_text=plan)
        m = _load(home)
        assert m.active_milestone(str(proj)) is None


# ── Report-entry validation ──────────────────────────────────────────

def test_report_missing_means_no_entry():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        proj = _make_proj(home, plan_text="## M3: payment")
        m = _load(home)
        assert m.has_report_entry(str(proj), "M3") is False


def test_report_with_heading_and_body_passes():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        report = (
            "# Reports\n\n"
            "## M3 — payment\n\n"
            "Skills considered before starting:\n"
            "- elixir-planning §11 (resilience)\n"
            "- elixir-implementing §3.6 (TDD)\n"
        )
        proj = _make_proj(home, plan_text="## M3: payment", report_text=report)
        m = _load(home)
        assert m.has_report_entry(str(proj), "M3") is True


def test_report_too_short_fails():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        report = "## M3 — payment\n\nTODO\n"
        proj = _make_proj(home, plan_text="## M3: payment", report_text=report)
        m = _load(home)
        assert m.has_report_entry(str(proj), "M3") is False


def test_report_with_bullet_form_passes():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        report = (
            "- **M3** payment — skills used: elixir-planning §11 covering "
            "resilience patterns, elixir-implementing §3.6 covering TDD "
            "boundaries and Mox patterns. Reviewed before starting impl.\n"
        )
        proj = _make_proj(home, plan_text="## M3: payment", report_text=report)
        m = _load(home)
        assert m.has_report_entry(str(proj), "M3") is True


# ── End-to-end handle ────────────────────────────────────────────────

def test_block_when_no_report():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        proj = _make_proj(home, plan_text="## M3: payment")
        m = _load(home)
        result = m.handle(_ev(proj / "lib" / "x.ex"))
        assert result is not None
        assert result.get("permissionDecision") == "deny"


def test_allow_when_report_satisfies():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        report = (
            "## M3 — payment\n\nSkills considered: elixir-planning §11 "
            "(resilience), elixir-implementing §3.6 (TDD).\n"
        )
        proj = _make_proj(home, plan_text="## M3: payment", report_text=report)
        m = _load(home)
        assert m.handle(_ev(proj / "lib" / "x.ex")) is None


def test_allow_edits_to_plan_and_report_themselves():
    """Always allow editing PLAN.md and milestone_skill_report.md so the
    report can be created in the first place."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        proj = _make_proj(home, plan_text="## M3: payment")
        m = _load(home)
        assert m.handle(_ev(proj / "PLAN.md")) is None
        assert m.handle(_ev(proj / "milestone_skill_report.md")) is None


def test_allow_when_no_active_milestone():
    """No PLAN.md or all milestones DONE → not a milestone-structured
    project, hook stays out of the way."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        proj = _make_proj(home, plan_text=None)
        m = _load(home)
        assert m.handle(_ev(proj / "lib" / "x.ex")) is None


def test_no_skills_report_marker_bypasses():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        proj = _make_proj(home, plan_text="## M3: payment")
        transcript = _make_transcript(home, ["[no-skills-report] just trying things"])
        m = _load(home)
        assert m.handle(_ev(proj / "lib" / "x.ex", transcript=transcript)) is None


def test_allow_edits_outside_milestone_project():
    """Edit to a path not under any project root (no PLAN.md upwards) →
    silent."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        # No PLAN.md at all
        loose = home / "loose.txt"
        loose.parent.mkdir(parents=True, exist_ok=True)
        loose.write_text("x")
        m = _load(home)
        assert m.handle(_ev(loose)) is None


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                print(f"FAIL {name}: {e}")
                failures += 1
            except Exception as e:
                print(f"ERROR {name}: {type(e).__name__}: {e}")
                failures += 1
    sys.exit(1 if failures else 0)
