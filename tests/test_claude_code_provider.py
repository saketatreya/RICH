import json
from decimal import Decimal
from pathlib import Path

import pytest

from liveutil import require_claude_login

from richbuild.claude_code_provider import (
    CLAUDE_CODE_PROVIDER,
    ClaudeCodeCliProvider,
    ClaudeCodeResult,
)
from richbuild.providers import GenerationRole, ModelRequest, ProviderFailure


BUNDLE = {
    "summary": "Implemented the approved behavior",
    "files": [
        {
            "operation": "create",
            "path": "apps/web/generated.ts",
            "content": "export const ok = true;\n",
        }
    ],
}


def _request(**overrides):
    values = {
        "run_id": "run-1",
        "task_id": "task-1",
        "correlation_id": "corr-1",
        "role": GenerationRole.IMPLEMENTER,
        "provider": CLAUDE_CODE_PROVIDER,
        "model": "claude-sonnet-5",
        "system_prompt": "You are the bounded RICH implementation worker.",
        "user_prompt": "Produce the smallest coherent source change.",
        "response_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        "max_input_tokens": 32_000,
        "max_output_tokens": 8_000,
        "max_cost_usd": Decimal("1"),
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return ModelRequest(**values)


def _envelope(**overrides):
    document = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "session_id": "5e0cd712-1820-4bb5-a3b5-112fd7a16249",
        "total_cost_usd": 0.0026390000000000003,
        "permission_denials": [],
        "api_error_status": None,
        "num_turns": 1,
        "result": json.dumps(BUNDLE),
        "modelUsage": {
            "claude-sonnet-5": {
                "inputTokens": 246,
                "outputTokens": 86,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "costUSD": 0.002028,
                "canonicalModel": "claude-sonnet-5",
            },
            "claude-haiku-4-5-20251001": {
                "inputTokens": 536,
                "outputTokens": 15,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "costUSD": 0.000611,
                "canonicalModel": "claude-haiku-4-5",
            },
        },
    }
    document.update(overrides)
    return document


class RecordingRunner:
    """Stand in for the subprocess so argv and isolation stay inspectable."""

    def __init__(self, envelope=None, *, returncode=0, stdout=None, timed_out=False):
        self.calls = []
        self._envelope = envelope
        self._returncode = returncode
        self._stdout = stdout
        self._timed_out = timed_out

    def __call__(self, argv, *, prompt, cwd, home):
        # Snapshot during the call: the sandbox is removed the moment generate
        # returns, which is itself part of the contract.
        self.calls.append(
            {
                "argv": list(argv),
                "prompt": prompt,
                "cwd": Path(cwd),
                "home": Path(home),
                "cwd_entries": sorted(entry.name for entry in Path(cwd).iterdir()),
                "home_entries": sorted(entry.name for entry in Path(home).iterdir()),
                "claude_entries": sorted(
                    entry.name for entry in (Path(home) / ".claude").iterdir()
                ),
                "credential_is_symlink": (
                    Path(home) / ".claude" / ".credentials.json"
                ).is_symlink(),
                "credential_target": (
                    Path(home) / ".claude" / ".credentials.json"
                ).resolve(),
            }
        )
        if self._stdout is not None:
            body = self._stdout
        else:
            body = (json.dumps(self._envelope) + "\n").encode("utf-8")
        return ClaudeCodeResult(
            returncode=self._returncode, stdout=body, timed_out=self._timed_out
        )


