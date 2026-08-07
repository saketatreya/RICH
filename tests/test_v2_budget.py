from decimal import Decimal

import pytest

from rich_v2.budget import (
    BudgetExceeded,
    BudgetLedger,
    ReservationExceeded,
    RunBudget,
    Usage,
)


def test_budget_is_reserved_before_attempt_and_retries_count_individually():
    ledger = BudgetLedger(
        RunBudget(
            max_model_attempts=2,
            max_input_tokens=200,
            max_output_tokens=100,
            max_cost_usd=Decimal("1.00"),
            max_execution_seconds=60,
        )
    )
    maximum = Usage(
        model_attempts=1,
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0.50"),
        execution_seconds=10,
    )
    ledger.reserve("attempt-1", maximum)
    ledger.reserve("attempt-2", maximum)

    with pytest.raises(BudgetExceeded, match="model attempts"):
        ledger.reserve("attempt-3", maximum)

    ledger.settle(
        "attempt-1",
        Usage(
            model_attempts=1,
            input_tokens=80,
            output_tokens=20,
            cost_usd=Decimal("0.30"),
            execution_seconds=4,
        ),
    )
    assert ledger.usage.model_attempts == 2


def test_failed_attempt_can_release_its_unused_reservation():
    ledger = BudgetLedger(
        RunBudget(1, 100, 100, Decimal("1.00"), 30)
    )
    reservation = Usage(1, 100, 100, Decimal("1.00"), 30)
    ledger.reserve("attempt", reservation)
    ledger.release("attempt")

    assert ledger.usage == Usage()


def test_reported_overage_is_charged_before_settlement_fails_closed():
    ledger = BudgetLedger(
        RunBudget(2, 1_000, 1_000, Decimal("10.00"), 60)
    )
    reservation = Usage(1, 100, 50, Decimal("0.50"), 10)
    reported = Usage(1, 140, 50, Decimal("0.70"), 10)
    ledger.reserve("attempt", reservation)

    with pytest.raises(ReservationExceeded) as caught:
        ledger.settle("attempt", reported)

    assert caught.value.maximum == reservation
    assert caught.value.actual == reported
    assert ledger.usage == reported
    with pytest.raises(KeyError, match="unknown reservation"):
        ledger.release("attempt")


@pytest.mark.parametrize(
    "usage",
    [
        lambda: Usage(model_attempts=-1),
        lambda: Usage(input_tokens=-1),
        lambda: Usage(output_tokens=-1),
        lambda: Usage(cost_usd=Decimal("-0.01")),
        lambda: Usage(execution_seconds=-0.1),
        lambda: Usage(cost_usd=Decimal("NaN")),
    ],
)
def test_negative_or_non_finite_usage_cannot_replenish_budget(usage):
    with pytest.raises(ValueError, match="usage"):
        usage()
