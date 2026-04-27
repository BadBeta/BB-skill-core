#!/usr/bin/env python3
"""
TDD state hook for Claude Code.

PostToolUse hook on Edit/Write/NotebookEdit. Tracks, per session, which
files were edited and when, with a language-aware classification of
test vs. production code. On each edit to *production* code that
introduces a NEW public function signature, the hook checks whether a
test file in the same project has been edited recently in this session.
If not, it emits a warn-level reminder that the TDD gate (see
rust-implementing §0 / elixir-implementing §0) was not followed
mechanically, even if it was followed in spirit.

The hook is an evidence layer, not a blocker: it never fails the tool
call, it just injects a system reminder. The intent is to close the
gap between "Rule 0 says tests first" (aspirational) and "tests first
is empirically the case" (observable).

Two false-positive suppressions worth knowing:
  1. Same-file test co-location. If the edited production file ALSO
     contains a `#[cfg(test)] mod tests`/`defmodule …Test`/`def test_…`
     block, the hook treats the file as test-touching even when the
     specific edit was to the production half. Rust's idiomatic
     "tests in the same file" pattern (also common in Elixir doctests)
     would otherwise trigger the warning constantly.
  2. Per-session verbosity. The first fire in a session shows the full
     reminder; subsequent fires are a one-liner pointing at §0. Reduces
     fatigue without losing the signal.

State file: ~/.claude/cache/tdd-state/<session_id>.json
  {
    "edits": [
      { "path": "src/foo.rs", "kind": "impl", "ts": 1234567890.5 },
      { "path": "src/foo_test.rs", "kind": "test", "ts": 1234567892.1 },
      ...
    ],
    "fire_count": 2
  }

Last ~200 edits per session are kept; the file is capped to avoid
unbounded growth across long sessions.

Fails open: any exception exits 0 so the session is never bricked.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "cache" / "tdd-state"
MAX_EDITS = 200

# Tools that modify files; we only track these.
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}

# Test-file classification by path and/or name. A file is "test" if any
# of these match; otherwise "impl" (production code) or "other" (docs,
# config, etc.).
TEST_PATH_PATTERNS = [
    re.compile(r"(^|/)tests?/"),               # tests/ or test/ directory
    re.compile(r"_test\.(rs|ex|exs|go)$"),    # Rust integration, Go
    re.compile(r"\.test\.(ts|tsx|js|jsx)$"),  # JS/TS
    re.compile(r"_spec\.(rb|ex|exs)$"),       # RSpec-ish, Elixir spec
    re.compile(r"/__tests__/"),               # Jest convention
]

# File extensions we consider "source code" for TDD-applicability.
# Production-code matches trigger the check; everything else is ignored.
#
# Pack-driven: this set is the union of `extensions` declared by all
# anti-slop pattern fragments under bb-anti-slop-patterns.d/*.json
# (excluding the "universal" group, which is itself the union). With
# only rust-phase-skills + elixir-phase-skills installed, this resolves
# to {.rs .ex .exs .c .h}. With no language packs installed, this is
# empty and the TDD hook silently no-ops — exactly the right behaviour
# for a core-only install.
def _compute_impl_extensions():
    base = Path.home() / ".claude" / "hooks" / "bb-anti-slop-patterns.json"
    dropin_dir = Path.home() / ".claude" / "hooks" / "bb-anti-slop-patterns.d"
    exts = set()

    def _absorb(doc):
        if not isinstance(doc, dict):
            return
        for group_name, group in doc.items():
            if group_name.startswith("_") or group_name == "universal":
                continue
            if not isinstance(group, dict):
                continue
            for ext in (group.get("extensions") or []):
                exts.add(ext.lower())

    try:
        if base.exists():
            _absorb(json.loads(base.read_text() or "{}"))
    except Exception:
        pass
    if dropin_dir.is_dir():
        for path in sorted(dropin_dir.glob("*.json")):
            try:
                _absorb(json.loads(path.read_text() or "{}"))
            except Exception:
                continue
    return exts


IMPL_EXTENSIONS = _compute_impl_extensions()

# Production-code paths that are NOT test code.
IMPL_PATH_HINTS = [
    re.compile(r"(^|/)src/"),
    re.compile(r"(^|/)lib/"),
    re.compile(r"(^|/)examples/"),  # example binaries still count as impl
]

# Path patterns for files that are "infrastructure" — not application
# production code. Hook scripts, deployment configs, dotfiles, and similar
# tooling glue are tested via smoke fixtures and CI runs rather than
# co-located unit-test files. Suppressing the TDD warning on these paths
# eliminates a class of false positives where the hook would otherwise
# scold itself for editing its own logic.
INFRA_PATH_PATTERNS = [
    re.compile(r"/\.claude/hooks/"),         # deployed hook scripts
    re.compile(r"/skill_hooks_mechanics/"),  # canonical hook repo
    re.compile(r"/(?:rust|elixir)-phase-skills/"),  # canonical skill repos
    re.compile(r"/\.claude/skills/"),        # deployed skill scripts
    # Build/config files — infrastructure wiring, not unit-testable
    re.compile(r"mix\.exs$"),               # Elixir project definition
    re.compile(r"\.formatter\.exs$"),       # Elixir formatter config
    re.compile(r"/config/.*\.exs$"),        # Elixir config files
    re.compile(r"application\.ex$"),        # OTP application supervision tree
    re.compile(r"Cargo\.toml$"),            # Rust project definition
    re.compile(r"build\.rs$"),              # Rust build script
]

# Regexes per language for "new public function signature added".
# We check the Edit/Write payload for these to decide whether to even
# care about TDD on this edit. Private fns / docs / formatting don't
# trigger a warning.
NEW_PUBLIC_FN_PATTERNS = {
    ".rs": re.compile(
        r"(?m)^\s*pub\s+(?:async\s+)?fn\s+[a-zA-Z_]",
    ),
    # Elixir: `def ` (public function — `defp ` is private). Guards:
    # macro-like decorators are fine.
    ".ex": re.compile(r"(?m)^\s*def\s+[a-zA-Z_]"),
    ".exs": re.compile(r"(?m)^\s*def\s+[a-zA-Z_]"),
    # Python: top-level `def ` (not `_` prefix). Very rough.
    ".go": re.compile(r"(?m)^func\s+[A-Z]"),   # exported = uppercase
    # TS/JS: `export function` / `export const X =`. Very rough.
    ".ts": re.compile(r"(?m)^export\s+(?:async\s+)?function\s+[a-zA-Z]"),
    ".tsx": re.compile(r"(?m)^export\s+(?:async\s+)?function\s+[a-zA-Z]"),
    ".js": re.compile(r"(?m)^export\s+(?:async\s+)?function\s+[a-zA-Z]"),
    ".jsx": re.compile(r"(?m)^export\s+(?:async\s+)?function\s+[a-zA-Z]"),
}

# How recent is "recent" for a matching test edit, in seconds.
RECENT_TEST_WINDOW_S = 15 * 60  # 15 minutes

# Per-language regex for "this file co-locates tests" — i.e. it has a
# test block embedded alongside the production code. When present, the
# TDD warning is suppressed even if no separate test file was edited
# recently, because the file itself is test-touching.
SAME_FILE_TEST_PATTERNS = {
    ".rs":  re.compile(r"(?m)^\s*#\[cfg\(test\)\]"),
    ".ex":  re.compile(r"(?m)^\s*defmodule\s+\S+Test\s+do|^\s*doctest\s+"),
    ".exs": re.compile(r"(?m)^\s*defmodule\s+\S+Test\s+do|^\s*doctest\s+"),
    ".py":  re.compile(r"(?m)^def\s+test_[a-zA-Z]"),
    ".go":  re.compile(r"(?m)^func\s+Test[A-Z]"),
}

# Rustler NIF loader stubs: every public function delegates to
# `:erlang.nif_error(:nif_not_loaded)`. The Rustler macro patches in
# real implementations at module load time. Such files have no testable
# behavior in isolation — the wrapper module's ExUnit tests + the
# crate's `cargo test` cover the real surface. Treating these as
# TDD-relevant produces a false positive on every NIF crate.
NIF_USE_RUSTLER_RE = re.compile(r"(?m)^\s*use\s+Rustler[\s,]")
NIF_STUB_BODY_RE = re.compile(r":erlang\.nif_error\s*\(\s*:nif_not_loaded\s*\)")
ELIXIR_DEF_LINE_RE = re.compile(r"(?m)^\s*def\s+\w+\s*\(")


def classify(path_str):
    """Return 'test', 'impl', or 'other'."""
    lower = path_str.lower()
    for pat in TEST_PATH_PATTERNS:
        if pat.search(lower):
            return "test"
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if ext not in IMPL_EXTENSIONS:
        return "other"
    # Accept any source-extension file as impl, even outside src/lib/
    # directories — scripts, examples, etc.
    return "impl"


def project_root(path_str):
    """
    Walk up from a path until we find a project-root marker
    (Cargo.toml, mix.exs, pyproject.toml, package.json, go.mod).
    Fallback: the first directory ABOVE a recognised source-layout
    folder (src, lib, tests, test, examples, bin). This ensures
    src/foo.rs and tests/foo.rs resolve to the same project when
    no marker file exists (e.g., in smoke tests or loose repos).
    Last-resort fallback: the directory containing the file.
    """
    path = Path(path_str).resolve()
    if path.is_file():
        path = path.parent
    candidates = [path, *path.parents]
    for candidate in candidates:
        for marker in ("Cargo.toml", "mix.exs", "pyproject.toml",
                       "package.json", "go.mod"):
            if (candidate / marker).exists():
                return str(candidate)
        if candidate == candidate.parent:
            break
    # No marker found — try to find a conventional layout ancestor.
    for candidate in candidates:
        if candidate.name in {"src", "lib", "tests", "test", "examples", "bin"}:
            return str(candidate.parent)
    return str(path)


def load_state(session_id):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / f"{session_id}.json"
    if not f.exists():
        return {"edits": []}
    try:
        with open(f) as fh:
            return json.load(fh)
    except Exception:
        return {"edits": []}


def save_state(session_id, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / f"{session_id}.json"
    # Cap history
    edits = state.get("edits", [])
    if len(edits) > MAX_EDITS:
        state["edits"] = edits[-MAX_EDITS:]
    try:
        with open(f, "w") as fh:
            json.dump(state, fh)
    except Exception:
        pass


def content_has_new_public_fn(tool_input, ext):
    """
    Look at the payload Claude just wrote. For Write tool: full content;
    for Edit: the new_string. If a public function signature is present
    in what was written, count it — this is a rough approximation of
    'new public API'. False positives for existing-fn edits are OK;
    the check is a nudge, not a block.
    """
    pat = NEW_PUBLIC_FN_PATTERNS.get(ext)
    if pat is None:
        return False
    # Write tool: content lives at .content; Edit tool: at .new_string;
    # NotebookEdit: at .new_source.
    for key in ("new_string", "content", "new_source"):
        text = tool_input.get(key)
        if isinstance(text, str) and pat.search(text):
            return True
    return False


def is_nif_loader_stub_file(path, ext):
    """True if this Elixir file is a Rustler NIF loader stub: declares
    `use Rustler, ...` and every (or nearly every) public function body
    is `:erlang.nif_error(:nif_not_loaded)`. Such files have no isolated
    behavior to TDD — the real implementations come from Rustler at
    load time. Tests live in the wrapper module's ExUnit suite and in
    the Rust crate's `cargo test`.
    """
    if ext not in (".ex", ".exs"):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read(50_000)
    except OSError:
        return False
    if not NIF_USE_RUSTLER_RE.search(content):
        return False
    def_count = len(ELIXIR_DEF_LINE_RE.findall(content))
    stub_count = len(NIF_STUB_BODY_RE.findall(content))
    if def_count == 0:
        return False
    # Allow one helper (e.g., a `via/1` lookup) without disqualifying.
    return stub_count >= def_count - 1


def file_co_locates_tests(path, ext):
    """
    True if the post-edit file contents include a test block (Rust
    `#[cfg(test)]`, Elixir `defmodule …Test do`, Python `def test_*`,
    Go `func Test*`). Suppresses the TDD warning for the very common
    pattern of co-located unit tests where the production edit and
    the test edit happen in the same file — the original recent-test-
    file check can't see those because they don't bump a separate
    test-classified path.
    """
    pat = SAME_FILE_TEST_PATTERNS.get(ext)
    if pat is None:
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read(200_000)  # cap reads at 200KB
    except OSError:
        return False
    return bool(pat.search(content))


def recent_test_edit_in_project(state, project, now):
    for edit in reversed(state.get("edits", [])):
        if edit.get("kind") != "test":
            continue
        if edit.get("project") != project:
            continue
        if now - edit.get("ts", 0) <= RECENT_TEST_WINDOW_S:
            return edit
    return None


def record_edit(state, path, kind, project, now):
    state.setdefault("edits", []).append({
        "path": path,
        "kind": kind,
        "project": project,
        "ts": now,
    })


def is_infra_path(path):
    """Hook / skill / dotfile-tooling paths — out of scope for TDD."""
    for pat in INFRA_PATH_PATTERNS:
        if pat.search(path):
            return True
    return False


def handle(data):
    tool_name = data.get("tool_name") or ""
    if tool_name not in EDIT_TOOLS:
        return None
    session_id = data.get("session_id") or "unknown"
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not path:
        return None

    # Infrastructure paths (hook scripts, skill files) are exempt — they're
    # tested via JSON-payload smoke fixtures, not co-located unit tests.
    # Skip BEFORE classification so we don't even record the edit.
    if is_infra_path(path):
        return None

    kind = classify(path)
    if kind == "other":
        return None

    now = time.time()
    project = project_root(path)
    state = load_state(session_id)

    # Always record the edit so future check has context.
    record_edit(state, path, kind, project, now)
    save_state(session_id, state)

    if kind == "test":
        # Test edits are what we WANT to see; never warn on them.
        return None

    # kind == "impl"
    ext = "." + path.lower().rsplit(".", 1)[-1] if "." in path else ""
    if ext not in IMPL_EXTENSIONS:
        return None

    if not content_has_new_public_fn(tool_input, ext):
        # Edit didn't introduce a new public fn signature — likely a
        # tweak / refactor / doc change. Not TDD-relevant.
        return None

    # New public fn added to impl code. Check if a test in the same
    # project was edited recently.
    recent = recent_test_edit_in_project(state, project, now)
    if recent:
        return None  # TDD discipline evident — nothing to report

    # Same-file co-located tests? Common Rust pattern (`#[cfg(test)] mod
    # tests`), Elixir doctests, Python `def test_*` in the same module.
    # If the file itself contains a test block, the user's discipline
    # is likely test-first within this file — suppress the warning.
    if file_co_locates_tests(path, ext):
        return None

    # Rustler NIF loader stub: every public def is :erlang.nif_error
    # — no isolated behavior to test. Tests live in the wrapper module
    # and in the Rust crate's cargo tests.
    if is_nif_loader_stub_file(path, ext):
        return None

    # TDD gate violation (potentially). Warn, don't block. Track per-
    # session fire count so the second-and-onward fire is a one-liner
    # rather than the same multi-paragraph reminder.
    fire_count = int(state.get("fire_count", 0))
    state["fire_count"] = fire_count + 1
    save_state(session_id, state)

    if fire_count == 0:
        return (
            "TDD gate reminder — just wrote a new public function to "
            f"{path} but no test file in the same project has been "
            f"edited in the last {RECENT_TEST_WINDOW_S // 60} minutes "
            "of this session. If you wrote the test first and confirmed "
            "it failed, fine — the hook is heuristic and your discipline "
            "may be ahead of this check. If you did NOT, STOP: write "
            "the failing test now, run it, confirm red, THEN re-edit "
            "this file. See rust-implementing §0 / elixir-implementing "
            "§0 for the full workflow and the autonomous-mode warning."
            "\n\n"
            "The ONLY valid exceptions are: HEEx/EEx templates, CSS/styling, "
            "and one-off scripts outside lib/src/. A `def`/`pub fn` in "
            "lib/ or src/ is production code — 'glue code' and 'thin "
            "wrapper' are NOT valid exemptions. The review that catches "
            "bugs in untested code costs 10x more than the test that "
            "prevents them.\n\n"
            "Subsequent fires this session will be a one-liner."
        )
    return (
        f"TDD gate — new pub fn in {path}, no recent test edit "
        f"(fire #{fire_count + 1} this session). See implementing-skill §0."
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        msg = handle(data)
    except Exception as e:
        sys.stderr.write(f"tdd-state-hook error: {e}\n")
        return 0
    if msg:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": msg,
            },
            "systemMessage": msg,
        }
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
