"""The rules every model provider obeys, applied to every provider.

`openai_provider.py` is wired to nothing and kept anyway, because a seam with
one implementation is not a seam. That argument only holds if the seam is
actually a contract — and until now it was an assertion. Each adapter had its
own tests, so nothing said what they had to agree about.

These are the rules the gateway, the budget ledger and the coding worker depend
on. An adapter that breaks one of them cannot be swapped in, whatever its own
suite says. They are exercised through each provider's real parsing path with a
faked transport: no network, no credentials, no model.
"""

import json
from decimal import Decimal

import pytest

from richbuild.anthropic_provider import AnthropicMessagesProvider
from richbuild.claude_code_provider import ClaudeCodeCliProvider
from richbuild.openai_provider import OpenAIResponsesProvider
from richbuild.providers import (
    GenerationRole,
    ModelRequest,
    ModelResponse,
    ProviderFailure,
)


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}


def _request(provider: str = "unused", **overrides) -> ModelRequest:
    values = {
        "run_id": "run.conformance",
        "task_id": "task.conformance",
        "correlation_id": "corr.conformance",
        "role": GenerationRole.IMPLEMENTER,
        "provider": provider,
        "model": "test-model",
        "system_prompt": "You are a bounded worker.",
        "user_prompt": "Return one JSON object.",
        "response_schema": SCHEMA,
        "max_input_tokens": 32_000,
        "max_output_tokens": 4_000,
        "max_cost_usd": Decimal("1"),
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return ModelRequest(**values)


class _HttpRecorder:
    """The `post_json` transport both HTTP adapters accept."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    def post_json(self, **call):
        self.calls.append(call)
        return self.reply


def _anthropic(text='{"summary":"ok"}'):
    from richbuild.anthropic_provider import AnthropicHTTPResponse, AnthropicTokenRates

    document = {
        "id": "msg_conformance",
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    recorder = _HttpRecorder(
        AnthropicHTTPResponse(
            status_code=200, body=json.dumps(document).encode(), headers={}
        )
    )
    provider = AnthropicMessagesProvider(
        "test-key",
        transport=recorder,
        rates={
            "test-model": AnthropicTokenRates(
                input=Decimal("3"),
                cache_write_5m=Decimal("3.75"),
                cache_write_1h=Decimal("6"),
                cache_read=Decimal("0.3"),
                output=Decimal("15"),
            )
        },
    )
    return provider, recorder


def _openai(text='{"summary":"ok"}'):
    from richbuild.openai_provider import OpenAIHTTPResponse, OpenAITokenRates

    document = {
        "id": "resp_conformance",
        "model": "test-model",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    recorder = _HttpRecorder(
        OpenAIHTTPResponse(
            status_code=200, body=json.dumps(document).encode(), headers={}
        )
    )
    provider = OpenAIResponsesProvider(
        "test-key",
        transport=recorder,
        rates={
            "test-model": OpenAITokenRates(
                input=Decimal("2"), cached_input=Decimal("0.5"), output=Decimal("8")
            )
        },
    )
    return provider, recorder


def _claude_code(text='{"summary":"ok"}', credential_path=None):
    import tempfile
    from pathlib import Path

    from richbuild.claude_code_provider import ClaudeCodeResult

    if credential_path is None:
        # A file that exists is all the adapter checks before it symlinks it
        # into the throwaway HOME; a login on the test machine is not assumed
        # (a CI runner has none), and nothing here is a credential.
        credential_path = Path(tempfile.mkdtemp(prefix="rich-fake-claude-")) / ".credentials.json"
        credential_path.write_text("{}")
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "model": "test-model",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 100, "output_tokens": 20},
        # The route bills a subscription, so it reports per-model usage and the
        # adapter checks the pinned model is the one that did the work.
        "modelUsage": {
            "test-model": {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "costUSD": 0.01,
                "canonicalModel": "test-model",
            }
        },
    }

    class _Runner:
        def __init__(self):
            self.calls: list[dict] = []

        def __call__(self, argv, *, prompt, cwd, home):
            self.calls.append({"argv": list(argv), "prompt": prompt})
            return ClaudeCodeResult(
                returncode=0,
                stdout=(json.dumps(envelope) + "\n").encode("utf-8"),
                timed_out=False,
            )

    runner = _Runner()
    return (
        ClaudeCodeCliProvider(credential_path=credential_path, runner=runner),
        runner,
    )


PROVIDERS = [
    pytest.param(_anthropic, id="anthropic"),
    pytest.param(_openai, id="openai"),
    pytest.param(_claude_code, id="claude-code"),
]


@pytest.mark.parametrize("build", PROVIDERS)
def test_every_provider_names_itself_stably(build):
    """The gateway routes by name and rejects duplicates, so the name is
    identity rather than decoration."""

    provider, _ = build()

    assert isinstance(provider.name, str) and provider.name.strip()
    assert provider.name == build()[0].name


@pytest.mark.parametrize("build", PROVIDERS)
def test_a_successful_call_returns_a_complete_model_response(build):
    provider, _ = build()

    response = provider.generate(_request(provider.name))

    assert isinstance(response, ModelResponse)
    assert response.provider == provider.name
    assert response.text
    assert response.parsed == {"summary": "ok"}, "structured output is parsed"
    assert response.attempt >= 1


@pytest.mark.parametrize("build", PROVIDERS)
def test_usage_is_always_reported_and_never_a_float(build):
    """The budget is decimal money and must be complete: a provider that
    reports no usage silently makes a run's ledger a guess."""

    provider, _ = build()

    usage = provider.generate(_request(provider.name)).usage

    assert isinstance(usage.cost_usd, Decimal)
    assert usage.cost_usd >= 0
    assert isinstance(usage.input_tokens, int) and usage.input_tokens >= 0
    assert isinstance(usage.output_tokens, int) and usage.output_tokens >= 0
    assert not isinstance(usage.input_tokens, bool)


@pytest.mark.parametrize("build", PROVIDERS)
def test_one_call_is_one_attempt(build):
    """Retries belong to the gateway, which is where they are counted against
    the budget. An adapter that retries internally spends money the ledger
    never sees."""

    provider, recorder = build()

    provider.generate(_request(provider.name))

    assert len(recorder.calls) == 1


@pytest.mark.parametrize("build", PROVIDERS)
def test_failure_is_a_provider_failure_that_says_whether_to_retry(build):
    """The gateway decides whether to try again from this flag alone."""

    # A body the adapter cannot parse: the shape of a bad day at any vendor.
    provider, _ = build(text="this is not json at all")

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request(provider.name))

    assert isinstance(caught.value.retryable, bool)
    assert isinstance(caught.value.request_was_sent, bool)
    assert str(caught.value)


@pytest.mark.parametrize("build", PROVIDERS)
def test_the_prompt_is_never_placed_where_a_process_list_can_read_it(build):
    """Prompts carry approved product intent. Passing one as an argv element
    publishes it to every other user on the machine."""

    provider, recorder = build()
    secret = "SENTINEL-PROMPT-CONTENT"

    provider.generate(_request(provider.name, user_prompt=secret))

    for call in recorder.calls:
        argv = call.get("argv")
        if argv is not None:
            assert all(secret not in str(item) for item in argv)


@pytest.mark.parametrize("build", PROVIDERS)
def test_a_request_no_provider_could_bound_is_refused_before_it_is_built(build):
    """Bounds are the budget's grip on a call, so they are checked at
    construction -- no adapter has to remember to."""

    build()

    for unbounded in (
        {"max_input_tokens": -1},
        {"max_output_tokens": -1},
        {"timeout_seconds": 0},
        {"max_cost_usd": Decimal("0")},
    ):
        with pytest.raises((ValueError, TypeError)):
            _request(**unbounded)