@pytest.fixture
def credential(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('{"token": "not-a-real-token"}')
    return path


def _provider(credential, runner, **kwargs):
    return ClaudeCodeCliProvider(
        credential_path=credential, runner=runner, **kwargs
    )


# --------------------------------------------------------------------------
# The firewall is the argv and the environment, so both are asserted exactly
# --------------------------------------------------------------------------


def test_the_worker_is_launched_with_no_tool_affordance_at_all(credential):
    runner = RecordingRunner(_envelope())

    _provider(credential, runner).generate(_request())

    argv = runner.calls[0]["argv"]
    assert argv[1:4] == ["--print", "--output-format", "json"]
    # An empty --tools list, not a denylist: there is nothing to deny.
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    # --system-prompt replaces the harness's own agent prompt; --append would
    # leave a coding agent's defaults underneath RICH's instructions.
    assert "--append-system-prompt" not in argv
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert system_prompt.startswith(_request().system_prompt)
    # The schema travels in the prompt because this route cannot enforce one.
    # Omitting it left the caller's "match the supplied schema" pointing at a
    # schema the worker was never shown.
    assert '"required":["summary"]' in system_prompt
    assert "no markdown fences" in system_prompt
    # The task prompt goes over stdin, well clear of the kernel's argv limit.
    assert runner.calls[0]["prompt"] == _request().user_prompt
    assert _request().user_prompt not in argv


def test_the_response_schema_is_stated_because_it_cannot_be_enforced(credential):
    runner = RecordingRunner(_envelope())
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "files": {"type": "array"}},
        "required": ["summary", "files"],
        "additionalProperties": False,
    }

    _provider(credential, runner).generate(_request(response_schema=schema))

    argv = runner.calls[0]["argv"]
    system_prompt = argv[argv.index("--system-prompt") + 1]
    # Object keys are sorted for a stable prompt; array order is the schema's
    # own and is left alone.
    assert '"required":["summary","files"]' in system_prompt
    # Stating it is only a request; the bundle parser is what enforces it. The
    # first end-to-end run failed exactly here -- right file paths, wrong
    # envelope -- because the schema never reached the worker at all.
    assert "exactly one JSON object" in system_prompt


def test_effort_is_explicit_and_validated():
    with pytest.raises(ValueError, match="effort"):
        ClaudeCodeCliProvider(effort="adaptive")


def test_effort_reaches_the_command(credential):
    runner = RecordingRunner(_envelope())

    _provider(credential, runner, effort="low").generate(_request())

    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--effort") + 1] == "low"


def test_the_worker_runs_in_an_empty_directory_under_a_throwaway_home(credential):
    runner = RecordingRunner(_envelope())

    _provider(credential, runner).generate(_request())

    call = runner.calls[0]
    assert call["cwd_entries"] == []
    # HOME is what CLAUDE.md discovery, user memory, settings and project
    # history all key off. Exactly one entry, and it is the credential.
    assert call["home_entries"] == [".claude"]
    assert call["claude_entries"] == [".credentials.json"]
    # Linked, not copied: the secret is never duplicated at rest, and a token
    # refresh still lands in the operator's real file.
    assert call["credential_is_symlink"]
    assert call["credential_target"] == credential.resolve()
    # And the whole sandbox is gone once the call returns.
    assert not call["home"].exists()


def test_a_missing_login_fails_closed_before_anything_is_spawned(tmp_path):
    runner = RecordingRunner(_envelope())
    provider = _provider(tmp_path / "absent.json", runner)

    with pytest.raises(ProviderFailure, match="credential is missing") as caught:
        provider.generate(_request())

    assert caught.value.retryable is False
    assert caught.value.request_was_sent is False
    assert runner.calls == []


def test_a_request_for_another_provider_is_refused(credential):
    runner = RecordingRunner(_envelope())

    with pytest.raises(ProviderFailure, match="wrong provider") as caught:
        _provider(credential, runner).generate(_request(provider="anthropic"))

    assert caught.value.request_was_sent is False
    assert runner.calls == []


def test_generation_without_a_schema_is_refused(credential):
    runner = RecordingRunner(_envelope())

    with pytest.raises(ProviderFailure, match="requires a response schema"):
        _provider(credential, runner).generate(_request(response_schema={}))

    assert runner.calls == []


# --------------------------------------------------------------------------
# Accounting: the harness spends on its own behalf, and that spend is real
# --------------------------------------------------------------------------


def test_usage_counts_every_model_the_harness_used_not_only_the_pinned_one(
    credential,
):
    runner = RecordingRunner(_envelope())

    usage = _provider(credential, runner).generate(_request()).usage

    # 246 + 536 input, 86 + 15 output. Counting only the pinned model would
    # under-report the budget by whatever the harness did on the side.
    assert usage.input_tokens == 782
    assert usage.output_tokens == 101
    assert usage.model_attempts == 1


