# BB-skill-core

Language-independent foundation for the BadBeta skill + hook ecosystem. Provides:

- **`bb-skill-enforcement.py`** — `[use-skills]` marker activation, recent-window detection, slash-command awareness
- **`bb-anti-slop-scan.py`** — PostToolUse pattern scanner with `.d/` plug-in support
- **`bb-tdd-state-hook.py`** — TDD state gate
- **`bb-stop-review-check.py`** — review-on-Stop reminder
- **`bb-milestone-commit-check.py`** — milestone-commit guard for long-running projects
- **`bb-sweep-rationale-markers.sh`** — strip `// §§` rationale markers before commit

Plus the universal slop-pattern catalog and language-independent skill-trigger keywords.

This package is the foundation that **`rust-phase-skills`** and **`elixir-phase-skills`** layer on top of via the `.d/` plug-in directories.

## Install

```bash
git clone https://github.com/BadBeta/BB-skill-core.git
cd BB-skill-core
./install.sh
```

This places the hooks under `~/.claude/hooks/` and merges the core hook entries into `~/.claude/settings.json`. Re-running is idempotent.

Override the install root with `CLAUDE_HOME=/some/other/path`.

## Uninstall

```bash
./uninstall.sh
```

Refuses if a language pack (rust-phase-skills, elixir-phase-skills) is still installed — uninstall those first.

## Architecture

The two largest hooks (`bb-anti-slop-scan.py`, `bb-skill-enforcement.py`) load their rule catalogs from the base file PLUS any `*.json` files in a `.d/` plug-in directory:

```
~/.claude/hooks/
  bb-anti-slop-patterns.json          ← universal patterns (this repo)
  bb-anti-slop-patterns.d/
    rust.json                         ← from rust-phase-skills
    elixir.json                       ← from elixir-phase-skills
  bb-skill-triggers.json              ← universal triggers (this repo)
  bb-skill-triggers.d/
    rust.json                         ← from rust-phase-skills
    elixir.json                       ← from elixir-phase-skills
```

This means language packs add to the catalog without touching the core file — no merge conflicts, clean install/uninstall per language.

## Tests

```bash
python3 tests/test_anti_slop_dropin_merge.py
python3 tests/test_skill_triggers_dropin_merge.py
python3 tests/test_merge_settings.py
```

## Layout

| Path | Purpose |
|---|---|
| `hooks/` | Hook scripts and base catalog files |
| `install/merge_settings.py` | Idempotent settings.json fragment merger (used by all 3 packs) |
| `settings-fragment.json` | Hook entries this pack registers |
| `install.sh` / `uninstall.sh` | Pack lifecycle |
| `VERSION` | Pack version (language packs pin a minimum via `REQUIRES_CORE`) |
| `tests/` | Pack tests |

→ Full architecture and how-to-extend reference: [User_Guide.md](User_Guide.md).
