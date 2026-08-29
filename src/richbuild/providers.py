"""Provider-neutral, budgeted model gateway for bounded RICH workers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Callable, Iterable, Mapping, Protocol
from uuid import uuid4

from .budget import BudgetLedger, ReservationExceeded, RunBudget, Usage


MODEL_ATTEMPT_EVENT_SCHEMA = "rich.model-attempt/v1"


class GenerationRole(str, Enum):
    INTERVIEWER = "interviewer"
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"


@dataclass(frozen=True, slots=True)
class ModelRequest:
    run_id: str
    task_id: str
    correlation_id: str
    role: GenerationRole
    provider: str
    model: str
    system_prompt: str
    user_prompt: str
    response_schema: Mapping[str, Any] = field(default_factory=dict)
    max_input_tokens: int = 16_000
    max_output_tokens: int = 4_000
    max_cost_usd: Decimal = Decimal("1.00")
    timeout_seconds: float = 120

    def __post_init__(self) -> None:
        required = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "provider": self.provider,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
        }
        empty = sorted(name for name, value in required.items() if not value)
        if empty:
            raise ValueError(f"model request requires {empty}")
        token_limits = (self.max_input_tokens, self.max_output_tokens)
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in token_limits
        ):
            raise ValueError("token reservations cannot be negative")
        if (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, Decimal)
            or not self.max_cost_usd.is_finite()
            # Strictly positive: a zero ceiling authorizes a call that can only
            # ever exceed it, so the request is incoherent before it is unsafe.
            # This once read `< 0` while the message said "positive", and the
            # message was the one telling the truth about the intent.
            or self.max_cost_usd <= 0
        ):
            raise ValueError("cost reservation must be positive")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout reservation must be positive")
        if self.prompt_bytes > self.max_input_tokens:
            raise ValueError(
                "prompt UTF-8 byte upper bound exceeds input token reservation"
            )

    @property
    def prompt_bytes(self) -> int:
        """Conservative token upper bound for the two textual prompt fields.

        Model tokenizers can merge multiple UTF-8 bytes into one token, but
        cannot produce more text tokens than input bytes.  Requiring the byte
        count to fit therefore avoids sending a prompt that is already known to
        exceed its token reservation without coupling the provider-neutral
        gateway to one tokenizer implementation.
        """

        return len(self.system_prompt.encode("utf-8")) + len(
            self.user_prompt.encode("utf-8")
        )

    @property
    def maximum_usage(self) -> Usage:
        return Usage(
            model_attempts=1,
            input_tokens=self.max_input_tokens,
            output_tokens=self.max_output_tokens,
            cost_usd=self.max_cost_usd,
            execution_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    provider: str
    model: str
    usage: Usage
    parsed: Any = None
    provider_request_id: str | None = None
    attempt: int = 1


class ProviderFailure(RuntimeError):
    """Provider call failed; usage may be supplied when the provider reported it."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        usage: Usage | None = None,
        request_was_sent: bool = True,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.usage = usage
        self.request_was_sent = request_was_sent


class ModelUsageRecoveryError(ValueError):
    """A persisted attempt history cannot be trusted for budget recovery."""


@dataclass(frozen=True, slots=True)
class _AttemptReservation:
    maximum: Usage
    identity: tuple[str, str, str, str, str, str, int]


