import json
from decimal import Decimal

import pytest

from rich_v2.providers import GenerationRole, ModelRequest, ProviderFailure
from rich_v2.anthropic_provider import (
    ANTHROPIC_API_VERSION,
    AnthropicHTTPResponse,
    AnthropicMessagesProvider,
    AnthropicTokenRates,
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


def _rates(**overrides):
    values = {
        "input": Decimal("2"),
        "cache_write_5m": Decimal("2.5"),
        "cache_write_1h": Decimal("4"),
        "cache_read": Decimal("0.2"),
        "output": Decimal("8"),
    }
    values.update(overrides)
    return AnthropicTokenRates(**values)


def _request(**overrides):
    values = {
        "run_id": "run-1",
        "task_id": "task-1",
        "correlation_id": "corr-1",
        "role": GenerationRole.IMPLEMENTER,
        "provider": "anthropic",
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
    return AnthropicHTTPResponse(
        status_code=status,
        body=json.dumps(document).encode(),
        headers=headers or {},
    )


def _success(**overrides):
    document = {
        "id": "msg_safe123",
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": '{"decision":"ship"}'}],
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 25,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 30,
                "ephemeral_1h_input_tokens": 10,
            },
            "output_tokens": 20,
        },
    }
    document.update(overrides)
    return document


def test_sends_tool_free_schema_request_and_parses_output():
    transport = RecordingTransport(_response(_success()))
    provider = AnthropicMessagesProvider("secret-key", transport=transport)

    result = provider.generate(_request())

    assert result.parsed == {"decision": "ship"}
    assert result.text == '{"decision":"ship"}'
    assert result.provider == "anthropic"
    assert result.provider_request_id == "msg_safe123"
    call = transport.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "secret-key"
    assert call["headers"]["anthropic-version"] == ANTHROPIC_API_VERSION
    assert call["timeout_seconds"] == 30
    assert "tools" not in call["payload"]
    assert call["payload"]["system"] == "Return an implementation decision."
    assert call["payload"]["messages"] == [
        {"role": "user", "content": "Build the bounded component."}
    ]
    assert call["payload"]["max_tokens"] == 100
    assert call["payload"]["output_config"] == {
        "effort": "high",
        "format": {
            "type": "json_schema",
            "schema": _request().response_schema,
        },
    }


def test_effort_is_explicit_in_every_request_and_validated_at_construction():
    transport = RecordingTransport(_response(_success()))
    provider = AnthropicMessagesProvider(
        "key", transport=transport, effort="medium"
    )

    provider.generate(_request())

    assert transport.calls[0]["payload"]["output_config"]["effort"] == "medium"
    with pytest.raises(ValueError, match="effort"):
        AnthropicMessagesProvider("key", transport=transport, effort="adaptive")


def test_charges_every_input_classification_at_its_own_rate():
    transport = RecordingTransport(_response(_success()))
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=transport,
    )

    usage = provider.generate(_request()).usage

    # Usage.input_tokens is the whole prompt.  Anthropic reports the uncached
    # remainder in input_tokens, so cache writes and reads must be added back.
    assert usage.input_tokens == 165
    assert usage.output_tokens == 20
    # 100*2 + 30*2.5 + 10*4 + 25*0.2 + 20*8 == 480 per million.
    assert usage.cost_usd == Decimal("0.00048")
    assert usage.model_attempts == 1


def test_missing_cache_write_breakdown_uses_the_costlier_one_hour_rate():
    document = _success(
        usage={
            "input_tokens": 100,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 25,
            "output_tokens": 20,
        }
    )
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=RecordingTransport(_response(document)),
    )

    # 100*2 + 40*4 + 25*0.2 + 20*8 == 525 per million.
    assert provider.generate(_request()).usage.cost_usd == Decimal("0.000525")


def test_cache_write_breakdown_that_does_not_reconcile_is_not_trusted():
    document = _success(
        usage={
            "input_tokens": 100,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 25,
            # Understates the total, which would under-charge if believed.
            "cache_creation": {
                "ephemeral_5m_input_tokens": 5,
                "ephemeral_1h_input_tokens": 5,
            },
            "output_tokens": 20,
        }
    )
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=RecordingTransport(_response(document)),
    )

    assert provider.generate(_request()).usage.cost_usd == Decimal("0.000525")


