# User Guide — BB skill-and-hook ecosystem

This guide covers the runtime architecture of the three-package skill /
hook system and the recipes for extending it: adding a new language
pack, a new hook, a new anti-slop pattern, or a new skill-trigger
keyword.

## 1. Architecture at a glance

Three independently installable packs, each its own GitHub repo:

| Pack | Repo (filesystem) | What it ships |
|---|---|---|
| **BB-skill-core** | `~/Projects/BB-skill-core/` | Language-independent hooks, universal slop catalog, language-independent skill triggers, the `merge_settings.py` settings-fragment merger. Required by everything else. |
| **rust-phase-skills** | `~/Projects/rust-phase-skills/` | The three Rust phase skills + Rust/C anti-slop drop-in + Rust skill-trigger drop-in + `bb-rationale-marker-rust.py` + `bb-no-std-build-check.py`. |
| **elixir-phase-skills** | `~/Projects/elixir-phase-skills/` | The three Elixir phase skills + `phoenix` + `phoenix-liveview` + Elixir anti-slop drop-in + Elixir skill-trigger drop-in + `bb-rationale-marker-elixir.py`. |

All three install into `~/.claude/` (override with `CLAUDE_HOME=/some/path`).

### What lives where after install

```
~/.claude/
├── settings.json                        ← hook entries from all 3 packs (merged)
├── BB-skill-core.VERSION                ← stamped by core install
├── rust-phase-skills.VERSION            ← stamped by rust install (if installed)
├── elixir-phase-skills.VERSION          ← stamped by elixir install (if installed)
├── install/
│   └── merge_settings.py                ← the idempotent settings merger (from core)
├── hooks/
│   ├── bb-anti-slop-scan.py             ← core: PostToolUse pattern scan
│   ├── bb-anti-slop-patterns.json       ← core: universal patterns only
│   ├── bb-anti-slop-patterns.d/         ← drop-in directory
│   │   ├── rust.json                    ← from rust-phase-skills
│   │   └── elixir.json                  ← from elixir-phase-skills
│   ├── bb-skill-enforcement.py          ← core: [use-skills] marker, recent-window
│   ├── bb-skill-triggers.json           ← core: language-independent keywords
│   ├── bb-skill-triggers.d/             ← drop-in directory
│   │   ├── rust.json                    ← from rust-phase-skills
│   │   └── elixir.json                  ← from elixir-phase-skills
│   ├── bb-tdd-state-hook.py             ← core: TDD gate
│   ├── bb-stop-review-check.py          ← core: review-on-Stop reminder
│   ├── bb-milestone-commit-check.py     ← core: long-running-project guard
│   ├── bb-sweep-rationale-markers.sh    ← core: pre-commit §§ stripper
│   ├── bb-rationale-marker-rust.py      ← from rust-phase-skills
│   ├── bb-no-std-build-check.py         ← from rust-phase-skills
│   └── bb-rationale-marker-elixir.py    ← from elixir-phase-skills
└── skills/
    ├── rust-planning/  rust-implementing/  rust-reviewing/
    ├── elixir-planning/  elixir-implementing/  elixir-reviewing/
    ├── phoenix/  phoenix-liveview/
    └── …
```

### Pack-driven extension scope

The two large catalog hooks (`bb-anti-slop-scan.py`,
`bb-tdd-state-hook.py`) use **only the extensions declared by installed
packs**. Concretely:

* `bb-anti-slop-patterns.json`'s `universal` group has its `extensions`
  list **computed at load time** as the union of every language group
  found across the base file + drop-ins.
* `bb-tdd-state-hook.py`'s `IMPL_EXTENSIONS` is **computed at module
  load time** from the same data.

Result:

| Installed packs | `IMPL_EXTENSIONS` / universal coverage |
|---|---|
| Core only | `{}` (TDD hook silently no-ops, universal scans nothing) |
| + rust-phase-skills | `{.rs .c .h}` |
| + elixir-phase-skills | `{.ex .exs .heex .leex}` |
| Both | `{.rs .c .h .ex .exs .heex .leex}` |

Languages without an installed pack don't fire any hooks. This is the
reason Python (used only for hook scripts) doesn't trigger the TDD
gate or universal slop scan despite being a perfectly valid language —
no pack covers it.

### Settings.json merge model

