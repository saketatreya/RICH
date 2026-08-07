"""Shared pytest policy for the hermetic and live test suites."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("rich")
    group.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked 'live' (may use external models or providers)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if config.getoption("--run-live"):
        return

    skip_live = pytest.mark.skip(
        reason="live test; pass --run-live and configure its provider to run",
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
