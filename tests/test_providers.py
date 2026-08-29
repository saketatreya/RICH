from decimal import Decimal

import pytest

from richbuild.budget import BudgetExceeded, BudgetLedger, RunBudget, Usage
from richbuild.providers import (
    GenerationRole,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ProviderFailure,
    recover_model_usage,
)


class ScriptedProvider:
    name = "fake"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _request(**overrides):
    values = {
        "run_id": "run-1",
        "task_id": "task-1",
        "correlation_id": "call-1",
        "role": GenerationRole.IMPLEMENTER,
        "provider": "fake",
        "model": "test-model",
        "system_prompt": "Implement the contract.",
        "user_prompt": "Contract content",
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_cost_usd": Decimal("0.50"),
        "timeout_seconds": 10,
    }
    values.update(overrides)
    return ModelRequest(**values)


def _ledger(max_attempts=3):
    return BudgetLedger(
        RunBudget(
            max_model_attempts=max_attempts,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_cost_usd=Decimal("10"),
            max_execution_seconds=100,
        )
    )


def test_gateway_accounts_for_retry_before_next_provider_call():
    provider = ScriptedProvider(
        [
            ProviderFailure(
                "temporary",
                retryable=True,
                usage=Usage(
                    model_attempts=1,
                    input_tokens=20,
                    output_tokens=0,
                    cost_usd=Decimal("0.05"),
                    execution_seconds=1,
                ),
            ),
            ModelResponse(
                text="done",
                provider="fake",
                model="test-model",
                usage=Usage(
                    model_attempts=1,
                    input_tokens=20,
                    output_tokens=10,
                    cost_usd=Decimal("0.10"),
                    execution_seconds=2,
                ),
            ),
        ]
    )
    ledger = _ledger()
    events = []
    gateway = ModelGateway(
        [provider], ledger, event_sink=lambda event, payload: events.append((event, payload))
    )

    result = gateway.generate(_request(), max_attempts=2)

    assert result.text == "done"
    assert result.attempt == 2
    assert provider.calls == 2
    assert ledger.usage.model_attempts == 2
    assert [event for event, _ in events].count("model.attempt.failed") == 1


def test_unknown_failed_usage_settles_conservative_maximum():
    provider = ScriptedProvider(
        [ProviderFailure("timeout", retryable=False, usage=None)]
    )
    ledger = _ledger()

    with pytest.raises(ProviderFailure):
        ModelGateway([provider], ledger).generate(_request())

    assert ledger.usage == _request().maximum_usage


def test_budget_prevents_retry_from_being_sent():
    provider = ScriptedProvider(
        [
            ProviderFailure(
                "retry",
                retryable=True,
                usage=Usage(model_attempts=1),
            ),
            ModelResponse(
                text="must not run",
                provider="fake",
                model="test-model",
                usage=Usage(model_attempts=1),
            ),
        ]
    )
    ledger = _ledger(max_attempts=1)

    with pytest.raises(BudgetExceeded):
        ModelGateway([provider], ledger).generate(_request(), max_attempts=2)

    assert provider.calls == 1


def test_preflight_provider_failure_releases_reservation():
    provider = ScriptedProvider(
        [
            ProviderFailure(
                "local executable missing",
                retryable=False,
                request_was_sent=False,
            )
        ]
    )
    ledger = _ledger()

    with pytest.raises(ProviderFailure):
        ModelGateway([provider], ledger).generate(_request())

    assert ledger.usage == Usage()


def test_prompt_cannot_exceed_its_input_reservation():
    with pytest.raises(ValueError, match="prompt UTF-8 byte upper bound"):
        _request(
            system_prompt="é" * 40,
            user_prompt="x" * 30,
            max_input_tokens=100,
        )


def test_reported_overage_is_durable_charged_and_fails_closed():
    reported = Usage(
        model_attempts=1,
        input_tokens=140,
        output_tokens=60,
        cost_usd=Decimal("0.70"),
        execution_seconds=4,
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                text="must not be accepted",
                provider="fake",
                model="test-model",
                usage=reported,
            )
        ]
    )
    ledger = _ledger()
    events = []
    gateway = ModelGateway(
        [provider],
        ledger,
        event_sink=lambda event, payload: events.append(
            {"event_type": event, "payload": payload}
        ),
    )

    with pytest.raises(
        ProviderFailure, match="reported usage exceeded"
    ) as caught:
        gateway.generate(_request())

    assert caught.value.retryable is False
    assert caught.value.usage == reported
    assert ledger.usage == reported
    terminal = events[-1]
    assert terminal["event_type"] == "model.attempt.failed"
    assert terminal["payload"]["settled_usage"] == reported.to_mapping()
    assert terminal["payload"][
        "reported_usage_exceeded_reservation"
    ] is True

    assert recover_model_usage(events) == reported


