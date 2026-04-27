"""
Tests for the three new Elixir 'else'-shape anti-slop checks:
  - unless-with-else (block) — both block AND keyword forms
  - if-elseif-chain (warn)   — `else if` chain start
  - try-with-else (warn)     — try do ... else

Each test feeds a synthetic .ex file through scan_file() with a
fixture catalog containing only the new check, and asserts whether
the check fires.
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


def _seed(home, check):
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "bb-anti-slop-patterns.json").write_text(json.dumps({
        "universal": {"extensions": [], "checks": []},
    }))
    drop = hooks / "bb-anti-slop-patterns.d"
    drop.mkdir(exist_ok=True)
    (drop / "elixir.json").write_text(json.dumps({
        "elixir": {"extensions": [".ex", ".exs"], "checks": [check]},
    }))


def _scan(home, content, ext=".ex"):
    f = home / f"target{ext}"
    f.write_text(content)
    m = _load(home)
    return m.scan_file(str(f), m.load_patterns())


# ── unless-with-else ─────────────────────────────────────────────────

UNLESS_CHECK = {
    "id": "unless-with-else",
    "cite": "elixir-reviewing §control-flow (unless+else inverts twice)",
    "severity": "block",
    "regex": r"\bunless\b[^\n]*,\s*else:|\bunless\b[^\n]*\bdo\b[\s\S]{0,1500}?\belse\b",
    "message": "`unless ... else` inverts the condition twice. Invert and use `if/else`. Credo's UnlessWithElse rule covers this."
}


def test_unless_else_block_form_fires():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, UNLESS_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x) do\n"
            "    unless x do\n"
            "      :a\n"
            "    else\n"
            "      :b\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert any(m["check_id"] == "unless-with-else" for m in matches)


def test_unless_else_keyword_form_fires():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, UNLESS_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x), do: unless x, do: :a, else: :b\n"
            "end\n"
        )
        assert any(m["check_id"] == "unless-with-else" for m in matches)


def test_unless_without_else_does_not_fire():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, UNLESS_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x) do\n"
            "    unless x do\n"
            "      :only_negative_branch\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert matches == []


# ── if-elseif-chain ──────────────────────────────────────────────────

ELSEIF_CHECK = {
    "id": "if-elseif-chain",
    "cite": "elixir-reviewing §control-flow (use cond for chains)",
    "severity": "warn",
    "regex": r"(?m)^\s*else\s+if\b",
    "message": "`else if` chain. Use `cond do` — flatter, Elixir-idiomatic, and each branch is its own predicate-action pair."
}


def test_elseif_chain_fires():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, ELSEIF_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x) do\n"
            "    if x == 1 do\n"
            "      :one\n"
            "    else\n"
            "      if x == 2 do\n"
            "        :two\n"
            "      else\n"
            "        :other\n"
            "      end\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert any(m["check_id"] == "if-elseif-chain" for m in matches)


def test_with_else_does_not_fire_elseif():
    """`with ... else` is the canonical idiom and must NOT trip this check."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, ELSEIF_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(id) do\n"
            "    with {:ok, u} <- fetch_user(id),\n"
            "         {:ok, p} <- fetch_post(u) do\n"
            "      {:ok, p}\n"
            "    else\n"
            "      {:error, _} = e -> e\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert matches == []


def test_simple_if_else_does_not_fire():
    """Plain `if/else` without chained `if` is fine."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, ELSEIF_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x), do: if x, do: :a, else: :b\n"
            "end\n"
        )
        assert matches == []


# ── try-with-else ────────────────────────────────────────────────────

TRY_ELSE_CHECK = {
    "id": "try-with-else",
    "cite": "elixir-reviewing §error-handling (try/else is rarely justified)",
    "severity": "warn",
    "regex": r"\btry\b\s+do\b[\s\S]{0,2000}?\belse\b",
    "message": "`try do ... else` is rarely justified outside library code. The function the `try` wraps should return `{:ok, _} | {:error, _}` and you should `case` it."
}


def test_try_else_fires():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, TRY_ELSE_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x) do\n"
            "    try do\n"
            "      do_thing(x)\n"
            "    rescue\n"
            "      _ -> :err\n"
            "    else\n"
            "      result -> {:ok, result}\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert any(m["check_id"] == "try-with-else" for m in matches)


def test_with_else_does_not_fire_try():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, TRY_ELSE_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(id) do\n"
            "    with {:ok, u} <- fetch(id) do\n"
            "      u\n"
            "    else\n"
            "      e -> e\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert matches == []


def test_try_rescue_without_else_does_not_fire():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed(home, TRY_ELSE_CHECK)
        matches = _scan(home,
            "defmodule X do\n"
            "  def f(x) do\n"
            "    try do\n"
            "      do_thing(x)\n"
            "    rescue\n"
            "      _ -> :err\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        assert matches == []


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
