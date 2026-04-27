"""
Tests for Bash-command-prefix exemption in bb-skill-enforcement.py.

Orientation commands (looking around — ls, pwd, which, find, tree,
cat <readonly file>, git status/log/diff/branch/remote/show, head, tail,
wc, file, stat) should NOT trigger skill enforcement. Mutation
commands (write, run, build, install, modify) still do.
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / "hooks" / "bb-skill-enforcement.py"


def _load():
    spec = importlib.util.spec_from_file_location("bb_skill_enf", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Cases the function MUST classify as orientation (return True)
ORIENTATION_OK = [
    "ls",
    "ls -la",
    "ls /home/vidar/Projects",
    "pwd",
    "which python3",
    "find . -name '*.ex'",
    "tree -L 2",
    "cat README.md",
    "cat /etc/os-release",
    "head -20 mix.exs",
    "tail -50 log.txt",
    "wc -l lib/foo.ex",
    "file /usr/bin/python3",
    "stat /tmp/foo",
    "git status",
    "git status --short",
    "git log --oneline",
    "git diff",
    "git diff HEAD~1",
    "git branch -a",
    "git remote -v",
    "git show HEAD",
    "git rev-parse HEAD",
    "echo hello",            # readonly stdout
    "true",
    "ls -la && pwd",         # AND-chain of orientation
    "ls; pwd",               # semicolon-chain of orientation
    "ls | head",             # pipe of orientation
]

# Cases the function MUST classify as mutating (return False)
MUTATING = [
    "rm /tmp/foo",
    "mv a b",
    "cp a b",
    "mkdir foo",
    "touch foo",
    "git commit -m 'x'",
    "git push",
    "git checkout main",
    "git reset --hard",
    "git rebase",
    "git stash",
    "mix deps.get",
    "mix phx.server",
    "mix compile",
    "cargo build",
    "cargo run",
    "npm install",
    "pip install foo",
    "make",
    "./build.sh",
    "python3 script.py",
    "ls; rm bar",            # mixed chain — anything mutating poisons the chain
    "ls && cargo build",
    "ls | tee log.txt",      # tee writes
    "cat foo > out",          # redirect writes
    "cat foo >> out",
    "echo 'x' > /tmp/foo",
    "",                       # empty — safer to gate
]


def test_orientation_commands_are_exempt():
    m = _load()
    fn = m.bash_command_is_orientation
    for cmd in ORIENTATION_OK:
        assert fn(cmd) is True, f"should be orientation: {cmd!r}"


def test_mutating_commands_are_not_exempt():
    m = _load()
    fn = m.bash_command_is_orientation
    for cmd in MUTATING:
        assert fn(cmd) is False, f"should NOT be orientation: {cmd!r}"


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
