import json
from decimal import Decimal

import pytest

from richbuild.providers import GenerationRole, ModelRequest, ProviderFailure
from richbuild.openai_provider import (
    OpenAIHTTPResponse,
    OpenAIResponsesProvider,
    OpenAITokenRates,
)


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post_json(self, **call):
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return self.response


def _request(**overrides):
    values = {
        "run_id": "run-1",
        "task_id": "task-1",
        "correlation_id": "corr-1",
        "role": GenerationRole.IMPLEMENTER,
        "provider": "openai",
        "model": "model-version-1",
        "system_prompt": "Return an implementation decision.",
        "user_prompt": "Build the bounded component.",
        "response_schema": {
            "type": "object",
            "properties": {"decision": {"type": "string"}},
            "required": ["decision"],
            "additionalProperties": False,
        },
        "max_input_tokens": 2_000,
        "max_output_tokens": 100,
        "max_cost_usd": Decimal("1"),
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return ModelRequest(**values)


def _response(document, *, status=200, headers=None):
    return OpenAIHTTPResponse(
        status_code=status,
        body=json.dumps(document).encode(),
        headers=headers or {},
    )


def _success(**overrides):
    document = {
        "id": "resp_safe123",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"decision":"ship"}',
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 25},
            "output_tokens": 20,
        },
    }
    document.update(overrides)
    return document


def test_sends_tool_free_strict_schema_request_and_parses_output():
    transport = RecordingTransport(_response(_success()))
    provider = OpenAIResponsesProvider("secret-key", transport=transport)

    result = provider.generate(_request())

    assert result.parsed == {"decision": "ship"}
    assert result.text == '{"decision":"ship"}'
    assert result.provider_request_id == "resp_safe123"
    call = transport.calls[0]
    assert call["url"] == "https://api.openai.com/v1/responses"
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert call["timeout_seconds"] == 30
    assert "tools" not in call["payload"]
    assert call["payload"]["store"] is False
    assert call["payload"]["text"]["format"] == {
        "type": "json_schema",
        "name": "rich_implementer_response",
        "schema": _request().response_schema,
        "strict": True,
    }


def test_computes_explicit_cached_and_uncached_token_cost():
    rates = OpenAITokenRates(
        input=Decimal("2"),
        cached_input=Decimal("0.5"),
        output=Decimal("8"),
    )
    transport = RecordingTransport(_response(_success()))
    provider = OpenAIResponsesProvider(
        "key",
        rates={"model-version-1": rates},
        transport=transport,
    )

    usage = provider.generate(_request()).usage

    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.cost_usd == Decimal("0.0003225")
    assert usage.model_attempts == 1


def test_missing_pricing_settles_conservative_approved_cost():
    transport = RecordingTransport(_response(_success()))
    result = OpenAIResponsesProvider("key", transport=transport).generate(_request())

    assert result.usage.cost_usd == Decimal("1")


def test_missing_or_malformed_usage_settles_all_conservative_maxima():
    transport = RecordingTransport(_response(_success(usage=None)))
    result = OpenAIResponsesProvider("key", transport=transport).generate(_request())

    assert result.usage.input_tokens == 2_000
    assert result.usage.output_tokens == 100
    assert result.usage.cost_usd == Decimal("1")


def test_reported_usage_above_reservation_is_preserved_for_gateway_accounting():
    document = _success(
        usage={
            "input_tokens": 2_200,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 120,
        }
    )
    rates = OpenAITokenRates(
        input=Decimal("2"),
        cached_input=Decimal("0.5"),
        output=Decimal("8"),
    )
    provider = OpenAIResponsesProvider(
        "key",
        rates={"model-version-1": rates},
        transport=RecordingTransport(_response(document)),
    )

    usage = provider.generate(_request()).usage

    assert usage.input_tokens == 2_200
    assert usage.output_tokens == 120
    assert usage.cost_usd == Decimal("0.00536")


def test_large_schema_cannot_escape_the_input_reservation_before_http():
    transport = RecordingTransport(_response(_success()))
    provider = OpenAIResponsesProvider("key", transport=transport)
    schema = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "description": "x" * 2_000,
            }
        },
        "required": ["decision"],
        "additionalProperties": False,
    }

    with pytest.raises(
        ProviderFailure, match="request envelope exceeds"
    ) as caught:
        provider.generate(_request(response_schema=schema))

    assert caught.value.request_was_sent is False
    assert transport.calls == []


def test_missing_cached_breakdown_uses_more_expensive_rate_classification():
    document = _success(
        usage={"input_tokens": 100, "output_tokens": 20}
    )
    provider = OpenAIResponsesProvider(
        "key",
        rates={
            "model-version-1": OpenAITokenRates(
                input=Decimal("1"),
                cached_input=Decimal("3"),
                output=Decimal("0"),
            )
        },
        transport=RecordingTransport(_response(document)),
    )

    assert provider.generate(_request()).usage.cost_usd == Decimal("0.0003")


