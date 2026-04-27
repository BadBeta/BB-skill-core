"""
Tests for the bb-skill-triggers.d/ plug-in merge behaviour added so
language packs can ship their per-language keyword → skill maps
without modifying the core trigger file.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-skill-enforcement.py"


def _load_module_with_home(home_dir):
    os.environ["HOME"] = str(home_dir)
    spec = importlib.util.spec_from_file_location(
        "bb_skill_enforcement", HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def test_merges_keywords_concatenating_skills_dedupe():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-skill-triggers.json", {
            "keywords": {
                "skill-authoring": ["skill-authoring"],
                "review": ["base-review"],
            }
        })
        _write_json(hooks / "bb-skill-triggers.d" / "rust.json", {
            "keywords": {
                "cargo": ["rust-implementing"],
                "review": ["rust-reviewing"],
            }
        })
        _write_json(hooks / "bb-skill-triggers.d" / "elixir.json", {
            "keywords": {
                "mix": ["elixir-implementing"],
                "review": ["elixir-reviewing"],
            }
        })
        mod = _load_module_with_home(home)
        triggers = mod.load_triggers()
        kws = triggers["keywords"]
        assert "skill-authoring" in kws
        assert kws["cargo"] == ["rust-implementing"]
        assert kws["mix"] == ["elixir-implementing"]
        # `review` from base + dropins (loaded in sorted filename order:
        # elixir.json before rust.json), dedupe preserving order.
        assert kws["review"] == ["base-review", "elixir-reviewing", "rust-reviewing"]


def test_dropin_dir_missing_returns_base_only():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-skill-triggers.json", {
            "keywords": {"foo": ["bar"]}
        })
        mod = _load_module_with_home(home)
        triggers = mod.load_triggers()
        assert triggers["keywords"] == {"foo": ["bar"]}


def test_base_missing_dropins_present():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-skill-triggers.d" / "rust.json", {
            "keywords": {"cargo": ["rust-implementing"]}
        })
        mod = _load_module_with_home(home)
        triggers = mod.load_triggers()
        assert triggers["keywords"]["cargo"] == ["rust-implementing"]


def test_string_skill_normalised_to_list():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        _write_json(hooks / "bb-skill-triggers.json", {"keywords": {}})
        _write_json(hooks / "bb-skill-triggers.d" / "x.json", {
            "keywords": {"foo": "single-skill"}
        })
        mod = _load_module_with_home(home)
        triggers = mod.load_triggers()
        assert triggers["keywords"]["foo"] == ["single-skill"]


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
