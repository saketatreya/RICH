"""Attempt-aware budget accounting for model and execution work."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import math
from threading import Lock
from typing import Any


_RUN_BUDGET_FIELDS = frozenset(
    {
        "max_model_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_usd",
        "max_execution_seconds",
    }
)
_USAGE_FIELDS = frozenset(
    {
        "model_attempts",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "execution_seconds",
    }
)


class BudgetExceeded(RuntimeError):
    """A new attempt would exceed an approved run budget."""


class ReservationExceeded(ValueError):
    """Provider-reported usage exceeded one attempt's reserved maximum.

    The ledger records the reported usage before raising this exception.  That
    prevents a provider/accounting invariant violation from replenishing budget
    or being silently rounded down to the smaller reservation.
    """

    def __init__(self, maximum: "Usage", actual: "Usage"):
        super().__init__("actual usage exceeds the reserved maximum")
        self.maximum = maximum
        self.actual = actual


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_model_attempts: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: Decimal
    max_execution_seconds: float

    def __post_init__(self) -> None:
        counts = (
            self.max_model_attempts,
            self.max_input_tokens,
            self.max_output_tokens,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in counts
        ):
            raise ValueError("budget count limits must be non-negative integers")
        if (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, Decimal)
            or not self.max_cost_usd.is_finite()
            or self.max_cost_usd < 0
            or isinstance(self.max_execution_seconds, bool)
            or not isinstance(self.max_execution_seconds, (int, float))
            or not _is_finite_number(self.max_execution_seconds)
            or self.max_execution_seconds < 0
        ):
            raise ValueError("budget limits cannot be negative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunBudget":
        """Parse one complete persisted budget without guessing missing limits.

        Dollar amounts must be decimal strings (or already-validated
        :class:`Decimal` values).  Binary floats are deliberately rejected for
        money so an approval cannot change meaning during JSON round trips.
        """

        document = _complete_mapping(value, _RUN_BUDGET_FIELDS, label="run budget")
        return cls(
            max_model_attempts=document["max_model_attempts"],
            max_input_tokens=document["max_input_tokens"],
            max_output_tokens=document["max_output_tokens"],
            max_cost_usd=_decimal_value(
                document["max_cost_usd"], label="run budget max_cost_usd"
            ),
            max_execution_seconds=document["max_execution_seconds"],
        )

    def to_mapping(self) -> dict[str, int | float | str]:
        """Return the canonical JSON representation used for approval/storage."""

        return {
            "max_model_attempts": self.max_model_attempts,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd": _canonical_decimal(self.max_cost_usd),
            "max_execution_seconds": float(self.max_execution_seconds),
        }


@dataclass(frozen=True, slots=True)
class Usage:
    model_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    execution_seconds: float = 0

    def __post_init__(self) -> None:
        counts = (self.model_attempts, self.input_tokens, self.output_tokens)
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in counts
        ):
            raise ValueError("usage counts must be non-negative integers")
        if (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, Decimal)
            or not self.cost_usd.is_finite()
            or self.cost_usd < 0
            or isinstance(self.execution_seconds, bool)
            or not isinstance(self.execution_seconds, (int, float))
            or not _is_finite_number(self.execution_seconds)
            or self.execution_seconds < 0
        ):
            raise ValueError("usage values must be finite and non-negative")

    def plus(self, other: "Usage") -> "Usage":
        return Usage(
            model_attempts=self.model_attempts + other.model_attempts,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            execution_seconds=self.execution_seconds + other.execution_seconds,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Usage":
        """Strictly parse the complete usage shape persisted in gateway events."""

        document = _complete_mapping(value, _USAGE_FIELDS, label="usage")
        return cls(
            model_attempts=document["model_attempts"],
            input_tokens=document["input_tokens"],
            output_tokens=document["output_tokens"],
            cost_usd=_decimal_value(document["cost_usd"], label="usage cost_usd"),
            execution_seconds=document["execution_seconds"],
        )

    def to_mapping(self) -> dict[str, int | float | str]:
        return {
            "model_attempts": self.model_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": _canonical_decimal(self.cost_usd),
            "execution_seconds": float(self.execution_seconds),
        }


class BudgetLedger:
    """Thread-safe reservation ledger.

    Providers reserve their maximum possible attempt before making the call and settle
    the reservation with actual usage afterwards. Retries are therefore accounted for
    individually and cannot race past a global cap.
    """

    def __init__(self, budget: RunBudget, *, initial_usage: Usage | None = None):
        if not isinstance(budget, RunBudget):
            raise TypeError("budget must be a RunBudget")
        settled = Usage() if initial_usage is None else initial_usage
        if not isinstance(settled, Usage):
            raise TypeError("initial_usage must be a Usage")
        self.budget = budget
        self._settled = settled
        self._reservations: dict[str, Usage] = {}
        self._lock = Lock()

    @property
    def breached(self) -> bool:
        """Whether settled usage already exceeds the approved run budget.

        Recovery must retain exact known usage even when a provider overage
        crossed the run limit.  Such a ledger remains inspectable but every
        future positive reservation fails through the ordinary budget check.
        """

        with self._lock:
            return bool(self._failure_dimensions(self._settled))

    @property
    def usage(self) -> Usage:
        with self._lock:
            return self._total_locked()

    def _total_locked(self) -> Usage:
        total = self._settled
        for reservation in self._reservations.values():
            total = total.plus(reservation)
        return total

    def _check(self, usage: Usage) -> None:
        failures = self._failure_dimensions(usage)
        if failures:
            raise BudgetExceeded(f"approved budget exceeded: {', '.join(failures)}")

    def _failure_dimensions(self, usage: Usage) -> list[str]:
        failures: list[str] = []
        if usage.model_attempts > self.budget.max_model_attempts:
            failures.append("model attempts")
        if usage.input_tokens > self.budget.max_input_tokens:
            failures.append("input tokens")
        if usage.output_tokens > self.budget.max_output_tokens:
            failures.append("output tokens")
        if usage.cost_usd > self.budget.max_cost_usd:
            failures.append("cost")
        if usage.execution_seconds > self.budget.max_execution_seconds:
            failures.append("execution time")
        return failures

    def reserve(self, reservation_id: str, maximum: Usage) -> None:
        if not reservation_id:
            raise ValueError("reservation id cannot be empty")
        with self._lock:
            if reservation_id in self._reservations:
                raise ValueError(f"reservation {reservation_id!r} already exists")
            proposed = self._total_locked().plus(maximum)
            self._check(proposed)
            self._reservations[reservation_id] = maximum

    def settle(self, reservation_id: str, actual: Usage) -> None:
        with self._lock:
            maximum = self._reservations.pop(reservation_id, None)
            if maximum is None:
                raise KeyError(f"unknown reservation {reservation_id!r}")
            if (
                actual.model_attempts > maximum.model_attempts
                or actual.input_tokens > maximum.input_tokens
                or actual.output_tokens > maximum.output_tokens
                or actual.cost_usd > maximum.cost_usd
                or actual.execution_seconds > maximum.execution_seconds
            ):
                # The provider has already performed the work.  Preserve the
                # reported charge even when it violates the reservation; putting
                # the smaller maximum back would silently undercount the attempt.
                self._settled = self._settled.plus(actual)
                raise ReservationExceeded(maximum, actual)
            self._settled = self._settled.plus(actual)

    def release(self, reservation_id: str) -> None:
        with self._lock:
            if self._reservations.pop(reservation_id, None) is None:
                raise KeyError(f"unknown reservation {reservation_id!r}")


def _complete_mapping(
    value: Mapping[str, Any],
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    document = dict(value)
    actual = set(document)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted((repr(item) for item in actual - fields))
        raise ValueError(
            f"{label} must contain exactly {sorted(fields)}; "
            f"missing={missing}, extra={extra}"
        )
    return document


def _decimal_value(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValueError(f"{label} must be a decimal string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)
    except Exception as exc:
        raise ValueError(f"{label} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return str(value.normalize())


def _is_finite_number(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False
