"""
Tests for bb-post-generator-scan.py — the one-shot scanner that fires
after `mix phx.new`, `mix igniter.new`, `cargo new`, `cargo init`,
or `cargo generate` completes successfully. Scans the new project
against a drop-in catalog of known-bad generator output patterns.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-post-generator-scan.py"


def _load(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_post_generator_scan", None)
    spec = importlib.util.spec_from_file_location(
        "bb_post_generator_scan", HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_catalog(home, fragments):
    """fragments: dict of {filename → list of check dicts}."""
    hooks = home / ".claude" / "hooks" / "bb-post-generator-patterns.d"
    hooks.mkdir(parents=True, exist_ok=True)
    for name, checks in fragments.items():
        (hooks / name).write_text(json.dumps({"checks": checks}))


# ── detection ────────────────────────────────────────────────────────

def test_detects_mix_phx_new():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        gen = m.detect_generator("mix phx.new my_app", "/cwd")
        assert gen is not None
        assert gen["name"] == "mix-phx-new"
        assert gen["project_dir"] == "/cwd/my_app"


def test_detects_mix_igniter_new_with_flags():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        gen = m.detect_generator("mix igniter.new myapp --install ash", "/work")
        assert gen and gen["name"] == "mix-igniter-new"
        assert gen["project_dir"] == "/work/myapp"


def test_detects_cargo_new():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        gen = m.detect_generator("cargo new my_lib --lib", "/projects")
        assert gen and gen["name"] == "cargo-new"
        assert gen["project_dir"] == "/projects/my_lib"


def test_detects_cargo_init():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        gen = m.detect_generator("cargo init", "/projects/already_here")
        assert gen and gen["name"] == "cargo-init"
        assert gen["project_dir"] == "/projects/already_here"


def test_ignores_unrelated_commands():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        for cmd in ["ls", "git status", "mix compile", "cargo build", ""]:
            assert m.detect_generator(cmd, "/cwd") is None, cmd


# ── catalog scan ─────────────────────────────────────────────────────

def test_scan_emits_finding_for_matching_file():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, {"elixir.json": [
            {
                "id": "phx-runtime-port-unguarded",
                "file_glob": "config/runtime.exs",
                "regex": r"^\s*config\s+:[a-z_]+,\s+\w+Web\.Endpoint,",
                "skip_if_in_file": r"System\.get_env\(\"PORT\"\)",
                "cite": "phoenix §Configuration Precedence",
                "severity": "warn",
                "message": "Endpoint config without PORT fallback.",
            }
        ]})
        proj = home / "myapp"
        (proj / "config").mkdir(parents=True)
        (proj / "config" / "runtime.exs").write_text(
            'import Config\nconfig :myapp, MyappWeb.Endpoint, http: [port: 4000]\n'
        )
        m = _load(home)
        findings = m.scan_project(str(proj))
        assert len(findings) == 1
        assert findings[0]["check_id"] == "phx-runtime-port-unguarded"


def test_scan_skip_if_in_file_silences_finding():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, {"elixir.json": [
            {
                "id": "phx-runtime-port-unguarded",
                "file_glob": "config/runtime.exs",
                "regex": r"^\s*config\s+:[a-z_]+,\s+\w+Web\.Endpoint,",
                "skip_if_in_file": r"System\.get_env\(\"PORT\"\)",
                "cite": "phoenix §Configuration Precedence",
                "severity": "warn",
                "message": "Endpoint config without PORT fallback.",
            }
        ]})
        proj = home / "myapp"
        (proj / "config").mkdir(parents=True)
        # File DOES use System.get_env("PORT") — the guard. No finding.
        (proj / "config" / "runtime.exs").write_text(
            'import Config\n'
            'port = String.to_integer(System.get_env("PORT") || "4000")\n'
            'config :myapp, MyappWeb.Endpoint, http: [port: port]\n'
        )
        m = _load(home)
        findings = m.scan_project(str(proj))
        assert findings == []


def test_scan_no_matching_files():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, {"elixir.json": [
            {
                "id": "irrelevant",
                "file_glob": "config/nonexistent.exs",
                "regex": r"foo",
                "cite": "x",
                "severity": "warn",
                "message": "x",
            }
        ]})
        proj = home / "myapp"
        proj.mkdir()
        (proj / "README.md").write_text("# myapp")
        m = _load(home)
        assert m.scan_project(str(proj)) == []


def test_scan_glob_pattern_matches_recursively():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, {"rust.json": [
            {
                "id": "missing-edition",
                "file_glob": "**/Cargo.toml",
                "regex": r"\[package\]",
                "skip_if_in_file": r"edition\s*=",
                "cite": "rust-planning §Workspace",
                "severity": "warn",
                "message": "Cargo.toml without `edition = \"2024\"`.",
            }
        ]})
        proj = home / "wsp"
        (proj / "crates" / "a").mkdir(parents=True)
        (proj / "Cargo.toml").write_text("[workspace]\nmembers = [\"crates/a\"]\n")
        (proj / "crates" / "a" / "Cargo.toml").write_text(
            "[package]\nname = \"a\"\nversion = \"0.1.0\"\n"
        )
        m = _load(home)
        findings = m.scan_project(str(proj))
        assert len(findings) == 1
        assert findings[0]["file"].endswith("crates/a/Cargo.toml")


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