def test_missing_pricing_settles_conservative_approved_cost():
    transport = RecordingTransport(_response(_success()))
    result = AnthropicMessagesProvider("key", transport=transport).generate(
        _request()
    )

    assert result.usage.cost_usd == Decimal("1")


def test_missing_or_malformed_usage_settles_all_conservative_maxima():
    for usage in (None, {"input_tokens": -1, "output_tokens": 5}, "usage"):
        transport = RecordingTransport(_response(_success(usage=usage)))
        result = AnthropicMessagesProvider("key", transport=transport).generate(
            _request()
        )

        assert result.usage.input_tokens == 2_000
        assert result.usage.output_tokens == 100
        assert result.usage.cost_usd == Decimal("1")


def test_reported_usage_above_reservation_is_preserved_for_gateway_accounting():
    document = _success(
        usage={"input_tokens": 2_200, "output_tokens": 120}
    )
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=RecordingTransport(_response(document)),
    )

    usage = provider.generate(_request()).usage

    assert usage.input_tokens == 2_200
    assert usage.output_tokens == 120
    assert usage.cost_usd == Decimal("0.00536")


def test_large_schema_cannot_escape_the_input_reservation_before_http():
    transport = RecordingTransport(_response(_success()))
    provider = AnthropicMessagesProvider("key", transport=transport)
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


def test_priced_reservation_above_approved_cost_fails_before_http():
    transport = RecordingTransport(_response(_success()))
    provider = AnthropicMessagesProvider(
        "key",
        rates={
            "model-version-1": _rates(
                input=Decimal("1000"),
                cache_write_5m=Decimal("1000"),
                cache_write_1h=Decimal("1000"),
                cache_read=Decimal("1000"),
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


def test_reservation_is_priced_at_the_costliest_input_classification():
    # A cache-write-heavy prompt must not be reserved at the base input rate.
    assert _rates().costliest_input == Decimal("4")
    transport = RecordingTransport(_response(_success()))
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=transport,
    )

    # 2000*4 + 100*8 == 8800 per million == 0.0088.
    provider.generate(_request(max_cost_usd=Decimal("0.0088")))
    assert len(transport.calls) == 1

    with pytest.raises(ProviderFailure, match="approved cost limit"):
        provider.generate(_request(max_cost_usd=Decimal("0.00879")))
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (408, True),
        (409, True),
        (429, True),
        (500, True),
        (504, True),
        (529, True),
    ],
)
def test_http_failures_are_classified_without_exposing_body(status, retryable):
    secret = "body-secret-that-must-not-escape"
    transport = RecordingTransport(
        AnthropicHTTPResponse(status, secret.encode(), {})
    )

    with pytest.raises(ProviderFailure) as caught:
        AnthropicMessagesProvider("api-secret", transport=transport).generate(
            _request()
        )

    assert caught.value.retryable is retryable
    assert caught.value.request_was_sent is True
    assert secret not in str(caught.value)
    assert "api-secret" not in str(caught.value)


def test_error_body_cannot_widen_a_terminal_status_into_a_retry():
    body = json.dumps(
        {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "secret-detail"},
            "request_id": "req_x",
        }
    ).encode()
    transport = RecordingTransport(AnthropicHTTPResponse(400, body, {}))

    with pytest.raises(ProviderFailure) as caught:
        AnthropicMessagesProvider("key", transport=transport).generate(_request())

    assert caught.value.retryable is False
    assert "secret-detail" not in str(caught.value)


def test_transport_error_is_sanitized_and_conservatively_sent():
    transport = RecordingTransport(
        error=OSError("proxy echoed x-api-key api-secret and body-secret")
    )

    with pytest.raises(ProviderFailure) as caught:
        AnthropicMessagesProvider("api-secret", transport=transport).generate(
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
        AnthropicMessagesProvider(
            broken_supplier, transport=transport
        ).generate(_request())
    assert caught.value.request_was_sent is False
    assert "secret-manager-secret" not in str(caught.value)

    with pytest.raises(ProviderFailure) as caught:
        AnthropicMessagesProvider("key", transport=transport).generate(
            _request(response_schema={})
        )
    assert caught.value.request_was_sent is False
    assert len(transport.calls) == 0


def test_request_routed_to_another_provider_is_refused_before_http():
    transport = RecordingTransport(_response(_success()))

    with pytest.raises(ProviderFailure, match="wrong provider") as caught:
        AnthropicMessagesProvider("key", transport=transport).generate(
            _request(provider="openai")
        )

    assert caught.value.retryable is False
    assert caught.value.request_was_sent is False
    assert transport.calls == []


def test_repr_and_failures_never_include_api_key():
    provider = AnthropicMessagesProvider(
        "super-secret-key",
        transport=RecordingTransport(
            _response(_success(stop_reason="max_tokens"))
        ),
    )
    assert "super-secret-key" not in repr(provider)

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())
    assert "super-secret-key" not in repr(caught.value)


