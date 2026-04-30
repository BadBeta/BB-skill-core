#!/usr/bin/env python3
"""
Prompt-named artifact contract enforcement.

Failure mode this prevents: the user's prompt names an external
artifact (URL / repo / spec file) as the source of truth, and the
agent writes implementation conforming to its memory of "what such
artifacts usually look like" instead of fetching the actual content.
The internal tests pass because they assert the agent's guess, not
the spec; the spec divergence only surfaces when the named acceptance
suite is finally run, by which point the wrong shape has propagated
through every controller and test.

This hook dispatches on `hook_event_name`:

  UserPromptSubmit — scan the prompt for tokens that name external
    artifacts in a contract-shaped context. Emit a system reminder
    listing them and the rule.

  PreToolUse — on Edit/Write/NotebookEdit into production-code paths
    (lib/, src/, apps/*/lib/, crates/*/src/), require that every
    contractual artifact named anywhere in the user-message stream
    has been retrieved (WebFetch URL / Bash git clone / Read file).
    Block the write until that holds.

There is no prompt-marker opt-out. The whole point of the gate is
that prompt language alone does not reliably cause artifact
retrieval — adding an opt-out marker would re-introduce the same
failure mode. False-positive reduction relies on the contract-shaped-
context heuristic: bare URLs without contract tokens nearby are
silent.

Per-artifact bypass: if the contract heuristic catches a URL that is
genuinely illustrative (caught in error), the only sanctioned escape
is for the agent to call AskUserQuestion with the canonical text
"This is likely a formal specification. Can I choose to not read
it?" — naming the specific artifact in the question — and the user
answering affirmatively. The hook scans the transcript for that
exchange and treats matching artifacts as approved-to-skip for the
session. This puts a human in the loop for every false-positive
override, by design.

Fails open: any exception → exit 0.
"""

import json
import os
import re
import sys
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}

# ── Detection ────────────────────────────────────────────────────────
#
# A URL is "contractual" only when it appears within a small window of
# words that suggest it is being held up as authoritative — "conform",
# "spec", "schema", "test suite", etc. Bare URLs without these tokens
# nearby are treated as illustrative and pass silently. This is the
# load-bearing decision: the failure mode the hook prevents is real but
# false positives would train the agent to dismiss the gate every turn.
CONTRACT_TOKEN_RE = re.compile(
    r"(?i)\b("
    r"conform(?:s|ing|ity)?"
    r"|specification|specs?"
    r"|exactly\s+(?:to|the|matches?|conforms?|follows?)"
    r"|follow(?:s|ing)?\s+(?:exactly|the\s+spec)"
    r"|must\s+(?:follow|conform|match|implement|comply)"
    r"|test\s+suite|official\s+(?:suite|spec|test|api)|the\s+\w+\s+suite"
    r"|acceptance\s+(?:test|suite|criter\w+|gate)"
    r"|defined\s+in|according\s+to|per\s+the"
    r"|API\s+(?:at|spec|documentation|doc|reference|definition)"
    r"|schemas?"
    r"|RFC\s*\d+|ECMA-\d+"
    r"|the\s+official"
    r"|source\s+of\s+truth"
    r")\b"
)

URL_RE = re.compile(r"https?://[^\s\)\]\"'<>`]+")

# Repo references — github.com/org/repo or gh:org/repo.
REPO_RE = re.compile(
    r"\b(?:github\.com/[\w.-]+/[\w.-]+|gh:[\w.-]+/[\w.-]+)"
)

# File-path artifacts. Inherently contractual extensions get matched
# regardless of context — Postman/Bruno/Hurl/OpenAPI/AsyncAPI files
# are always API contracts, never illustrative.
CONTRACTUAL_EXTENSIONS = (
    ".postman_collection.json",
    ".bru",
    ".hurl",
)
# Files that are sometimes-contracts (require contract-token nearby).
SOFT_CONTRACTUAL_EXTENSIONS = (
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.json", "swagger.yaml",
    "asyncapi.yaml", "asyncapi.json",
)

CONTRACTUAL_PATH_RE = re.compile(
    r"`?([\w./-]+(?:" + "|".join(re.escape(e) for e in CONTRACTUAL_EXTENSIONS) +
    r"))`?"
)

CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
CONTEXT_WINDOW = 80  # chars before/after artifact for contract-token scan