def test_priced_reservation_above_approved_cost_fails_before_http():
    transport = RecordingTransport(_response(_success()))
    provider = OpenAIResponsesProvider(
        "key",
        rates={
            "model-version-1": OpenAITokenRates(
                input=Decimal("1000"),
                cached_input=Decimal("1000"),
                output=Decimal("1000"),
            )
        },
        transport=transport,
    )

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request(max_cost_usd=Decimal("0.01")))

    assert caught.value.request_was_sent is False
    assert caught.value.retryable is False
    assert transport.calls == []


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (408, True), (409, True), (429, True), (500, True)],
)
def test_http_failures_are_classified_without_exposing_body(status, retryable):
    secret = "body-secret-that-must-not-escape"
    transport = RecordingTransport(
        OpenAIHTTPResponse(status, secret.encode(), {})
    )

    with pytest.raises(ProviderFailure) as caught:
        OpenAIResponsesProvider("api-secret", transport=transport).generate(
            _request()
        )

    assert caught.value.retryable is retryable
    assert caught.value.request_was_sent is True
    assert secret not in str(caught.value)
    assert "api-secret" not in str(caught.value)


def test_transport_error_is_sanitized_and_conservatively_sent():
    transport = RecordingTransport(
        error=OSError("proxy echoed Bearer api-secret and body-secret")
    )

    with pytest.raises(ProviderFailure) as caught:
        OpenAIResponsesProvider("api-secret", transport=transport).generate(
            _request()
        )

    assert caught.value.retryable is True
    assert caught.value.request_was_sent is True
    assert "api-secret" not in str(caught.value)
    assert "body-secret" not in str(caught.value)


def test_key_supplier_failure_and_invalid_schema_are_preflight_failures():
    def broken_supplier():
        raise RuntimeError("secret-manager-secret")

    transport = RecordingTransport(_response(_success()))
    with pytest.raises(ProviderFailure) as caught:
        OpenAIResponsesProvider(
            broken_supplier, transport=transport
        ).generate(_request())
    assert caught.value.request_was_sent is False
    assert "secret-manager-secret" not in str(caught.value)

    with pytest.raises(ProviderFailure) as caught:
        OpenAIResponsesProvider("key", transport=transport).generate(
            _request(response_schema={})
        )
    assert caught.value.request_was_sent is False
    assert len(transport.calls) == 0


def test_repr_and_failures_never_include_api_key():
    provider = OpenAIResponsesProvider(
        "super-secret-key",
        transport=RecordingTransport(_response(_success(status="failed"))),
    )
    assert "super-secret-key" not in repr(provider)

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())
    assert "super-secret-key" not in repr(caught.value)


def test_refusal_incomplete_and_provider_error_preserve_known_usage():
    documents = [
        _success(
            output=[
                {
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "unsafe"}],
                }
            ]
        ),
        _success(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
        _success(status="failed", error={"code": "server_error", "message": "secret"}),
    ]
    expected = [(False, "refused"), (False, "incomplete"), (True, "failure")]
    for document, (retryable, message) in zip(documents, expected, strict=True):
        provider = OpenAIResponsesProvider(
            "key", transport=RecordingTransport(_response(document))
        )
        with pytest.raises(ProviderFailure) as caught:
            provider.generate(_request())
        assert caught.value.retryable is retryable
        assert caught.value.usage is not None
        assert message in str(caught.value)
        assert "secret" not in str(caught.value)
        assert "unsafe" not in str(caught.value)


def test_invalid_structured_json_is_retryable_with_known_usage():
    document = _success(
        output=[
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "not json"}],
            }
        ]
    )
    provider = OpenAIResponsesProvider(
        "key", transport=RecordingTransport(_response(document))
    )

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())

    assert caught.value.retryable is True
    assert caught.value.usage.input_tokens == 100


def test_untrusted_response_id_is_not_propagated():
    document = _success(id="response-id contains secret material")
    result = OpenAIResponsesProvider(
        "key",
        transport=RecordingTransport(
            _response(document, headers={"X-Request-Id": "req_safe_123"})
        ),
    ).generate(_request())

    assert result.provider_request_id == "req_safe_123"


def test_untrusted_error_shape_is_sanitized_instead_of_crashing():
    document = _success(status="failed", error={"code": ["not", "hashable"]})
    provider = OpenAIResponsesProvider(
        "key", transport=RecordingTransport(_response(document))
    )

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())

    assert caught.value.retryable is False
    assert "not" not in str(caught.value)
