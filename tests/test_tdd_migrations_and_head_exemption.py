"""
Tests for three TDD-hook tightenings driven by Phoenix-API user feedback:

  1. Migration files under priv/repo/migrations/ are exempt — they're
     DDL scripts with a `def change/0` callback by Ecto convention,
     not behavior to TDD.
  2. New refactor exemption: if the function name appears as a `def`
     in HEAD's committed version of the same file, the edit is a
     modification of an existing function, not new behavior. This
     covers the "extract from tested public fn that doesn't itself
     appear in test files" case.
  3. The fire message is concise (≤350 chars) — the previous ~700-char
     paragraph was reported as too verbose for a hook that fires
     repeatedly during refactor-heavy work.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "bb-tdd-state-hook.py"


def _seed_extensions(home, exts=(".ex", ".exs")):
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "bb-anti-slop-patterns.json").write_text(json.dumps({
        "universal": {"extensions": [], "checks": []},
    }))
    drop = hooks / "bb-anti-slop-patterns.d"
    drop.mkdir(exist_ok=True)
    (drop / "elixir.json").write_text(json.dumps({
        "elixir": {"extensions": list(exts), "checks": []},
    }))


def _load(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_tdd_state_hook", None)
    spec = importlib.util.spec_from_file_location("bb_tdd_state_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_proj(parent, name="proj"):
    proj = parent / name
    proj.mkdir()
    (proj / "mix.exs").write_text("# fake mix project")
    (proj / "lib").mkdir()
    return proj


def _make_transcript(home, msgs):
    p = home / "transcript.jsonl"
    with open(p, "w") as f:
        for m in msgs:
            f.write(json.dumps({"type": "user", "message": {"content": m}}) + "\n")
    return p


def _ev(file_path, new_string, transcript):
    return {
        "session_id": "tdd-test",
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path), "new_string": new_string},
        "transcript_path": str(transcript),
    }


def _git(proj, *args):
    subprocess.run(
        ["git", *args], cwd=proj, capture_output=True, check=True
    )


def _init_git_with_initial_commit(proj):
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "test@test")
    _git(proj, "config", "user.name", "test")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "initial")


# ── Migration exemption ──────────────────────────────────────────────

def test_migration_file_with_def_change_does_not_fire():
    """Edits to priv/repo/migrations/*.exs always pass through —
    even with a public `def change/0`. Migrations are DDL, not behavior."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed_extensions(home)
        proj = _make_proj(home)
        mig_dir = proj / "priv" / "repo" / "migrations"
        mig_dir.mkdir(parents=True)
        mig = mig_dir / "20260430_create_users.exs"
        mig.write_text(
            "defmodule MyApp.Repo.Migrations.CreateUsers do\n"
            "  use Ecto.Migration\n\n"
            "  def change do\n"
            "    create table(:users) do\n"
            "      add :email, :string\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        transcript = _make_transcript(home, ["[TDD] add user table migration"])
        m = _load(home)
        msg = m.handle(_ev(mig, "  def change do", transcript))
        assert msg is None, f"migration should be silent; got: {msg!r}"


def test_migration_in_umbrella_app_also_exempt():
    """apps/*/priv/repo/migrations/*.exs is the umbrella shape."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed_extensions(home)
        proj = _make_proj(home)
        mig_dir = proj / "apps" / "my_app" / "priv" / "repo" / "migrations"
        mig_dir.mkdir(parents=True)
        mig = mig_dir / "20260430_create_posts.exs"
        mig.write_text("defmodule X do\n  def change do\n    nil\n  end\nend\n")
        transcript = _make_transcript(home, ["[TDD] migration"])
        m = _load(home)
        assert m.handle(_ev(mig, "  def change do", transcript)) is None


# ── HEAD-of-file refactor exemption ──────────────────────────────────

def test_function_in_head_version_of_file_is_exempt():
    """File at HEAD has `def foo/2`; current edit modifies that function.
    Refactor — gate must stay silent."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "user.ex"
        impl.write_text(
            "defmodule User do\n"
            "  def registration_changeset(changeset, attrs) do\n"
            "    changeset\n"
            "  end\n"
            "end\n"
        )
        _init_git_with_initial_commit(proj)
        # Now modify the function (refactor — extract a helper)
        impl.write_text(
            "defmodule User do\n"
            "  def registration_changeset(changeset, attrs) do\n"
            "    base_changeset(changeset, attrs)\n"
            "  end\n\n"
            "  defp base_changeset(changeset, attrs), do: changeset\n"
            "end\n"
        )
        transcript = _make_transcript(home, ["[TDD] extract base_changeset"])
        m = _load(home)
        msg = m.handle(_ev(
            impl,
            "  def registration_changeset(changeset, attrs) do\n"
            "    base_changeset(changeset, attrs)\n"
            "  end",
            transcript,
        ))
        assert msg is None, \
            f"refactor of fn already in HEAD should be silent; got: {msg!r}"


def test_truly_new_function_in_existing_file_still_fires():
    """File at HEAD doesn't have `def brand_new_xyz`; current edit
    adds it. Genuinely new behavior — fire."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "user.ex"
        impl.write_text("defmodule User do\nend\n")
        _init_git_with_initial_commit(proj)
        impl.write_text(
            "defmodule User do\n"
            "  def brand_new_xyz(x), do: x\n"
            "end\n"
        )
        transcript = _make_transcript(home, ["[TDD] add new behaviour"])
        m = _load(home)
        msg = m.handle(_ev(impl, "  def brand_new_xyz(x), do: x", transcript))
        assert msg is not None, \
            "genuinely new public fn should fire"


def test_no_git_repo_does_not_crash():
    """If the project isn't under git (no .git dir), the HEAD check
    must not raise — fall through to the other refactor exemptions."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "user.ex"
        impl.write_text("defmodule User do\n  def new_fn(x), do: x\nend\n")
        # Deliberately no _init_git_with_initial_commit
        transcript = _make_transcript(home, ["[TDD] add fn"])
        m = _load(home)
        # Should not crash; with no other exemptions matching, will fire
        msg = m.handle(_ev(impl, "  def new_fn(x), do: x", transcript))
        assert msg is not None, \
            "with no git history and no exemptions, should fire"


# ── Reminder size ────────────────────────────────────────────────────

def test_fire_reminder_is_concise():
    """The fire message has been a friction-source per user feedback —
    keep it under 350 chars so it doesn't dominate transcripts when
    it fires repeatedly during refactor-heavy work."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed_extensions(home)
        proj = _make_proj(home)
        impl = proj / "lib" / "user.ex"
        impl.write_text(
            "defmodule User do\n  def truly_unique_xyz(x), do: x\nend\n"
        )
        transcript = _make_transcript(home, ["[TDD] add fn"])
        m = _load(home)
        msg = m.handle(_ev(impl, "  def truly_unique_xyz(x), do: x", transcript))
        assert msg is not None
        assert len(msg) < 350, f"reminder too long ({len(msg)} chars): {msg!r}"
        # Still names the file path and the bypass
        assert "user.ex" in msg or impl.name in msg
        assert "[no-TDD]" in msg


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