def _strip_fenced_code(text):
    """Code blocks are data being shown to the LLM, not contract directives.
    URLs inside them are pasted output, not authoritative references."""
    return CODE_FENCE_RE.sub("\n", text or "")


def _near_contract_token(text, match_start, match_end, window=CONTEXT_WINDOW):
    """True if any contract-shaped token appears within `window` chars
    before match_start or after match_end (overlap counts too)."""
    lo = max(0, match_start - window)
    hi = min(len(text), match_end + window)
    return CONTRACT_TOKEN_RE.search(text, lo, hi) is not None


def extract_contractual_artifacts(text):
    """Return a deduped list of {kind, value} dicts for artifacts named
    in `text` in a contract-shaped context.

    kind ∈ {"url", "repo", "path"}. URLs and repos require a contract
    token nearby; path artifacts use the extension as an inherent
    signal (Postman / Bruno / Hurl files are always contracts).
    """
    if not text or not isinstance(text, str):
        return []
    cleaned = _strip_fenced_code(text)

    seen = set()
    out = []

    def _add(kind, value):
        # Strip trailing punctuation often dragged in by regex
        value = value.rstrip(".,;:!?)")
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        out.append({"kind": kind, "value": value})

    for m in URL_RE.finditer(cleaned):
        if _near_contract_token(cleaned, m.start(), m.end()):
            _add("url", m.group(0))

    for m in REPO_RE.finditer(cleaned):
        if _near_contract_token(cleaned, m.start(), m.end()):
            _add("repo", m.group(0))

    for m in CONTRACTUAL_PATH_RE.finditer(cleaned):
        # Inherently contractual extensions — flag regardless of context
        _add("path", m.group(1))

    return out


# ── State tracking ──────────────────────────────────────────────────

def _user_text(rec):
    """Plain text from a user-message record."""
    if rec.get("type") != "user":
        return ""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else rec.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _assistant_tool_uses(rec):
    """Yield (tool_name, tool_input) for every tool_use in an assistant rec."""
    if rec.get("type") != "assistant":
        return
    content = (rec.get("message") or {}).get("content", [])
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name", ""), (block.get("input") or {})


def artifact_was_retrieved(artifact, records):
    """Walk all assistant tool_use records; return True if `artifact`
    has been retrieved by an appropriate tool somewhere in the session."""
    kind = artifact["kind"]
    value = artifact["value"]

    for rec in records:
        for tool_name, tool_input in _assistant_tool_uses(rec):
            if kind == "url" and tool_name == "WebFetch":
                fetched = tool_input.get("url", "")
                if isinstance(fetched, str) and (
                    fetched.startswith(value) or value.startswith(fetched)
                ):
                    return True
            elif kind == "repo":
                # Normalise to the github.com/org/repo path component
                if value.startswith("gh:"):
                    repo_path = "github.com/" + value[3:]
                else:
                    repo_path = value
                if tool_name == "WebFetch":
                    fetched = tool_input.get("url", "")
                    if isinstance(fetched, str) and repo_path in fetched:
                        return True
                if tool_name == "Bash":
                    cmd = tool_input.get("command", "")
                    if isinstance(cmd, str) and "git clone" in cmd and (
                        repo_path in cmd or value in cmd
                    ):
                        return True
            elif kind == "path":
                if tool_name == "Read":
                    fp = tool_input.get("file_path", "")
                    if isinstance(fp, str) and (
                        fp.endswith(value) or fp.endswith("/" + value)
                    ):
                        return True
    return False


# ── User-acceptance bypass ──────────────────────────────────────────
#
# The canonical phrase the agent must use in AskUserQuestion to obtain
# permission to skip a specific artifact. Match is substring + case-
# insensitive, so light variations in punctuation / surrounding text
# don't break the bypass.
USER_ACCEPT_PHRASE_RE = re.compile(
    r"(?i)likely\s+a\s+formal\s+specification.*can\s+i\s+choose\s+to\s+not\s+read"
)

# Tokens in a tool_result that count as user-affirmative.
AFFIRMATIVE_RE = re.compile(
    r"(?i)\b(?:yes|skip|illustrative|approved?|proceed\s+without|don't\s+read|not\s+contractual)\b"
)


def _ask_user_question_input_text(tool_input):
    """Stringify the AskUserQuestion input so we can substring-match
    artifact values + the canonical phrase against it."""
    if not isinstance(tool_input, dict):
        return ""
    try:
        return json.dumps(tool_input)
    except (TypeError, ValueError):
        return ""


