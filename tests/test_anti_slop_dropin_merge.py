"""
Tests for the bb-anti-slop-patterns.d/ plug-in merge behaviour added so
language packs (rust-phase-skills, elixir-phase-skills) can ship their
own per-language pattern catalogs without modifying the core file.

Run from /home/vidar/Projects/BB-skill-core/:
    python3 -m pytest tests/

(or `python3 tests/test_anti_slop_dropin_merge.py` for the no-pytest path.)
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-anti-slop-scan.py"


def _load_module_with_home(home_dir):
    """Import bb-anti-slop-scan.py with $HOME pointed at a fixture dir,
    so PATTERNS_PATH and PATTERNS_DROPIN_DIR resolve under the fixture."""
    os.environ["HOME"] = str(home_dir)
    spec = importlib.util.spec_from_file_location("bb_anti_slop_scan", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def test_load_patterns_merges_extensions_and_checks():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "universal": {"extensions": [".rs"], "checks": [{"id": "u1"}]},
            "rust": {"extensions": [".rs"], "checks": [{"id": "r1"}]},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "rust.json", {
            "rust": {"extensions": [".rs"], "checks": [{"id": "r2"}]},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "elixir.json", {
            "elixir": {"extensions": [".ex", ".exs"], "checks": [{"id": "e1"}]},
        })
        mod = _load_module_with_home(home)
        db = mod.load_patterns()

        assert set(db.keys()) == {"universal", "rust", "elixir"}
        rust_check_ids = [c["id"] for c in db["rust"]["checks"]]
        assert rust_check_ids == ["r1", "r2"], rust_check_ids
        elixir_check_ids = [c["id"] for c in db["elixir"]["checks"]]
        assert elixir_check_ids == ["e1"]
        assert db["elixir"]["extensions"] == [".ex", ".exs"]


def test_universal_extensions_are_union_of_language_groups():
    """Universal group's extensions are computed as the union of all
    language groups' extensions after the .d/ merge. Any value typed
    into the universal extensions field in the base file is overwritten."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "universal": {"extensions": ["IGNORED"], "checks": [{"id": "u1"}]},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "rust.json", {
            "rust": {"extensions": [".rs"], "checks": []},
            "c": {"extensions": [".c", ".h"], "checks": []},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "elixir.json", {
            "elixir": {"extensions": [".ex", ".exs"], "checks": []},
        })
        mod = _load_module_with_home(home)
        db = mod.load_patterns()
        # Sorted-filename dropin order: elixir.json before rust.json,
        # so .ex/.exs come before .rs/.c/.h. Universal IGNORED is replaced.
        assert db["universal"]["extensions"] == [".ex", ".exs", ".rs", ".c", ".h"]


def test_universal_extensions_empty_when_no_language_packs():
    """Core-only install: no .d/ files. Universal group has no
    extensions, so universal checks scan nothing — exactly right
    when no language packs are installed."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "universal": {"extensions": [".rs", ".py"], "checks": [{"id": "u1"}]},
        })
        mod = _load_module_with_home(home)
        db = mod.load_patterns()
        assert db["universal"]["extensions"] == []


def test_load_patterns_no_dropin_dir_works():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "rust": {"extensions": [".rs"], "checks": [{"id": "r1"}]},
        })
        mod = _load_module_with_home(home)
        db = mod.load_patterns()
        assert "rust" in db
        assert db["rust"]["checks"] == [{"id": "r1"}]


def test_load_patterns_skips_underscore_meta_keys_in_fragment():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {})
        _write_json(hooks / "bb-anti-slop-patterns.d" / "f.json", {
            "_comment": "should be ignored",
            "rust": {"extensions": [".rs"], "checks": []},
        })
        mod = _load_module_with_home(home)
        db = mod.load_patterns()
        assert "_comment" not in db
        assert "rust" in db


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
    sys.exit(1 if failures else 0)