@pytest.mark.parametrize(
    "stop_reason",
    ["max_tokens", "refusal", "model_context_window_exceeded", "tool_use"],
)
def test_every_incomplete_stop_reason_fails_closed_with_known_usage(stop_reason):
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=RecordingTransport(_response(_success(stop_reason=stop_reason))),
    )

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())

    # Replaying an identical request against an identical reservation
    # reproduces each of these, so none of them is retryable.
    assert caught.value.retryable is False
    assert "incomplete" in str(caught.value)
    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 165


def test_error_envelope_returned_with_http_200_preserves_known_usage():
    document = _success(
        type="error",
        error={"type": "overloaded_error", "message": "secret"},
    )
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=RecordingTransport(_response(document)),
    )

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())

    assert caught.value.retryable is True
    assert "failure" in str(caught.value)
    assert "secret" not in str(caught.value)
    assert caught.value.usage is not None


def test_untrusted_error_shape_is_sanitized_instead_of_crashing():
    document = _success(type="error", error={"type": ["not", "hashable"]})
    provider = AnthropicMessagesProvider(
        "key", transport=RecordingTransport(_response(document))
    )

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())

    assert caught.value.retryable is False
    assert "not" not in str(caught.value)


def test_thinking_blocks_are_ignored_and_only_text_blocks_are_output():
    document = _success(
        content=[
            {"type": "thinking", "thinking": "consider {invalid json", "signature": "s"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": '{"decision":"ship"}'},
        ]
    )
    provider = AnthropicMessagesProvider(
        "key", transport=RecordingTransport(_response(document))
    )

    assert provider.generate(_request()).parsed == {"decision": "ship"}


def test_response_without_a_text_block_is_retryable_with_known_usage():
    for content in ([], [{"type": "thinking", "thinking": "only"}], "text"):
        provider = AnthropicMessagesProvider(
            "key",
            rates={"model-version-1": _rates()},
            transport=RecordingTransport(_response(_success(content=content))),
        )

        with pytest.raises(ProviderFailure, match="structured output") as caught:
            provider.generate(_request())

        assert caught.value.retryable is True


def test_invalid_structured_json_is_retryable_with_known_usage():
    document = _success(content=[{"type": "text", "text": "not json"}])
    provider = AnthropicMessagesProvider(
        "key",
        rates={"model-version-1": _rates()},
        transport=RecordingTransport(_response(document)),
    )

    with pytest.raises(ProviderFailure) as caught:
        provider.generate(_request())

    assert caught.value.retryable is True
    assert caught.value.usage.input_tokens == 165


def test_untrusted_message_id_is_not_propagated():
    document = _success(id="message-id contains secret material")
    result = AnthropicMessagesProvider(
        "key",
        transport=RecordingTransport(
            _response(document, headers={"Request-Id": "req_safe_123"})
        ),
    ).generate(_request())

    assert result.provider_request_id == "req_safe_123"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.anthropic.com/v1",
        "https://user:pass@api.anthropic.com/v1",
        "https://api.anthropic.com/v1?key=secret",
        "",
    ],
)
def test_base_url_must_be_a_plain_https_origin(base_url):
    with pytest.raises(ValueError, match="HTTPS origin"):
        AnthropicMessagesProvider("key", base_url=base_url)


@pytest.mark.parametrize(
    "field",
    ["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"],
)
def test_every_token_rate_must_be_a_finite_non_negative_decimal(field):
    for bad in (Decimal("-1"), Decimal("NaN"), 2.5, None):
        with pytest.raises((ValueError, TypeError)):
            _rates(**{field: bad})
