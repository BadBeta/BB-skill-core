"""
Tests for bb-prompt-artifact-contract.py — detect contractual artifacts
in user prompts and block production-code edits until they've been
retrieved (WebFetch / git clone / Read).

The failure mode this prevents: agent writes implementation conforming
to its memory of "what spec X usually looks like" instead of fetching
the actual artifact named in the prompt and reading what it requires.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "bb-prompt-artifact-contract.py"


def _load(home_dir):
    os.environ["HOME"] = str(home_dir)
    sys.modules.pop("bb_prompt_artifact_contract", None)
    spec = importlib.util.spec_from_file_location(
        "bb_prompt_artifact_contract", HOOK
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_proj(parent):
    proj = parent / "proj"
    proj.mkdir()
    # Project marker so _project_root() resolves correctly
    (proj / "mix.exs").write_text("# fake mix project")
    (proj / "lib").mkdir()
    (proj / "test").mkdir()
    (proj / "lib" / "x.ex").write_text("x")
    return proj


def _make_transcript(parent, records):
    """Build a JSONL transcript from a list of `{"type": ..., ...}` dicts."""
    p = parent / "transcript.jsonl"
    with open(p, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return p


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant_tool(name, tool_input):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": name, "input": tool_input}
            ]
        },
    }


# ───────────────────── extract_contractual_artifacts ─────────────────

def test_url_near_conform_is_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "Build a backend that conforms exactly to the spec at https://realworld-docs.netlify.app please."
        )
        assert any(a["kind"] == "url" and "realworld-docs.netlify.app" in a["value"] for a in out)


def test_url_near_spec_is_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "See the API spec: https://docs.example.com/v1 for the field names."
        )
        assert any(a["kind"] == "url" for a in out)


def test_url_near_the_official_suite_is_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "Use the official Postman suite from https://github.com/realworld/postman-collection as acceptance."
        )
        assert any(a["kind"] in ("url", "repo") for a in out)


def test_url_with_must_conform_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "The output must conform to https://example.com/schema.json"
        )
        assert any(a["kind"] == "url" for a in out)


def test_bare_url_without_context_not_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "I saw a project at https://github.com/example/foo the other day, neat."
        )
        # No contract-shaped tokens nearby — should be silent.
        assert out == []


def test_similar_to_url_not_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "We want something similar to https://example.com but our own design."
        )
        assert out == []


def test_url_in_fenced_code_block_not_flagged():
    """A URL inside ``` fences is data being shown, not a contract directive."""
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        text = (
            "Here's an example log line:\n"
            "```\n"
            "GET https://example.com/api/v1 — must conform to the format above\n"
            "```\n"
            "Make a parser for it."
        )
        # Even though "must conform" is present, the URL is fenced
        out = m.extract_contractual_artifacts(text)
        urls = [a for a in out if a["kind"] == "url"]
        assert urls == []


def test_repo_reference_near_official_is_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "Implement against the official test suite at github.com/realworld/specs"
        )
        assert any(a["kind"] in ("repo", "url") for a in out)


def test_postman_collection_filename_always_flagged():
    """File extensions strongly associated with API contracts (Postman,
    Bruno, Hurl, OpenAPI) flag independent of contract-shaped context —
    they're inherently contractual."""
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "I have a `Conduit.postman_collection.json` to test against."
        )
        assert any(a["kind"] == "path" and "postman_collection.json" in a["value"] for a in out)


def test_hurl_filename_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "Use `auth.hurl` and `articles.hurl` as the acceptance gate."
        )
        # At least one .hurl path flagged
        assert any(a["kind"] == "path" and a["value"].endswith(".hurl") for a in out)


def test_duplicate_url_flagged_once():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        out = m.extract_contractual_artifacts(
            "Conform to https://x.com/spec exactly. Also see spec https://x.com/spec for details."
        )
        urls = [a for a in out if a["value"] == "https://x.com/spec"]
        assert len(urls) == 1


# ───────────────────── artifact_was_retrieved ────────────────────────

def test_webfetch_exact_url_satisfies():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        records = [
            _user("conform to https://x.com/spec"),
            _assistant_tool("WebFetch", {"url": "https://x.com/spec"}),
        ]
        artifact = {"kind": "url", "value": "https://x.com/spec"}
        assert m.artifact_was_retrieved(artifact, records) is True