def test_failure_with_reported_overage_cannot_retry_or_undercount():
    reported = Usage(
        model_attempts=1,
        input_tokens=101,
        output_tokens=10,
        cost_usd=Decimal("0.51"),
        execution_seconds=2,
    )
    provider = ScriptedProvider(
        [
            ProviderFailure(
                "provider asks for retry",
                retryable=True,
                usage=reported,
            ),
            ModelResponse(
                text="must not retry",
                provider="fake",
                model="test-model",
                usage=Usage(model_attempts=1),
            ),
        ]
    )
    ledger = _ledger()

    with pytest.raises(
        ProviderFailure, match="reported usage exceeded"
    ) as caught:
        ModelGateway([provider], ledger).generate(
            _request(), max_attempts=2
        )

    assert caught.value.retryable is False
    assert provider.calls == 1
    assert ledger.usage == reported


def test_persisted_overage_survives_terminal_sink_failpoint():
    reported = Usage(
        model_attempts=1,
        input_tokens=140,
        output_tokens=60,
        cost_usd=Decimal("0.70"),
        execution_seconds=4,
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                text="not accepted",
                provider="fake",
                model="test-model",
                usage=reported,
            )
        ]
    )
    ledger = _ledger()
    events = []

    def append_then_fail(event_type, payload):
        events.append({"event_type": event_type, "payload": dict(payload)})
        if event_type == "model.attempt.failed":
            raise RuntimeError("crash after durable terminal append")

    with pytest.raises(RuntimeError, match="after durable terminal"):
        ModelGateway(
            [provider], ledger, event_sink=append_then_fail
        ).generate(_request())

    assert ledger.usage == reported
    assert recover_model_usage(events) == reported


def test_shared_provider_helpers_refuse_what_each_copy_used_to():
    from richbuild.providers import non_negative_int

    assert non_negative_int(0) == 0
    for bad in (True, False, -1, 1.0, "1", None):
        with pytest.raises(ValueError):
            non_negative_int(bad)


def test_the_request_id_reader_differs_only_in_which_header_it_trusts():
    """That difference is per-API and correct; the boilerplate around it was
    duplicated verbatim."""

    from richbuild.providers import safe_request_id

    headers = {"Request-Id": "anthropic-1", "X-Request-Id": "openai-1"}

    assert safe_request_id(None, headers, header_name="request-id") == "anthropic-1"
    assert safe_request_id(None, headers, header_name="x-request-id") == "openai-1"
    assert safe_request_id("body-id", headers, header_name="request-id") == "body-id"
    assert safe_request_id(None, {}, header_name="request-id") is None
    assert (
        safe_request_id("has spaces", {}, header_name="request-id") is None
    ), "an unsafe id is dropped rather than recorded"
    assert safe_request_id("a" * 129, {}, header_name="request-id") is None


def test_a_failed_attempt_records_why(monkeypatch):
    """An operator reading "handler raised ProviderFailure" has nowhere to go,
    and the commonest cause -- a route with no credential -- is one line of
    text away from obvious."""

    from richbuild.providers import _redacted_failure

    events: list[tuple[str, dict]] = []

    class _Refusing:
        name = "refusing"

        def generate(self, request):
            raise ProviderFailure(
                "ANTHROPIC_API_KEY is not set",
                retryable=False,
                request_was_sent=False,
            )

    gateway = ModelGateway(
        [_Refusing()],
        BudgetLedger(
            RunBudget(
                max_model_attempts=2,
                max_input_tokens=1000,
                max_output_tokens=500,
                max_cost_usd=Decimal("1"),
                max_execution_seconds=60,
            )
        ),
        event_sink=lambda kind, payload: events.append((kind, dict(payload))),
    )

    with pytest.raises(ProviderFailure):
        gateway.generate(
            ModelRequest(
                run_id="run.why",
                task_id="task.why",
                correlation_id="corr.why",
                role=GenerationRole.IMPLEMENTER,
                provider="refusing",
                model="test-model",
                system_prompt="s",
                user_prompt="u",
                max_input_tokens=1000,
                max_output_tokens=500,
                max_cost_usd=Decimal("1"),
                timeout_seconds=30,
            )
        )

    failed = [payload for kind, payload in events if kind == "model.attempt.failed"]
    assert failed, "the failure must be recorded at all"
    assert failed[0]["reason"] == "ANTHROPIC_API_KEY is not set"

    # Bounded, so an unexpectedly chatty upstream cannot push a response body
    # into the durable event stream.
    assert len(_redacted_failure(RuntimeError("x" * 5000))) <= 300
    assert _redacted_failure(RuntimeError("")) == "RuntimeError"
    assert _redacted_failure(RuntimeError("a\n  b")) == "a b"
