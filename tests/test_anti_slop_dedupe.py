"""
Tests for per-session, per-(check_id, file_path) dedupe in
bb-anti-slop-scan.py. The same check firing on the same file repeatedly
within one session should produce at most one reminder — addresses
'reminder noise' user feedback (rescue-without-stacktrace fired N
times for the same line).
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-anti-slop-scan.py"


def _load(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_anti_slop_scan", None)
    spec = importlib.util.spec_from_file_location(
        "bb_anti_slop_scan", HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_catalog(home, regex):
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "bb-anti-slop-patterns.json").write_text(json.dumps({
        "universal": {"extensions": [], "checks": []},
        "rust": {
            "extensions": [".rs"],
            "checks": [{
                "id": "test-check",
                "cite": "test §1",
                "severity": "warn",
                "regex": regex,
                "message": "Test message",
            }],
        },
    }))


def test_first_match_emits_reminder():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, r"\bbad_token\b")
        target = home / "lib.rs"
        target.write_text("fn main() { let x = bad_token; }\n")
        m = _load(home)
        matches = m.scan_file(str(target), m.load_patterns())
        assert len(matches) == 1
        # Now apply the dedupe filter
        kept = m.filter_already_seen("session-A", str(target), matches)
        assert len(kept) == 1


def test_repeat_match_in_same_session_is_silenced():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, r"\bbad_token\b")
        target = home / "lib.rs"
        target.write_text("fn main() { let x = bad_token; }\n")
        m = _load(home)
        matches = m.scan_file(str(target), m.load_patterns())

        first = m.filter_already_seen("session-A", str(target), matches)
        assert len(first) == 1
        # Mark as seen
        m.mark_seen("session-A", str(target), first)
        # Same session, same file, same check → silenced
        second = m.filter_already_seen("session-A", str(target), matches)
        assert second == []


def test_different_session_still_fires():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, r"\bbad_token\b")
        target = home / "lib.rs"
        target.write_text("fn main() { let x = bad_token; }\n")
        m = _load(home)
        matches = m.scan_file(str(target), m.load_patterns())
        kept = m.filter_already_seen("session-A", str(target), matches)
        m.mark_seen("session-A", str(target), kept)
        # New session ID → starts fresh
        new = m.filter_already_seen("session-B", str(target), matches)
        assert len(new) == 1


def test_different_file_still_fires_in_same_session():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_catalog(home, r"\bbad_token\b")
        f1 = home / "a.rs"
        f1.write_text("fn a() { bad_token; }\n")
        f2 = home / "b.rs"
        f2.write_text("fn b() { bad_token; }\n")
        m = _load(home)
        m1 = m.scan_file(str(f1), m.load_patterns())
        m.mark_seen("session-A", str(f1), m.filter_already_seen("session-A", str(f1), m1))
        # Same session, DIFFERENT file → still fires
        m2 = m.scan_file(str(f2), m.load_patterns())
        kept = m.filter_already_seen("session-A", str(f2), m2)
        assert len(kept) == 1


def test_different_check_id_still_fires_for_same_file():
    """Two distinct check ids on the same file: each fires once. Marking
    one as seen doesn't silence the other."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        hooks = home / ".claude" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "bb-anti-slop-patterns.json").write_text(json.dumps({
            "universal": {"extensions": [], "checks": []},
            "rust": {
                "extensions": [".rs"],
                "checks": [
                    {"id": "check-A", "cite": "x", "severity": "warn",
                     "regex": r"\btoken_a\b", "message": "A"},
                    {"id": "check-B", "cite": "x", "severity": "warn",
                     "regex": r"\btoken_b\b", "message": "B"},
                ],
            },
        }))
        target = home / "lib.rs"
        target.write_text("fn x() { token_a; token_b; }\n")
        m = _load(home)
        matches = m.scan_file(str(target), m.load_patterns())
        assert len(matches) == 2
        # Mark check-A as seen, scan again
        seen_A = [x for x in matches if x["check_id"] == "check-A"]
        m.mark_seen("session-A", str(target), seen_A)
        kept = m.filter_already_seen("session-A", str(target), matches)
        kept_ids = sorted(x["check_id"] for x in kept)
        assert kept_ids == ["check-B"]


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