def _tool_result_text(block):
    """Extract a stringified view of an assistant→user tool_result block."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for sub in content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                parts.append(sub.get("text", ""))
            elif isinstance(sub, str):
                parts.append(sub)
        return "\n".join(parts)
    if isinstance(content, dict):
        try:
            return json.dumps(content)
        except (TypeError, ValueError):
            return ""
    return ""


def user_approved_skip(artifact, records):
    """True if the transcript contains an AskUserQuestion call that
    (a) used the canonical confirmation phrase, (b) mentioned this
    specific artifact's value, and (c) received an affirmative
    response from the user."""
    pending = {}  # tool_use_id -> True (we only care about matching id)
    for rec in records:
        rec_type = rec.get("type")
        if rec_type == "assistant":
            content = (rec.get("message") or {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "AskUserQuestion"
                ):
                    continue
                inp_text = _ask_user_question_input_text(block.get("input"))
                if not USER_ACCEPT_PHRASE_RE.search(inp_text):
                    continue
                if artifact["value"] not in inp_text:
                    continue
                use_id = block.get("id")
                if use_id:
                    pending[use_id] = True
        elif rec_type == "user":
            content = (rec.get("message") or {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                ):
                    continue
                use_id = block.get("tool_use_id")
                if use_id not in pending:
                    continue
                if AFFIRMATIVE_RE.search(_tool_result_text(block)):
                    return True
    return False


# ── Path scoping ─────────────────────────────────────────────────────
#
# Mirrors `is_milestone_gated_path` in bb-milestone-skill-report.py —
# the two hooks should agree on "is this a production-code path."
# Duplicated rather than imported because each hook script is invoked
# as its own subprocess; keep the two definitions in sync when either
# changes.
_NON_IMPL_DIR_PREFIXES = (
    "test/", "tests/", "spec/",
    "docs/", "documentation/",
    "config/",
    "target/", "_build/", "deps/", "node_modules/",
    "priv/static/", "priv/repo/",
    ".github/", ".vscode/", ".claude/", ".git/",
    ".elixir_ls/",
    "examples/",
)
_NON_IMPL_BASENAMES = {
    "mix.exs", "mix.lock",
    "Cargo.toml", "Cargo.lock",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "pyproject.toml", "Pipfile", "Pipfile.lock", "poetry.lock",
    "go.mod", "go.sum",
    ".gitignore", ".gitattributes",
    ".formatter.exs",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
}
_NON_IMPL_EXTENSIONS = {
    ".md", ".rst", ".txt",
    ".toml", ".yaml", ".yml", ".json",
    ".lock",
    ".cfg", ".ini", ".env",
}

_PROJECT_MARKERS = (
    "PLAN.md", "Cargo.toml", "mix.exs", "pyproject.toml",
    "package.json", "go.mod", ".git",
)


def _project_root(file_path):
    p = Path(file_path).resolve()
    if p.is_file():
        p = p.parent
    elif not p.exists():
        p = p.parent
    cur = p
    seen = set()
    while cur not in seen:
        seen.add(cur)
        for marker in _PROJECT_MARKERS:
            if (cur / marker).exists():
                return cur
        if cur == cur.parent:
            return None
        cur = cur.parent
    return None


def _is_production_code_path(file_path, project_root_path):
    if not file_path or not project_root_path:
        return False
    try:
        rel = str(Path(file_path).resolve().relative_to(
            Path(project_root_path).resolve()
        ))
    except (ValueError, OSError):
        return False
    base = Path(rel).name
    if base in _NON_IMPL_BASENAMES:
        return False
    if Path(rel).suffix.lower() in _NON_IMPL_EXTENSIONS:
        return False
    if rel.startswith("."):
        return False
    rel_n = rel.replace("\\", "/")
    for prefix in _NON_IMPL_DIR_PREFIXES:
        if rel_n.startswith(prefix) or "/" + prefix in "/" + rel_n:
            return False
    if "/" not in rel_n:
        return False
    return True


# ── Transcript ───────────────────────────────────────────────────────

def _read_transcript(path):
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return out


# ── Event handlers ───────────────────────────────────────────────────

def _format_artifact_list(artifacts):
    lines = []
    for a in artifacts:
        if a["kind"] == "url":
            lines.append(f"  - URL: {a['value']}")
        elif a["kind"] == "repo":
            lines.append(f"  - repo: {a['value']}")
        elif a["kind"] == "path":
            lines.append(f"  - file: {a['value']}")
    return "\n".join(lines)


def _user_prompt_submit_reminder(prompt_text):
    artifacts = extract_contractual_artifacts(prompt_text)
    if not artifacts:
        return None
    listing = _format_artifact_list(artifacts)
    body = (
        f"ARTIFACT-CONTRACT: this prompt names external sources that "
        f"are load-bearing for the task:\n\n{listing}\n\n"
        f"You MUST retrieve each one (WebFetch / git clone / Read) "
        f"BEFORE writing any implementation file under lib/, src/, "
        f"apps/*/lib/, or crates/*/src/. The exact request/response "
        f"shapes, error envelopes, status codes, field names, and "
        f"sort orders from these artifacts ARE the contract — your "
        f"memory of how artifacts of this kind usually look is not a "
        f"substitute, no matter how confident the memory feels.\n\n"
        f"If an artifact cannot be fetched, stop and tell the user "
        f"before writing code. The PreToolUse companion to this "
        f"reminder will block production-code edits until every "
        f"named artifact has been retrieved this session."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": body,
        }
    }


def _pretooluse_block(data):
    tool_name = data.get("tool_name", "")
    if tool_name not in EDIT_TOOLS:
        return None
    tool_input = data.get("tool_input") or {}
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or ""
    )
    if not file_path:
        return None
    proj = _project_root(file_path)
    if proj is None:
        return None
    if not _is_production_code_path(file_path, str(proj)):
        return None

    records = _read_transcript(data.get("transcript_path"))
    if not records:
        return None
    # Scan ALL user-typed text in the session for contractual artifacts
    seen = set()
    artifacts = []
    for rec in records:
        text = _user_text(rec)
        if not text:
            continue
        for a in extract_contractual_artifacts(text):
            key = (a["kind"], a["value"])
            if key not in seen:
                seen.add(key)
                artifacts.append(a)
    if not artifacts:
        return None
    unfetched = [
        a for a in artifacts
        if not artifact_was_retrieved(a, records)
        and not user_approved_skip(a, records)
    ]
    if not unfetched:
        return None
    listing = _format_artifact_list(unfetched)
    reason = (
        "[BLOCK] PreToolUse: prompt-named artifact(s) not yet "
        "retrieved this session.\n\n"
        f"Unfetched contractual artifact(s):\n{listing}\n\n"
        "The prompt named these artifacts as authoritative. Writing "
        "implementation code in production paths (lib/, src/, "
        "apps/*/lib/, crates/*/src/) before retrieving them is the "
        "failure mode this gate exists to prevent — internal tests go "
        "green against the agent's guess of the contract while the "
        "actual contract diverges silently.\n\n"
        "Fix: retrieve each unfetched artifact now (WebFetch for URLs, "
        "Bash `git clone <url>` for repos, Read for file paths), "
        "extract the concrete shapes (request/response examples, error "
        "envelopes, status codes, field names) into your test fixtures, "
        "then re-attempt the write.\n\n"
        "If an artifact is genuinely illustrative (caught by the "
        "contract heuristic in error) and you want to proceed without "
        "fetching it, the only sanctioned escape is to call "
        "AskUserQuestion with the exact prompt:\n"
        "    \"This is likely a formal specification. Can I choose "
        "to not read it?\"\n"
        "naming the specific artifact in the question, and proceeding "
        "only if the user explicitly answers Yes. There is no "
        "prompt-marker opt-out — the human-in-the-loop gate is "
        "deliberate. If the artifact is genuinely unavailable, tell "
        "the user before writing code; they can then redirect or "
        "supply the content directly."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }


def handle(data):
    """Single dispatch point. Returns a hook output dict or None."""
    event = data.get("hook_event_name", "")
    if event == "UserPromptSubmit":
        prompt = data.get("prompt") or ""
        if not prompt:
            # Fallback: read latest user-typed text from the transcript
            recs = _read_transcript(data.get("transcript_path"))
            for rec in reversed(recs):
                t = _user_text(rec)
                if t:
                    prompt = t
                    break
        return _user_prompt_submit_reminder(prompt)
    if event == "PreToolUse":
        return _pretooluse_block(data)
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        result = handle(data)
    except Exception as e:
        sys.stderr.write(f"prompt-artifact-contract hook error: {e}\n")
        return 0
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"prompt-artifact-contract hook error: {e}\n")
        sys.exit(0)
