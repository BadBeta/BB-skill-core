"""
Tests for Layer 1 path-based scoping in bb-milestone-skill-report.py.

The pre-Layer-1 hook gated EVERY Edit/Write into a project that had
an active milestone — including doc edits (Technical_report.md,
README.md), config bumps (mix.exs, Cargo.toml), test additions, and
generated-artefact paths. Layer 1 narrows the gate to paths that
plausibly contain milestone-relevant production code.

The behavior is implemented by `is_milestone_gated_path(path, project_root)`
returning True only for paths the gate should enforce.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "bb-milestone-skill-report.py"


def _load(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_milestone_skill_report", None)
    spec = importlib.util.spec_from_file_location(
        "bb_milestone_skill_report", HOOK
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_proj(parent, plan_text="## M1: feature"):
    proj = parent / "proj"
    proj.mkdir()
    (proj / "PLAN.md").write_text(plan_text)
    (proj / "lib").mkdir()
    (proj / "test").mkdir()
    (proj / "config").mkdir()
    (proj / "docs").mkdir()
    return proj


def _ev(file_path, transcript=None):
    return {
        "session_id": "scope-test",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path), "new_string": "x"},
        "transcript_path": str(transcript) if transcript else "",
    }


# ──────────────────────────────────────────────────────────────────────
# Direct unit tests on is_milestone_gated_path
# ──────────────────────────────────────────────────────────────────────

GATED_PATHS = [
    "lib/myapp/feature.ex",
    "lib/myapp/feature.exs",
    "lib/myapp_web/router.ex",
    "src/main.rs",
    "src/lib.rs",
    "src/feature/mod.rs",
    "apps/api/lib/api/auth.ex",
    "apps/api/lib/api_web/controllers/user_controller.ex",
    "crates/signal/src/encoder.rs",
    "crates/codec/src/lib.rs",
]

ALLOWED_PATHS = [
    # markdown — docs / plans / reports / readmes / the report itself
    "Technical_report.md",
    "PLAN.md",
    "milestone_skill_report.md",
    "README.md",
    "continue.md",
    "docs/architecture.md",
    "docs/api/auth.md",
    "documentation/setup.md",
    ".github/workflows/ci.yml",
    # test paths — TDD hook handles these
    "test/myapp_test.exs",
    "tests/integration/auth.rs",
    "spec/feature_spec.rb",
    "lib/myapp_test_helper.ex",  # NOT — this is in lib/, treat as gated  -- expected gated
    # config files
    "mix.exs",
    "Cargo.toml",
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.lock",
    "mix.lock",
    "config/config.exs",
    "config/runtime.exs",
    ".gitignore",
    ".formatter.exs",
    # generated / build
    "target/debug/foo",
    "_build/dev/lib/myapp/ebin/myapp.app",
    "deps/jason/lib/jason.ex",
    "node_modules/foo/index.js",
    "priv/static/assets/app.css",
    # hidden tooling
    ".vscode/settings.json",
    ".github/CODEOWNERS",
    ".claude/anything.json",
]


def test_gated_paths_are_gated():
    """Real implementation paths under lib/ src/ apps/*/lib/ crates/*/src/."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        for rel in GATED_PATHS:
            full = proj / rel
            assert m.is_milestone_gated_path(str(full), str(proj)), \
                f"should be gated: {rel}"


def test_allowed_paths_pass_through():
    """Docs, config, tests, build artefacts, hidden tooling — never gated."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        for rel in ALLOWED_PATHS:
            if rel == "lib/myapp_test_helper.ex":
                continue  # listed for human reading; not allowed
            full = proj / rel
            assert not m.is_milestone_gated_path(str(full), str(proj)), \
                f"should be allowed: {rel}"


def test_lib_test_helper_is_gated():
    """A file in lib/ with `_test_helper` in the name is still impl
    code — the test-path heuristic only triggers on test/ tests/ spec/
    directories, not on filename-based test conventions."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        full = proj / "lib/myapp_test_helper.ex"
        assert m.is_milestone_gated_path(str(full), str(proj))


def test_top_level_source_not_in_known_root_is_allowed():
    """A top-level `script.ex` outside lib/src/apps/crates is treated
    as scaffolding, not production code. Same for foo.rs at the root."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        for rel in ("script.ex", "scratch.exs", "build.rs", "examples/demo.rs"):
            full = proj / rel
            assert not m.is_milestone_gated_path(str(full), str(proj)), \
                f"top-level / examples/ files should not be gated: {rel}"


# ──────────────────────────────────────────────────────────────────────
# End-to-end tests through handle() — confirms the gate now lets
# previously-blocked legitimate edits through
# ──────────────────────────────────────────────────────────────────────

def test_handle_allows_doc_edit_when_milestone_active():
    """Editing Technical_report.md while M1 is active and the report
    file is missing should NOT block — that was the user-feedback bug."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        result = m.handle(_ev(proj / "Technical_report.md"))
        assert result is None, \
            f"docs should pass through; got: {result}"


def test_handle_allows_test_edit_when_milestone_active():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        assert m.handle(_ev(proj / "test/myapp_test.exs")) is None


def test_handle_allows_config_edit_when_milestone_active():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        assert m.handle(_ev(proj / "mix.exs")) is None
        assert m.handle(_ev(proj / "config/runtime.exs")) is None


def test_handle_still_blocks_lib_edit_when_milestone_active():
    """The gate must still fire on real implementation paths."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        result = m.handle(_ev(proj / "lib/myapp/feature.ex"))
        assert result is not None
        assert result.get("permissionDecision") == "deny"


def test_handle_still_allows_plan_md_edit():
    """Existing exemption (editing PLAN.md to mark a milestone done)
    must remain in place."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        assert m.handle(_ev(proj / "PLAN.md")) is None


def test_handle_still_allows_milestone_report_edit():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        m = _load(home)
        assert m.handle(_ev(proj / "milestone_skill_report.md")) is None


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
