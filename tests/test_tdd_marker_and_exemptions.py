"""
Tests for the [TDD] marker gating and the new refactor exemptions in
bb-tdd-state-hook.py.

Behaviour contract:
  * Default state: hook is silent (no marker → no fire).
  * [TDD] in any user prompt within the last BB_TDD_RECENT_WINDOW
    user-message boundaries activates enforcement.
  * [no-TDD] in a more-recent prompt overrides [TDD].
  * When active, the hook always emits the FULL reminder (no fade).
  * When active, two structural exemptions silence the gate:
      - the new public fn's name appears in any test file in the project
      - the new public fn's name appears in `git log -S` history
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-tdd-state-hook.py"


def _load_with_home(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_tdd_state_hook", None)
    spec = importlib.util.spec_from_file_location("bb_tdd_state_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_extensions(home, exts=(".ex", ".exs", ".rs")):
    """Make IMPL_EXTENSIONS at module-load time include the given exts
    by writing a fake anti-slop catalog under $HOME/.claude/hooks/."""
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "bb-anti-slop-patterns.json").write_text(json.dumps({
        "universal": {"extensions": [], "checks": []},
    }))
    dropin = hooks / "bb-anti-slop-patterns.d"
    dropin.mkdir(exist_ok=True)
    (dropin / "test.json").write_text(json.dumps({
        "test-lang": {"extensions": list(exts), "checks": []},
    }))


def _make_transcript(home, user_messages):
    """Write a minimal JSONL transcript file. Each entry in user_messages
    becomes a `{"type": "user", "message": {"content": "..."}}` line."""
    p = home / "transcript.jsonl"
    with open(p, "w") as f:
        for msg in user_messages:
            f.write(json.dumps({
                "type": "user",
                "message": {"content": msg},
            }) + "\n")
    return p


def _make_event(file_path, new_string, transcript_path, session_id="t-sess"):
    return {
        "session_id": session_id,
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path), "new_string": new_string},
        "transcript_path": str(transcript_path),
    }


def _make_proj(parent, name="proj"):
    proj = parent / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "mix.exs").write_text("# fake\n")
    (proj / "lib").mkdir(exist_ok=True)
    return proj


# ───────────────────────── tests ─────────────────────────

def test_default_silent_no_marker():
    """No [TDD] anywhere → hook returns None even on a textbook violation."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "thing.ex"
        impl.write_text("defmodule Thing do\n  def shiny(x), do: x\nend\n")
        transcript = _make_transcript(home, ["please add a function called shiny"])
        ev = _make_event(impl, "  def shiny(x), do: x", transcript)
        mod = _load_with_home(home)
        assert mod.handle(ev) is None


def test_marker_activates_full_reminder():
    """[TDD] in the latest user message → hook fires the full reminder."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "thing.ex"
        impl.write_text("defmodule Thing do\n  def shiny(x), do: x\nend\n")
        transcript = _make_transcript(home, ["[TDD] please add a function called shiny"])
        ev = _make_event(impl, "  def shiny(x), do: x", transcript)
        mod = _load_with_home(home)
        msg = mod.handle(ev)
        assert msg is not None
        # Forceful — full reminder, not a one-liner
        assert "STOP" in msg or "failing test" in msg
        assert len(msg) > 200


def test_no_tdd_marker_overrides():
    """[no-TDD] in a more-recent prompt → silent again."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "thing.ex"
        impl.write_text("defmodule Thing do\n  def shiny(x), do: x\nend\n")
        transcript = _make_transcript(home, [
            "[TDD] turn it on",
            "[no-TDD] back off, I'm prototyping",
        ])
        ev = _make_event(impl, "  def shiny(x), do: x", transcript)
        mod = _load_with_home(home)
        assert mod.handle(ev) is None


def test_marker_outside_window_does_not_activate(monkeypatch=None):
    """[TDD] more than BB_TDD_RECENT_WINDOW user-turns ago → silent."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        os.environ["BB_TDD_RECENT_WINDOW"] = "2"
        try:
            _seed_extensions(home)
            proj = _make_proj(home)
            impl = proj / "lib" / "thing.ex"
            impl.write_text("defmodule Thing do\n  def shiny(x), do: x\nend\n")
            transcript = _make_transcript(home, [
                "[TDD] turn it on",
                "ok do step 1",
                "ok do step 2",
                "ok do step 3",  # [TDD] is now 3 turns back, beyond window=2
            ])
            ev = _make_event(impl, "  def shiny(x), do: x", transcript)
            mod = _load_with_home(home)
            assert mod.handle(ev) is None
        finally:
            os.environ.pop("BB_TDD_RECENT_WINDOW", None)


def test_refactor_exempt_when_name_in_test_files():
    """Active [TDD], but the new fn name already appears in a test file
    → treat as refactor / re-exposure of tested behaviour, stay silent."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "thing.ex"
        impl.write_text("defmodule Thing do\n  def already_tested(x), do: x\nend\n")
        (proj / "test").mkdir()
        (proj / "test" / "thing_test.exs").write_text(
            "defmodule ThingTest do\n  test \"x\" do\n    Thing.already_tested(1)\n  end\nend\n"
        )
        transcript = _make_transcript(home, ["[TDD] extract that helper"])
        ev = _make_event(impl, "  def already_tested(x), do: x", transcript)
        mod = _load_with_home(home)
        assert mod.handle(ev) is None


def test_refactor_exempt_when_name_in_git_history():
    """Active [TDD], but the new fn name exists in git log -S history
    → it's a rename / move, stay silent."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "thing.ex"
        # Initial commit — establishes the fn name in history
        impl.write_text("defmodule Thing do\n  def renamed_fn(x), do: x\nend\n")
        for cmd in [
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@test"],
            ["git", "config", "user.name", "test"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "initial"],
        ]:
            subprocess.run(cmd, cwd=proj, check=True, capture_output=True)
        # Now simulate moving the fn — same name reappears in a new edit
        transcript = _make_transcript(home, ["[TDD] move that to a new module"])
        ev = _make_event(impl, "  def renamed_fn(x), do: x", transcript)
        mod = _load_with_home(home)
        assert mod.handle(ev) is None


def test_default_window_is_whole_session():
    """Default BB_TDD_RECENT_WINDOW=0 → [TDD] from many prompts ago
    is still active. Confirms the per-session-persistence default."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        # Make sure no env override is leaking in from a prior test
        os.environ.pop("BB_TDD_RECENT_WINDOW", None)
        _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "thing.ex"
        impl.write_text("defmodule Thing do\n  def brand_new_xyz(x), do: x\nend\n")
        # [TDD] is 50 prompts back — well beyond any reasonable window
        msgs = ["[TDD] turn it on"] + [f"step {i}" for i in range(50)]
        transcript = _make_transcript(home, msgs)
        ev = _make_event(impl, "  def brand_new_xyz(x), do: x", transcript)
        mod = _load_with_home(home)
        msg = mod.handle(ev)
        assert msg is not None, "[TDD] should still be active 50 prompts later by default"


def test_truly_new_fn_fires_under_marker():
    """Active [TDD], name is novel (not in tests, not in git) → fires."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "thing.ex"
        impl.write_text(
            "defmodule Thing do\n  def brand_new_unique_xyzzy_fn(x), do: x\nend\n"
        )
        transcript = _make_transcript(home, ["[TDD] add a new behaviour"])
        ev = _make_event(impl, "  def brand_new_unique_xyzzy_fn(x), do: x", transcript)
        mod = _load_with_home(home)
        msg = mod.handle(ev)
        assert msg is not None, "should fire — name is novel"


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