def test_cost_is_the_exact_reported_literal_and_never_a_float(credential):
    runner = RecordingRunner(_envelope())

    usage = _provider(credential, runner).generate(_request()).usage

    assert isinstance(usage.cost_usd, Decimal)
    # Parsed straight from the JSON literal, so no float is ever constructed.
    assert usage.cost_usd == Decimal("0.0026390000000000003")


def test_a_malformed_usage_report_settles_the_conservative_reservation(credential):
    for broken in (None, {}, {"claude-sonnet-5": "not-a-mapping"}):
        runner = RecordingRunner(_envelope(modelUsage=broken))
        provider = _provider(credential, runner)
        try:
            usage = provider.generate(_request()).usage
        except ProviderFailure as failure:
            # An unusable report also fails the pinned-model check; either way
            # the reservation is what gets charged, never a guess.
            assert failure.usage.cost_usd == Decimal("1")
            continue
        assert usage.cost_usd == Decimal("1")
        assert usage.input_tokens == 32_000


# --------------------------------------------------------------------------
# Fail-closed conditions
# --------------------------------------------------------------------------


def test_a_tool_denial_from_a_tool_free_worker_fails_closed(credential):
    runner = RecordingRunner(
        _envelope(permission_denials=[{"tool_name": "Read"}])
    )

    with pytest.raises(ProviderFailure, match="tool permission denial") as caught:
        _provider(credential, runner).generate(_request())

    # The worker was launched with no tools. An attempted use means the
    # firewall assumption no longer holds, whatever the output happens to say.
    assert caught.value.retryable is False


def test_a_response_from_the_wrong_model_is_refused(credential):
    runner = RecordingRunner(
        _envelope(
            modelUsage={
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "canonicalModel": "claude-haiku-4-5",
                }
            }
        )
    )

    with pytest.raises(ProviderFailure, match="did not use the pinned model") as caught:
        _provider(credential, runner).generate(_request())

    assert caught.value.retryable is False


def test_an_auxiliary_model_cannot_out_generate_the_pinned_one(credential):
    runner = RecordingRunner(
        _envelope(
            modelUsage={
                "claude-sonnet-5": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "canonicalModel": "claude-sonnet-5",
                },
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 10,
                    "outputTokens": 900,
                    "canonicalModel": "claude-haiku-4-5",
                },
            }
        )
    )

    with pytest.raises(ProviderFailure, match="out-generated the pinned model"):
        _provider(credential, runner).generate(_request())


@pytest.mark.parametrize(
    ("overrides", "retryable"),
    [
        ({"is_error": True, "api_error_status": 429}, True),
        ({"is_error": True, "api_error_status": 503}, True),
        ({"is_error": True, "api_error_status": 400}, False),
        ({"subtype": "error_during_execution"}, False),
    ],
)
def test_a_failed_session_is_classified_from_its_status(
    overrides, retryable, credential
):
    runner = RecordingRunner(_envelope(**overrides))

    with pytest.raises(ProviderFailure, match="failed session") as caught:
        _provider(credential, runner).generate(_request())

    assert caught.value.retryable is retryable
    assert caught.value.usage is not None


def test_an_incomplete_turn_is_not_retryable(credential):
    runner = RecordingRunner(_envelope(stop_reason="max_tokens"))

    with pytest.raises(ProviderFailure, match="incomplete \\(max_tokens\\)") as caught:
        _provider(credential, runner).generate(_request())

    # Replaying the same request against the same reservation reproduces it.
    assert caught.value.retryable is False


def test_a_timeout_is_retryable_and_counts_as_sent(credential):
    runner = RecordingRunner(_envelope(), timed_out=True)

    with pytest.raises(ProviderFailure, match="timed out") as caught:
        _provider(credential, runner).generate(_request())

    assert caught.value.retryable is True
    assert caught.value.request_was_sent is True