Each pack ships a `settings-fragment.json` declaring the hook entries
it owns. `install/merge_settings.py` merges fragments into
`settings.json` idempotently:

* Two entries with the same `matcher` (or both with no matcher) are
  **combined** — their `hooks[]` lists are merged by `command` string.
* Re-running install is byte-identical.
* Uninstall removes only the commands whose strings appear in that
  pack's fragment, leaving entries from other packs untouched.

You can read the current settings shape at any time with:

```bash
jq .hooks ~/.claude/settings.json
```

### Hook lifecycle summary

| Hook | Event | What fires it |
|---|---|---|
| `bb-skill-enforcement.py` | UserPromptSubmit, PreToolUse | Every prompt; every non-exempt tool call. Detects `[use-skills]` marker, scans triggers, gates non-exempt tools until a Skill is invoked. |
| `bb-anti-slop-scan.py` | PostToolUse (Edit/Write/NotebookEdit) | Every file edit. Runs the union of pattern groups whose `extensions` match the path. |
| `bb-tdd-state-hook.py` | PostToolUse (Edit/Write/NotebookEdit) | Every file edit, but only acts on `IMPL_EXTENSIONS` files. Detects new public functions without a recent test edit. |
| `bb-rationale-marker-{rust,elixir}.py` | PostToolUse (Edit/Write/NotebookEdit) | Edits to `.rs` / `.ex` / `.exs`. Reminds about `// §§` rationale markers. |
| `bb-no-std-build-check.py` | PostToolUse (Edit/Write/NotebookEdit) | Edits to `.rs` files in a `no_std` crate. Re-runs the build. |
| `bb-milestone-commit-check.py` | PreToolUse (Bash) | Bash commands. Specifically guards against premature `M\d+:` milestone commits. |
| `bb-stop-review-check.py` | Stop | Session end. Reminds to review changes / sync source. |

## 2. Install / uninstall

### Install everything (interactive)

```bash
git clone https://github.com/BadBeta/BB-skill-core.git ; cd BB-skill-core ; ./install.sh ; cd ..
git clone https://github.com/BadBeta/rust-phase-skills.git ; cd rust-phase-skills ; ./install.sh ; cd ..
git clone https://github.com/BadBeta/elixir-phase-skills.git ; cd elixir-phase-skills ; ./install.sh
```

A language-pack installer auto-detects core; if missing, it prompts to
clone+install core first (set `BB_NONINTERACTIVE=1` to fail-fast
instead). Override the source repo with `BB_CORE_REPO=...`.

### Uninstall

Each pack has its own `uninstall.sh`. The core uninstaller refuses
while a language pack is still installed:

```bash
~/Projects/elixir-phase-skills/uninstall.sh
~/Projects/rust-phase-skills/uninstall.sh
~/Projects/BB-skill-core/uninstall.sh         # only after both above
```

### Test the install in a sandbox

```bash
rm -rf /tmp/sandbox && mkdir -p /tmp/sandbox/.claude
HOME=/tmp/sandbox CLAUDE_HOME=/tmp/sandbox/.claude ~/Projects/BB-skill-core/install.sh
HOME=/tmp/sandbox CLAUDE_HOME=/tmp/sandbox/.claude BB_NONINTERACTIVE=1 ~/Projects/rust-phase-skills/install.sh
HOME=/tmp/sandbox CLAUDE_HOME=/tmp/sandbox/.claude BB_NONINTERACTIVE=1 ~/Projects/elixir-phase-skills/install.sh
```

Useful for verifying changes before deploying to your real `~/.claude`.

## 3. How to add a new language pack

Use `rust-phase-skills` or `elixir-phase-skills` as the template.

1. **Create the repo skeleton:**
   ```
   my-language-phase-skills/
   ├── README.md
   ├── VERSION                   ← e.g. "0.1.0"
   ├── REQUIRES_CORE             ← e.g. "0.1.0"
   ├── install.sh                ← copy from rust-phase-skills, change SKILL list
   ├── uninstall.sh              ← copy from rust-phase-skills, change SKILL list
   ├── settings-fragment.json    ← hook entries this pack registers (or {} if none)
   ├── hooks/
   │   ├── bb-anti-slop-patterns.d/
   │   │   └── my-language.json
   │   └── bb-skill-triggers.d/
   │       └── my-language.json
   └── my-language-planning/  my-language-implementing/  my-language-reviewing/
   ```