def test_webfetch_sub_url_satisfies():
    """If user named https://x.com/docs and agent fetched
    https://x.com/docs/auth — counts (sub-page of the named root)."""
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        records = [
            _user("spec at https://x.com/docs"),
            _assistant_tool("WebFetch", {"url": "https://x.com/docs/auth"}),
        ]
        assert m.artifact_was_retrieved(
            {"kind": "url", "value": "https://x.com/docs"}, records
        ) is True


def test_webfetch_super_url_satisfies():
    """If user named a sub-page and agent fetched the parent — also counts."""
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        records = [
            _user("conform to https://x.com/docs/auth"),
            _assistant_tool("WebFetch", {"url": "https://x.com/docs"}),
        ]
        assert m.artifact_was_retrieved(
            {"kind": "url", "value": "https://x.com/docs/auth"}, records
        ) is True


def test_webfetch_different_domain_does_not_satisfy():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        records = [
            _user("conform to https://x.com/spec"),
            _assistant_tool("WebFetch", {"url": "https://example.com/other"}),
        ]
        assert m.artifact_was_retrieved(
            {"kind": "url", "value": "https://x.com/spec"}, records
        ) is False


def test_git_clone_satisfies_repo_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        records = [
            _user("the official suite at github.com/realworld/specs"),
            _assistant_tool("Bash", {
                "command": "git clone https://github.com/realworld/specs /tmp/specs"
            }),
        ]
        assert m.artifact_was_retrieved(
            {"kind": "repo", "value": "github.com/realworld/specs"}, records
        ) is True


def test_read_file_satisfies_path_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        m = _load(Path(tmp))
        records = [
            _user("test against `auth.hurl`"),
            _assistant_tool("Read", {"file_path": "/tmp/specs/auth.hurl"}),
        ]
        assert m.artifact_was_retrieved(
            {"kind": "path", "value": "auth.hurl"}, records
        ) is True


# ───────────────────── UserPromptSubmit ──────────────────────────────

def test_user_prompt_submit_with_contractual_url_emits_reminder():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        proj = _make_proj(home)
        transcript = _make_transcript(home, [
            _user("Build a backend that conforms exactly to https://example.com/spec")
        ])
        ev = {
            "session_id": "test",
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": str(transcript),
            "prompt": "Build a backend that conforms exactly to https://example.com/spec",
        }
        m = _load(home)
        result = m.handle(ev)
        assert result is not None
        text = json.dumps(result)
        assert "ARTIFACT-CONTRACT" in text or "artifact" in text.lower()
        assert "example.com/spec" in text


def test_user_prompt_submit_with_no_contractual_artifact_silent():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        transcript = _make_transcript(home, [
            _user("Refactor the login form to use Tailwind utilities.")
        ])
        ev = {
            "session_id": "test",
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": str(transcript),
            "prompt": "Refactor the login form to use Tailwind utilities.",
        }
        m = _load(home)
        assert m.handle(ev) is None


# ───────────────────── PreToolUse end-to-end ─────────────────────────

def _pretooluse_event(file_path, transcript):
    return {
        "session_id": "test",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path), "new_string": "x"},
        "transcript_path": str(transcript),
    }


def test_lib_edit_with_unfetched_contractual_url_denies():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        transcript = _make_transcript(home, [
            _user("conform exactly to https://docs.example.com/v1"),
        ])
        m = _load(home)
        result = m.handle(_pretooluse_event(proj / "lib/x.ex", transcript))
        assert result is not None
        assert result.get("permissionDecision") == "deny"
        assert "docs.example.com/v1" in result.get("permissionDecisionReason", "")


def test_lib_edit_with_fetched_url_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        transcript = _make_transcript(home, [
            _user("conform exactly to https://docs.example.com/v1"),
            _assistant_tool("WebFetch", {"url": "https://docs.example.com/v1/auth"}),
        ])
        m = _load(home)
        assert m.handle(_pretooluse_event(proj / "lib/x.ex", transcript)) is None