class ModelProvider(Protocol):
    name: str

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Perform exactly one provider attempt."""


EventSink = Callable[[str, Mapping[str, Any]], None]



# A provider failure names a route, a status, or a missing credential -- never
# the credential itself. The adapters raise text they wrote; this bounds it so
# an unexpectedly chatty upstream cannot push a response body into the durable
# event stream.
_MAX_FAILURE_REASON = 300


def _redacted_failure(failure: BaseException) -> str:
    reason = " ".join(str(failure).split())
    if len(reason) > _MAX_FAILURE_REASON:
        reason = reason[: _MAX_FAILURE_REASON - 1] + "…"
    return reason or failure.__class__.__name__


class ModelGateway:
    """Routes one structured request while accounting for every provider retry."""

    def __init__(
        self,
        providers: list[ModelProvider],
        ledger: BudgetLedger,
        *,
        event_sink: EventSink | None = None,
    ):
        self._providers = {provider.name: provider for provider in providers}
        if len(self._providers) != len(providers):
            raise ValueError("provider names must be unique")
        self._ledger = ledger
        self._event_sink = event_sink or (lambda _event, _payload: None)

    @property
    def budget(self) -> RunBudget:
        return self._ledger.budget

    @property
    def usage(self) -> Usage:
        return self._ledger.usage

    def generate(self, request: ModelRequest, *, max_attempts: int = 1) -> ModelResponse:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        provider = self._providers.get(request.provider)
        if provider is None:
            raise KeyError(f"model provider {request.provider!r} is not registered")

        last_error: ProviderFailure | None = None
        for attempt in range(1, max_attempts + 1):
            reservation_id = (
                f"{request.correlation_id}:attempt:{attempt}:{uuid4().hex}"
            )
            maximum = request.maximum_usage
            self._ledger.reserve(reservation_id, maximum)
            base_event = {
                "schema_version": MODEL_ATTEMPT_EVENT_SCHEMA,
                "reservation_id": reservation_id,
                "run_id": request.run_id,
                "task_id": request.task_id,
                "correlation_id": request.correlation_id,
                "provider": request.provider,
                "model": request.model,
                "role": request.role.value,
                "attempt": attempt,
                "maximum_usage": maximum.to_mapping(),
            }
            try:
                self._event_sink("model.attempt.started", base_event)
            except Exception:
                # No provider request was sent. A durable start record is required
                # before crossing the network boundary.
                self._ledger.release(reservation_id)
                raise

            try:
                response = provider.generate(request)
            except ProviderFailure as exc:
                last_error = exc
                reservation_exceeded = False
                if not exc.request_was_sent:
                    self._ledger.release(reservation_id)
                    settled = Usage()
                    reservation_state = "released"
                    usage_known = False
                else:
                    usage_known = exc.usage is not None
                    try:
                        settled = (
                            _normalized_usage(exc.usage)
                            if usage_known
                            else maximum
                        )
                    except (TypeError, ValueError):
                        settled = maximum
                        usage_known = False
                    reservation_exceeded = not _usage_within(
                        settled, maximum
                    )
                    reservation_state = "settled"
                terminal_payload = {
                    **base_event,
                    # Why it failed, not merely that it did. Without this an
                    # operator reads "handler raised ProviderFailure" and has
                    # nowhere to go -- and the commonest cause, a route with no
                    # credential, is one line of text away from obvious.
                    "reason": _redacted_failure(exc),
                    "reservation_state": reservation_state,
                    "settled_usage": settled.to_mapping(),
                    "retryable": (
                        False if reservation_exceeded else exc.retryable
                    ),
                    "usage_known": usage_known,
                    "request_was_sent": exc.request_was_sent,
                    "reported_usage_exceeded_reservation": (
                        reservation_exceeded
                    ),
                }
                if reservation_exceeded:
                    # Persist the exact provider report before replacing the
                    # in-memory reservation.  A crash after this append recovers
                    # the overage, never the smaller reserved maximum.
                    self._persist_and_settle_overage(
                        reservation_id, settled, terminal_payload
                    )
                    raise ProviderFailure(
                        "provider-reported usage exceeded the reserved maximum",
                        retryable=False,
                        usage=settled,
                        request_was_sent=True,
                    ) from exc
                if exc.request_was_sent:
                    self._ledger.settle(reservation_id, settled)
                self._event_sink(
                    "model.attempt.failed",
                    terminal_payload,
                )
                if not exc.retryable:
                    raise
                continue
            except Exception:
                # An unknown provider exception may have occurred after the request was
                # accepted. Settle the full reservation rather than undercounting.
                self._ledger.settle(reservation_id, maximum)
                self._event_sink(
                    "model.attempt.failed",
                    {
                        **base_event,
                        "reservation_state": "settled",
                        "settled_usage": maximum.to_mapping(),
                        "retryable": False,
                        "usage_known": False,
                        "request_was_sent": True,
                        "reported_usage_exceeded_reservation": False,
                        "reason": "unexpected provider error",
                    },
                )
                raise
            else:
                try:
                    actual = _normalized_usage(response.usage)
                except (AttributeError, TypeError, ValueError):
                    # A malformed response is still a sent attempt. If its reported
                    # usage cannot be trusted, charge the complete reservation.
                    self._ledger.settle(reservation_id, maximum)
                    self._event_sink(
                        "model.attempt.failed",
                        {
                            **base_event,
                            "reservation_state": "settled",
                            "settled_usage": maximum.to_mapping(),
                            "retryable": False,
                            "usage_known": False,
                            "request_was_sent": True,
                            "reported_usage_exceeded_reservation": False,
                        },
                    )
                    raise
                if not _usage_within(actual, maximum):
                    terminal_payload = {
                        **base_event,
                        "reservation_state": "settled",
                        "settled_usage": actual.to_mapping(),
                        "retryable": False,
                        "usage_known": True,
                        "request_was_sent": True,
                        "reported_usage_exceeded_reservation": True,
                    }
                    # Durability precedes in-memory settlement for the same
                    # reason as the ProviderFailure overage path above.
                    reservation_error = self._persist_and_settle_overage(
                        reservation_id, actual, terminal_payload
                    )
                    raise ProviderFailure(
                        "provider-reported usage exceeded the reserved maximum",
                        retryable=False,
                        usage=actual,
                        request_was_sent=True,
                    ) from reservation_error
                self._ledger.settle(reservation_id, actual)
                result = replace(response, attempt=attempt)
                self._event_sink(
                    "model.attempt.succeeded",
                    {
                        **base_event,
                        "reservation_state": "settled",
                        "settled_usage": actual.to_mapping(),
                        "reported_usage_exceeded_reservation": False,
                    },
                )
                return result

        assert last_error is not None
        raise last_error

    def _persist_and_settle_overage(
        self,
        reservation_id: str,
        actual: Usage,
        terminal_payload: Mapping[str, Any],
    ) -> ReservationExceeded:
        """Journal exact known usage before replacing the smaller reservation.

        If a sink raises after committing its append, recovery sees the exact
        terminal event.  Settlement also occurs when the sink raises so the
        live process cannot continue with an artificially smaller in-memory
        charge.
        """

        sink_error: BaseException | None = None
        try:
            self._event_sink("model.attempt.failed", terminal_payload)
        except BaseException as exc:
            sink_error = exc
        reservation_failure: ReservationExceeded | None = None
        try:
            self._ledger.settle(reservation_id, actual)
        except ReservationExceeded as exc:
            reservation_failure = exc
        else:
            raise RuntimeError(
                "over-reservation settlement was not rejected"
            )
        if sink_error is not None:
            raise sink_error
        assert reservation_failure is not None
        return reservation_failure


def _normalized_usage(usage: Usage | None) -> Usage:
    if usage is None:
        raise ValueError("provider response must include usage")
    if usage.model_attempts not in (0, 1):
        raise ValueError("one provider call must report zero or one model attempt")
    return replace(usage, model_attempts=1)


def recover_model_usage(events: Iterable[Mapping[str, Any]]) -> Usage:
    """Recover conservative settled usage from durable gateway events.

    A started reservation without a matching terminal event is charged at its
    complete maximum. Malformed or contradictory attempt records stop recovery
    rather than allowing a restart with an artificially replenished budget.
    Unrelated run events are ignored.
    """

    reservations: dict[str, _AttemptReservation] = {}
    recovered = Usage()
    for envelope in events:
        if not isinstance(envelope, Mapping):
            raise ModelUsageRecoveryError("model event envelope must be a mapping")
        event_type = envelope.get("event_type")
        if event_type not in {
            "model.attempt.started",
            "model.attempt.succeeded",
            "model.attempt.failed",
        }:
            continue
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise ModelUsageRecoveryError(
                f"{event_type} requires a persisted payload mapping"
            )
        if payload.get("schema_version") != MODEL_ATTEMPT_EVENT_SCHEMA:
            raise ModelUsageRecoveryError(
                f"{event_type} has an unsupported or missing schema version"
            )
        reservation_id = payload.get("reservation_id")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ModelUsageRecoveryError(
                f"{event_type} requires a reservation id"
            )
        maximum = _event_usage(payload, "maximum_usage", event_type)
        identity = _event_identity(payload, event_type)
        if maximum.model_attempts != 1:
            raise ModelUsageRecoveryError(
                f"{event_type} maximum must represent exactly one model attempt"
            )

        if event_type == "model.attempt.started":
            if reservation_id in reservations:
                raise ModelUsageRecoveryError(
                    f"duplicate model reservation {reservation_id!r}"
                )
            reservations[reservation_id] = _AttemptReservation(
                maximum=maximum,
                identity=identity,
            )
            continue

        reserved = reservations.pop(reservation_id, None)
        if reserved is None:
            raise ModelUsageRecoveryError(
                f"terminal model event has no start: {reservation_id!r}"
            )
        if maximum != reserved.maximum:
            raise ModelUsageRecoveryError(
                f"model reservation maximum changed: {reservation_id!r}"
            )
        if identity != reserved.identity:
            raise ModelUsageRecoveryError(
                f"model reservation identity changed: {reservation_id!r}"
            )
        settled = _event_usage(payload, "settled_usage", event_type)
        state = payload.get("reservation_state")
        if event_type == "model.attempt.succeeded" and state != "settled":
            raise ModelUsageRecoveryError(
                "a succeeded model attempt must settle its reservation"
            )
        if event_type == "model.attempt.failed":
            request_was_sent = payload.get("request_was_sent")
            if not isinstance(request_was_sent, bool):
                raise ModelUsageRecoveryError(
                    "a failed model attempt requires request_was_sent"
                )
            if not isinstance(payload.get("retryable"), bool) or not isinstance(
                payload.get("usage_known"), bool
            ):
                raise ModelUsageRecoveryError(
                    "a failed model attempt requires boolean classifications"
                )
            expected_state = "settled" if request_was_sent else "released"
            if state != expected_state:
                raise ModelUsageRecoveryError(
                    "failed model attempt reservation state contradicts send state"
                )
        if state == "released":
            overage = payload.get(
                "reported_usage_exceeded_reservation", False
            )
            if not isinstance(overage, bool) or overage:
                raise ModelUsageRecoveryError(
                    "a released model reservation cannot report an overage"
                )
            if settled != Usage():
                raise ModelUsageRecoveryError(
                    "a released model reservation must settle zero usage"
                )
        elif state == "settled":
            overage = payload.get(
                "reported_usage_exceeded_reservation", False
            )
            if not isinstance(overage, bool):
                raise ModelUsageRecoveryError(
                    "model attempt overage classification must be boolean"
                )
            within_reservation = _usage_within(settled, reserved.maximum)
            if overage and (
                event_type != "model.attempt.failed"
                or not payload.get("request_was_sent")
                or not payload.get("usage_known")
                or within_reservation
            ):
                raise ModelUsageRecoveryError(
                    "model attempt has an invalid reported-usage overage"
                )
            if not overage and not within_reservation:
                raise ModelUsageRecoveryError(
                    "settled model usage exceeds its reservation"
                )
            recovered = recovered.plus(settled)
        else:
            raise ModelUsageRecoveryError(
                f"{event_type} has an invalid reservation state"
            )

    for reservation in reservations.values():
        recovered = recovered.plus(reservation.maximum)
    return recovered


def _event_usage(
    payload: Mapping[str, Any],
    field: str,
    event_type: str,
) -> Usage:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise ModelUsageRecoveryError(f"{event_type} requires complete {field}")
    try:
        return Usage.from_mapping(value)
    except (TypeError, ValueError) as exc:
        raise ModelUsageRecoveryError(
            f"{event_type} contains invalid {field}"
        ) from exc


def _event_identity(
    payload: Mapping[str, Any],
    event_type: str,
) -> tuple[str, str, str, str, str, str, int]:
    string_fields = (
        "run_id",
        "task_id",
        "correlation_id",
        "provider",
        "model",
        "role",
    )
    values: list[str] = []
    for field_name in string_fields:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value:
            raise ModelUsageRecoveryError(
                f"{event_type} requires non-empty {field_name}"
            )
        values.append(value)
    attempt = payload.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ModelUsageRecoveryError(
            f"{event_type} requires a positive integer attempt"
        )
    return (*values, attempt)


def _usage_within(actual: Usage, maximum: Usage) -> bool:
    return (
        actual.model_attempts <= maximum.model_attempts
        and actual.input_tokens <= maximum.input_tokens
        and actual.output_tokens <= maximum.output_tokens
        and actual.cost_usd <= maximum.cost_usd
        and actual.execution_seconds <= maximum.execution_seconds
    )


# ── Shared provider-adapter helpers ──────────────────────────────────
# Each adapter is its own trust boundary and keeps its own parsing, but these
# were byte-identical copies doing nothing vendor-specific. The only real
# difference between the request-id readers was which header the API answers
# with, so that is the parameter rather than a second function. The request
# envelope is measured with canonical.canonical_json_bytes -- one encoding,
# never a provider-side second one.

SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def non_negative_int(value: Any) -> int:
    """Return a genuine non-negative int, refusing bools and everything else."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("value must be a non-negative integer")
    return value


def safe_request_id(
    value: Any, headers: Mapping[str, str], *, header_name: str
) -> str | None:
    """Return a provider request id safe to record, from the body or a header."""

    candidates = [value]
    candidates.extend(
        header_value
        for candidate_name, header_value in headers.items()
        if candidate_name.lower() == header_name
    )
    for candidate in candidates:
        if isinstance(candidate, str) and SAFE_REQUEST_ID.fullmatch(candidate):
            return candidate
    return None
