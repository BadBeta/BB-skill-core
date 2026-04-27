"""
Tests for the tightened `try-rescue-for-expected-failure` regex in
elixir-phase-skills/hooks/bb-anti-slop-patterns.d/elixir.json.

Old regex: `try\\s+do[\\s\\S]{0,300}?rescue` — fired on every try/rescue,
including the idiomatic typed-rescue shapes.

New regex: `(?m)try\\s+do[\\s\\S]{0,300}?^\\s*rescue\\s+[a-z_][a-zA-Z0-9_]*\\s*->` —
fires only when rescue is bound to a lowercase variable WITHOUT an
`in Module` clause. That's the genuine smell shape (catch-and-return-
{:error, _} instead of using `case`). Module-typed rescues stay quiet.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "bb-anti-slop-scan.py"


def _load(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_anti_slop_scan", None)
    spec = importlib.util.spec_from_file_location("bb_anti_slop_scan", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECK = {
    "id": "try-rescue-for-expected-failure",
    "cite": "elixir §error-handling",
    "severity": "warn",
    "regex": r"(?m)try\s+do[\s\S]{0,300}?^\s*rescue\s+[a-z_][a-zA-Z0-9_]*\s*->",
    "message": "test"
}


def _seed(home):
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "bb-anti-slop-patterns.json").write_text(json.dumps({
        "universal": {"extensions": [], "checks": []},
    }))
    drop = hooks / "bb-anti-slop-patterns.d"
    drop.mkdir(exist_ok=True)
    (drop / "elixir.json").write_text(json.dumps({
        "elixir": {"extensions": [".ex", ".exs"], "checks": [CHECK]},
    }))


def _scan(home, content):
    f = home / "target.ex"
    f.write_text(content)
    m = _load(home)
    return m.scan_file(str(f), m.load_patterns())


def _fired(matches):
    return any(m["check_id"] == "try-rescue-for-expected-failure" for m in matches)


# ── Positive: shapes that ARE the smell ──────────────────────────────

def test_rescue_variable_no_module_fires():
    """rescue e -> ... — variable, no `in Module` — the smell shape."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f do\n"
            "    try do\n"
            "      Foo.bang!()\n"
            "    rescue\n"
            "      e ->\n"
            "        {:error, e}\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert _fired(matches)


def test_rescue_underscore_variable_fires():
    """rescue _e -> ... — underscored variable, still no `in Module`."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "try do\n"
            "  Foo.bang!()\n"
            "rescue\n"
            "  _e -> :err\n"
            "end\n"
        )
        assert _fired(matches)


def test_rescue_short_variable_fires():
    """Single-letter variable — `rescue e ->` — still smell."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "try do\n"
            "  thing()\n"
            "rescue\n"
            "  x -> {:error, x}\n"
            "end\n"
        )
        assert _fired(matches)


# ── Negative: idiomatic shapes that must NOT fire ────────────────────

def test_rescue_typed_module_does_not_fire():
    """rescue Ecto.NoResultsError -> ... — typed rescue is legitimate."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "try do\n"
            "  Repo.get!(User, id)\n"
            "rescue\n"
            "  Ecto.NoResultsError -> {:error, :not_found}\n"
            "end\n"
        )
        assert not _fired(matches), \
            "rescue with explicit module type should not fire"


def test_rescue_variable_in_module_does_not_fire():
    """rescue e in Postgrex.Error -> reraise — legitimate typed-with-binding."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "try do\n"
            "  thing()\n"
            "rescue\n"
            "  e in Postgrex.Error ->\n"
            "    reraise wrap_error(e), __STACKTRACE__\n"
            "end\n"
        )
        assert not _fired(matches), \
            "rescue e in Module should not fire"


def test_rescue_variable_in_module_list_does_not_fire():
    """rescue e in [Foo, Bar] -> ... — multi-type typed-with-binding."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "try do\n"
            "  do_thing()\n"
            "rescue\n"
            "  e in [Postgrex.Error, DBConnection.ConnectionError] ->\n"
            "    reraise e, __STACKTRACE__\n"
            "end\n"
        )
        assert not _fired(matches)


def test_no_try_rescue_in_file_does_not_fire():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x) do\n"
            "    case Foo.bar(x) do\n"
            "      {:ok, v} -> v\n"
            "      {:error, e} -> e\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert not _fired(matches)


def test_module_only_rescue_does_not_fire():
    """rescue ArgumentError -> ... — bare module name, no variable."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); _seed(home)
        matches = _scan(home,
            "try do\n"
            "  String.to_integer(s)\n"
            "rescue\n"
            "  ArgumentError -> {:error, :not_a_number}\n"
            "end\n"
        )
        assert not _fired(matches)


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