def test_an_unparsable_or_absent_envelope_is_reported_without_its_bytes(credential):
    secret = b"stderr-secret-that-must-not-escape"
    for stdout, pattern in (
        (b"", "no result envelope"),
        (secret, "unparsable"),
        (b'{"type": "something-else"}\n', "unrecognized"),
    ):
        runner = RecordingRunner(stdout=stdout, returncode=1)
        with pytest.raises(ProviderFailure, match=pattern) as caught:
            _provider(credential, runner).generate(_request())
        assert secret.decode() not in str(caught.value)


# --------------------------------------------------------------------------
# Output recovery: this route can only ask for JSON, not demand a schema
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        json.dumps(BUNDLE),
        "```json\n" + json.dumps(BUNDLE) + "\n```",
        "```\n" + json.dumps(BUNDLE) + "\n```",
        "Here is the bundle:\n" + json.dumps(BUNDLE),
        "   " + json.dumps(BUNDLE) + "   ",
    ],
)
def test_a_narrated_or_fenced_document_is_still_recovered(result, credential):
    runner = RecordingRunner(_envelope(result=result))

    response = _provider(credential, runner).generate(_request())

    # Tolerant here is safe: this decides whether a generation is accepted,
    # never whether it is verified. The bundle parser repeats every check and
    # the sandboxed gates are what publish success.
    assert response.parsed == BUNDLE


@pytest.mark.parametrize("result", ["", "   ", "no json at all", None, 7])
def test_output_with_no_document_in_it_is_retryable(result, credential):
    runner = RecordingRunner(_envelope(result=result))

    with pytest.raises(ProviderFailure) as caught:
        _provider(credential, runner).generate(_request())

    assert caught.value.retryable is True


def test_an_untrusted_session_id_is_not_propagated(credential):
    clean = RecordingRunner(_envelope())
    dirty = RecordingRunner(_envelope(session_id="id with secret material"))

    assert _provider(credential, clean).generate(
        _request()
    ).provider_request_id == "5e0cd712-1820-4bb5-a3b5-112fd7a16249"
    assert _provider(credential, dirty).generate(_request()).provider_request_id is None


def test_the_response_names_the_route_that_produced_it(credential):
    runner = RecordingRunner(_envelope())

    response = _provider(credential, runner).generate(_request())

    # A run record has to say which route answered: the trust properties of a
    # bounded HTTP request and of an agent harness are not the same.
    assert response.provider == "anthropic-claude-code"
    assert response.model == "claude-sonnet-5"


def test_repr_never_leaks_the_credential_path(credential):
    provider = _provider(credential, RecordingRunner(_envelope()))

    assert str(credential) not in repr(provider)


# --------------------------------------------------------------------------
# Live: the real CLI, one small call
# --------------------------------------------------------------------------


@pytest.mark.live
def test_the_real_cli_answers_with_a_structured_document(tmp_path):
    require_claude_login()

    provider = ClaudeCodeCliProvider()
    # The schema has to describe the same document the prompt asks for. This
    # test used to ask in prose for a "files" array while passing the default
    # summary-only schema with additionalProperties false -- and once the CLI
    # route began sending the schema, the model rightly obeyed the schema and
    # dropped the array. Which is the behavior we want: the schema is the
    # contract, and prose that disagrees with it is the thing that is wrong.
    request = _request(
        system_prompt=(
            "You are a bounded generator. Return exactly one JSON object "
            "matching the supplied schema. No prose, no markdown fences."
        ),
        user_prompt=(
            "Create apps/web/generated.ts exporting a const named ok set to "
            "true."
        ),
        response_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "files"],
            "properties": {
                "summary": {"type": "string"},
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["operation", "path", "content"],
                        "properties": {
                            "operation": {"type": "string", "enum": ["create"]},
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
            },
        },
        timeout_seconds=180,
    )

    response = provider.generate(request)

    assert isinstance(response.parsed, dict)
    assert response.parsed["files"][0]["path"] == "apps/web/generated.ts"
    assert response.provider == CLAUDE_CODE_PROVIDER
    assert response.model == "claude-sonnet-5"
    # A subscription still reports what the work cost, so the ledger stays
    # complete even when no invoice arrives per call.
    assert response.usage.cost_usd > 0
    assert response.usage.output_tokens > 0
