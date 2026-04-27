"""
Tests for install/merge_settings.py — the idempotent settings.json
fragment merger used by install.sh / uninstall.sh.

Properties exercised:
  * merge into empty settings produces fragment.hooks
  * merge is idempotent (running twice == running once)
  * merge preserves existing entries in non-fragment events
  * merge into existing matcher group dedupes by command
  * unmerge of a fragment exactly inverts merge for the no-prior-state case
  * unmerge of an entry whose hooks all get stripped removes the entry
  * unmerge leaves unrelated entries untouched
"""
import importlib.util
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "install" / "merge_settings.py"


def _load():
    spec = importlib.util.spec_from_file_location("merge_settings", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _frag_core():
    return {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit|Write|NotebookEdit", "hooks": [
                    {"type": "command", "command": "python3 X/anti-slop.py", "timeout": 10},
                    {"type": "command", "command": "python3 X/tdd.py", "timeout": 10},
                ]}
            ],
            "Stop": [
                {"hooks": [
                    {"type": "command", "command": "python3 X/stop.py", "timeout": 10}
                ]}
            ],
        }
    }


def _frag_rust():
    return {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit|Write|NotebookEdit", "hooks": [
                    {"type": "command", "command": "python3 X/rationale-rust.py", "timeout": 10},
                ]}
            ]
        }
    }


def test_merge_into_empty():
    m = _load()
    out = m.merge({}, _frag_core())
    assert "hooks" in out
    assert "PostToolUse" in out["hooks"]
    cmds = [h["command"] for h in out["hooks"]["PostToolUse"][0]["hooks"]]
    assert cmds == ["python3 X/anti-slop.py", "python3 X/tdd.py"]


def test_merge_idempotent():
    m = _load()
    once = m.merge({}, _frag_core())
    twice = m.merge(once, _frag_core())
    assert once == twice


def test_merge_pack_extends_matcher_group():
    m = _load()
    base = m.merge({}, _frag_core())
    extended = m.merge(base, _frag_rust())
    cmds = [h["command"] for h in extended["hooks"]["PostToolUse"][0]["hooks"]]
    assert cmds == [
        "python3 X/anti-slop.py",
        "python3 X/tdd.py",
        "python3 X/rationale-rust.py",
    ]


def test_merge_preserves_unrelated_event():
    m = _load()
    existing = {"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "python3 user/foo.py", "timeout": 5}
    ]}]}}
    out = m.merge(existing, _frag_core())
    assert out["hooks"]["UserPromptSubmit"] == existing["hooks"]["UserPromptSubmit"]
    assert "PostToolUse" in out["hooks"]


def test_unmerge_inverts_merge_from_empty():
    m = _load()
    after_merge = m.merge({}, _frag_core())
    after_unmerge = m.unmerge(after_merge, _frag_core())
    # When all hooks are removed, the events get pruned
    assert after_unmerge.get("hooks", {}) == {}


def test_unmerge_leaves_unrelated_entries():
    m = _load()
    existing = {"hooks": {"PostToolUse": [
        {"matcher": "Edit|Write|NotebookEdit", "hooks": [
            {"type": "command", "command": "python3 mine/keep.py", "timeout": 10},
            {"type": "command", "command": "python3 X/anti-slop.py", "timeout": 10},
        ]},
    ]}}
    out = m.unmerge(existing, _frag_core())
    cmds = [h["command"] for h in out["hooks"]["PostToolUse"][0]["hooks"]]
    assert cmds == ["python3 mine/keep.py"]


def test_unmerge_pack_only():
    m = _load()
    # core + rust both installed; uninstalling rust must leave core hooks intact
    state = m.merge({}, _frag_core())
    state = m.merge(state, _frag_rust())
    after = m.unmerge(state, _frag_rust())
    cmds = [h["command"] for h in after["hooks"]["PostToolUse"][0]["hooks"]]
    assert cmds == ["python3 X/anti-slop.py", "python3 X/tdd.py"]
    assert "Stop" in after["hooks"]


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