def test_docs_edit_passes_through_path_scoping():
    """Path-scoping (from Layer 1 work): docs / config / tests / build
    artefacts are not gated regardless of artifact state."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        (proj / "Technical_report.md").write_text("# notes")
        transcript = _make_transcript(home, [
            _user("conform exactly to https://docs.example.com/v1"),
        ])
        m = _load(home)
        # Editing the doc, not gated even though the URL is unfetched
        assert m.handle(_pretooluse_event(proj / "Technical_report.md", transcript)) is None


def test_lib_edit_with_no_contractual_artifacts_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        transcript = _make_transcript(home, [
            _user("Refactor the login form."),
        ])
        m = _load(home)
        assert m.handle(_pretooluse_event(proj / "lib/x.ex", transcript)) is None


def _ask_user_question_use(use_id, question_text, artifact_value):
    """Build an assistant tool_use record for AskUserQuestion."""
    return {
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use",
                "name": "AskUserQuestion",
                "id": use_id,
                "input": {
                    "questions": [{
                        "question": question_text,
                        "header": f"Skip {artifact_value}?",
                        "options": [
                            {"label": "Yes, skip it", "description": "Illustrative, not contractual"},
                            {"label": "No, fetch it", "description": "It's the spec"},
                        ],
                    }],
                },
            }]
        },
    }


def _tool_result(use_id, content_text):
    return {
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": use_id,
                "content": content_text,
            }]
        },
    }


# ───────────────────── User-acceptance bypass ────────────────────────

def test_skip_approved_via_user_acceptance_allows_edit():
    """Agent asked the user via AskUserQuestion using the canonical
    phrase, naming the artifact; user answered affirmatively. The
    write should now succeed even though the URL was never fetched."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        artifact_url = "https://docs.example.com/v1"
        question = (
            f"This is likely a formal specification. Can I choose to "
            f"not read it? URL: {artifact_url}"
        )
        transcript = _make_transcript(home, [
            _user(f"conform exactly to {artifact_url}"),
            _ask_user_question_use("ask-1", question, artifact_url),
            _tool_result("ask-1", "Yes, skip it"),
        ])
        m = _load(home)
        assert m.handle(_pretooluse_event(proj / "lib/x.ex", transcript)) is None


def test_skip_question_without_canonical_phrase_does_not_unlock():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        artifact_url = "https://docs.example.com/v1"
        # Question without the canonical phrase
        transcript = _make_transcript(home, [
            _user(f"conform exactly to {artifact_url}"),
            _ask_user_question_use(
                "ask-1",
                f"Want me to skip {artifact_url}?",
                artifact_url,
            ),
            _tool_result("ask-1", "Yes"),
        ])
        m = _load(home)
        result = m.handle(_pretooluse_event(proj / "lib/x.ex", transcript))
        assert result is not None and result.get("permissionDecision") == "deny"


def test_skip_question_with_negative_answer_still_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        artifact_url = "https://docs.example.com/v1"
        question = (
            f"This is likely a formal specification. Can I choose to "
            f"not read it? Artifact: {artifact_url}"
        )
        transcript = _make_transcript(home, [
            _user(f"conform exactly to {artifact_url}"),
            _ask_user_question_use("ask-1", question, artifact_url),
            _tool_result("ask-1", "No, fetch it"),
        ])
        m = _load(home)
        result = m.handle(_pretooluse_event(proj / "lib/x.ex", transcript))
        assert result is not None and result.get("permissionDecision") == "deny"


def test_skip_approved_for_one_artifact_does_not_unlock_another():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        url_a = "https://docs.example.com/v1"
        url_b = "https://other.example.com/spec"
        question_a = (
            f"This is likely a formal specification. Can I choose to "
            f"not read it? Artifact: {url_a}"
        )
        transcript = _make_transcript(home, [
            _user(f"conform exactly to {url_a} and the spec at {url_b}"),
            _ask_user_question_use("ask-1", question_a, url_a),
            _tool_result("ask-1", "Yes, skip it"),
            # url_b never asked-about, never fetched
        ])
        m = _load(home)
        result = m.handle(_pretooluse_event(proj / "lib/x.ex", transcript))
        # Should still deny — url_b wasn't approved
        assert result is not None and result.get("permissionDecision") == "deny"
        assert url_b in result.get("permissionDecisionReason", "")


def test_pretooluse_only_fires_on_edit_tools():
    """Read / Glob / Grep / Skill / WebFetch must not be gated."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp); proj = _make_proj(home)
        transcript = _make_transcript(home, [
            _user("conform exactly to https://docs.example.com/v1"),
        ])
        m = _load(home)
        for tool in ("Read", "Glob", "Grep", "WebFetch", "Skill", "TodoWrite"):
            ev = {
                "session_id": "t",
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": {"file_path": str(proj / "lib/x.ex")},
                "transcript_path": str(transcript),
            }
            assert m.handle(ev) is None, f"{tool} should pass through"


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
