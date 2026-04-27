"""
Tests that bb-tdd-state-hook.py's IMPL_EXTENSIONS is computed from the
installed anti-slop pattern fragments at module load time. With no
packs installed, the set is empty (TDD hook silently no-ops). With
language packs installed, it equals the union of their extensions.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-tdd-state-hook.py"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _load_with_home(home_dir):
    os.environ["HOME"] = str(home_dir)
    # Force a fresh import so module-level computation re-runs.
    sys.modules.pop("bb_tdd_state_hook", None)
    spec = importlib.util.spec_from_file_location("bb_tdd_state_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_packs_means_empty_impl_extensions():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "universal": {"extensions": [], "checks": []},
        })
        mod = _load_with_home(home)
        assert mod.IMPL_EXTENSIONS == set()


def test_rust_pack_contributes_rs_c_h():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "universal": {"extensions": [], "checks": []},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "rust.json", {
            "rust": {"extensions": [".rs"], "checks": []},
            "c": {"extensions": [".c", ".h"], "checks": []},
        })
        mod = _load_with_home(home)
        assert mod.IMPL_EXTENSIONS == {".rs", ".c", ".h"}


def test_both_packs_install():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "universal": {"extensions": [], "checks": []},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "rust.json", {
            "rust": {"extensions": [".rs"], "checks": []},
            "c": {"extensions": [".c", ".h"], "checks": []},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "elixir.json", {
            "elixir": {"extensions": [".ex", ".exs"], "checks": []},
        })
        mod = _load_with_home(home)
        assert mod.IMPL_EXTENSIONS == {".rs", ".c", ".h", ".ex", ".exs"}


def test_universal_group_excluded_from_impl_extensions():
    """The universal group's `extensions` field is the union it
    computes itself. We must not double-count it into IMPL_EXTENSIONS,
    or a value typed by hand into universal.extensions would slip
    through. Verify by writing a junk extension into universal."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-anti-slop-patterns.json", {
            "universal": {"extensions": [".junk"], "checks": []},
        })
        _write_json(hooks / "bb-anti-slop-patterns.d" / "rust.json", {
            "rust": {"extensions": [".rs"], "checks": []},
        })
        mod = _load_with_home(home)
        assert mod.IMPL_EXTENSIONS == {".rs"}
        assert ".junk" not in mod.IMPL_EXTENSIONS


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