2. **Fill `bb-anti-slop-patterns.d/my-language.json`:**
   ```json
   {
     "my-language": {
       "extensions": [".myl"],
       "checks": [
         {
           "id": "rule-id",
           "cite": "my-language-reviewing §X.Y",
           "severity": "warn",
           "regex": "anti-pattern-regex",
           "skip_if_in_file": "optional escape hatch",
           "skip_if_path_matches": "optional path filter",
           "message": "What the user should do instead."
         }
       ]
     }
   }
   ```
   `extensions` are auto-merged into the universal-group extension union.

3. **Fill `bb-skill-triggers.d/my-language.json`:**
   ```json
   {
     "keywords": {
       "framework-name": ["my-language-implementing"],
       "specific-keyword": ["my-language-planning"]
     }
   }
   ```
   Mixed keywords (e.g. `"refactor"`) can appear in multiple packs;
   the runtime merges and deduplicates.

4. **Edit `install.sh`** to copy your skill directories and any
   language-specific hook scripts into `~/.claude/`. Use the rust
   installer's structure verbatim.

5. **Edit `settings-fragment.json`** to register any new hook scripts
   (omit if you don't add any). Keep entries small — the merger only
   merges, it doesn't unify two separately-formatted entries with the
   same matcher.

6. **Test in a sandbox** (see §2). Verify both install and uninstall.

7. **Commit and push.**

The runtime requires no changes to add a pack — the drop-in pattern
means hooks discover the new fragment automatically.

## 4. How to add a new hook

A hook is a Python script that reads JSON from stdin, optionally
writes JSON to stdout, and exits 0. The runtime guarantees:

* `hook_event_name` field tells the script which event fired.
* `tool_name` and `tool_input` contain the tool that triggered it.
* Stderr is shown only on non-zero exit; stdout (if valid JSON) becomes
  `additionalContext` for the LLM.

### Choose a pack

* **Universal** (matters for any language) → BB-skill-core
* **Language-specific** → that language's pack

### Add the file

1. Place the script under `<pack>/hooks/bb-<descriptive-name>.py`,
   `chmod +x` it.
2. Read the JSON event payload, decide whether to act, write the
   reminder to stdout if relevant.
3. **Always exit 0** — a non-zero exit blocks the LLM. Print
   diagnostics to stderr.
4. **Always fail open** — wrap the body in `try/except` that catches
   everything and returns. A broken hook must never brick the session.

### Register in `settings-fragment.json`

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 $HOME/.claude/hooks/bb-my-hook.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

`matcher` is a regex over tool names. Omit it for "always-fire".
Pick the smallest event scope that solves the problem; PostToolUse
is the right place for "scan what was just written".

### Update install / uninstall

The pack-level `install.sh` already glob-copies `hooks/*.py` into
`~/.claude/hooks/` for the core pack; for a language pack, add an
explicit `cp -p` line for the new script. Mirror in `uninstall.sh`
with `rm -f`.

### Test

`echo '{...event payload...}' | python3 hooks/bb-my-hook.py` —
verify it emits valid JSON and exits 0 in both fire-and-noop cases.
Then sandbox-install and try the actual scenario.

## 5. How to add a new anti-slop pattern

1. Decide which group it belongs to:
   * `universal` (matters in any language) → core's
     `bb-anti-slop-patterns.json`
   * Language-specific → the language pack's drop-in,
     e.g. `rust-phase-skills/hooks/bb-anti-slop-patterns.d/rust.json`

2. Append a check object to the group's `checks` array:
   ```json
   {
     "id": "kebab-case-rule-id",
     "cite": "<lang>-reviewing §X.Y / §7b #N",
     "severity": "block",
     "regex": "the\\.matching\\.pattern",
     "skip_if_in_file": "optional regex — file-wide opt-out",
     "skip_if_path_matches": "optional regex — path opt-out",
     "only_if_path_matches": "optional regex — restrict to certain paths",
     "requires_missing_in_file": "optional regex — only fire if absent",
     "message": "Plain-English remediation. Tells Claude what to do instead."
   }
   ```

3. **Severity:**
   * `block` — fix-before-stop. Hook injects a strong reminder.
   * `warn` — likely slop, has legit uses. Gentle reminder.
   The cost of a false positive is high — start with `warn` and
   promote to `block` only if the pattern proves hard for the LLM to
   work around.

4. **Regex tips:**
   * Anchor with `(?m)^` for line-leading patterns.
   * Be conservative — a regex that matches `String` will fire on docs,
     comments, identifiers. Use `\b` boundaries.
   * For language-specific tokens, use the actual language's syntax in
     the regex (e.g. `\.unwrap\(\)` for Rust).

5. **Inline opt-out:** code can suppress a finding with
   `// RULE-EXCEPTION: <id-or-all> — <reason>` (or `# `, or `<!-- -->`)
   on the offending line or within ±2 lines.

6. **Test:** in a sandbox, write a file containing both a positive
   match and a negative case, run the hook by hand:
   ```bash
   echo '{
     "hook_event_name":"PostToolUse",
     "tool_name":"Write",
     "tool_input":{"file_path":"/tmp/probe.rs"}
   }' | python3 ~/.claude/hooks/bb-anti-slop-scan.py
   ```

7. Commit. The drop-in pattern means the runtime picks up the new
   check on the next prompt — no restart required.

## 6. How to add a new skill-trigger keyword

Skill triggers turn user-prompt vocabulary into "you should be using
skill X" hints. The hook fires before tool use and tells Claude
which skill matches the prompt.

1. Pick the right file:
   * Language-independent (e.g. `mermaid`, `svg`, `skill-authoring`)
     → `BB-skill-core/hooks/bb-skill-triggers.json`
   * Language-specific → the language pack's drop-in.

2. Append to the `keywords` map:
   ```json
   {
     "keywords": {
       "your-keyword": ["target-skill"],
       "another-keyword": ["skill-a", "skill-b"]
     }
   }
   ```
   * Keywords are case-insensitive substring matches against the
     prompt text.
   * Multiple skills per keyword is fine — the runtime suggests all of
     them.

3. **Cross-language keywords** (`refactor`, `review`, `architecture`,
   `planning`): each pack declares its own slice of the skill list.
   The merger concatenates and deduplicates at runtime, so when
   both packs are installed `"refactor"` correctly resolves to all
   five rust-and-elixir refactor-relevant skills.

4. **Test:** drop the file in place, then in a Claude session use the
   keyword in a prompt — the hook should suggest the right skill.

## 7. Common gotchas

* **Editing the deployed catalog directly:** `~/.claude/hooks/...` is
  the **runtime** copy. Edits there will be overwritten on the next
  install. Always edit the source repo (`~/Projects/<pack>/...`) and
  re-run that pack's `install.sh`.

* **Settings drift:** if you hand-edit `~/.claude/settings.json` to
  change a hook's `timeout`, the next install will preserve your edit
  only if the command string is identical. Re-running install is
  idempotent for the entries that match by command; new commands get
  appended.

* **Phantom hook fire:** if a hook fires when it shouldn't (e.g. on
  a file extension you don't expect), check whether some pack's
  drop-in declares that extension. The TDD hook and universal slop
  group are pack-driven — extensions only enter the union from a
  declared `.d/` fragment.

* **Test before pushing:** sandbox-install (§2) catches install /
  uninstall regressions cheaply. The merger is idempotency-tested,
  but custom install logic isn't.

## 8. Where to look for…

| Need | File |
|---|---|
| The language-independent rules and triggers | `BB-skill-core/hooks/bb-anti-slop-patterns.json`, `BB-skill-core/hooks/bb-skill-triggers.json` |
| Rust/C anti-slop patterns and triggers | `rust-phase-skills/hooks/bb-anti-slop-patterns.d/rust.json`, `rust-phase-skills/hooks/bb-skill-triggers.d/rust.json` |
| Elixir / Phoenix patterns and triggers | `elixir-phase-skills/hooks/bb-anti-slop-patterns.d/elixir.json`, `elixir-phase-skills/hooks/bb-skill-triggers.d/elixir.json` |
| The settings merger's algorithm | `BB-skill-core/install/merge_settings.py` |
| The hook event names + matcher syntax | Existing `settings-fragment.json` files in each pack |
| Tests | `BB-skill-core/tests/` — 20 tests covering both drop-in mergers and the settings merger |
| Rollback | `~/Projects/skill_hooks_mechanics/` — frozen monolithic snapshot of the pre-split state |
