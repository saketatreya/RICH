"""Trusted production construction for one approved RICH v2 build run.

This module is intentionally configuration-light: one exact Anthropic model, one
explicit price table, one pinned local Node/pnpm toolchain, and no fallback to
ambient package managers. Credentials remain lazy and are resolved only when
the provider is about to cross the network boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
import os
from typing import Any

from .anthropic_provider import (
    AnthropicMessagesProvider,
    AnthropicTokenRates,
    AnthropicTransport,
    ApiKeySource,
)
from .budget import BudgetLedger, RunBudget, Usage
from .executor import (
    BubblewrapExecutor,
    TrustedNodePnpmRuntime,
    WorkspaceBootstrapper,
    trusted_node_pnpm_runtime,
)
from .providers import (
    EventSink,
    ModelGateway,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderFailure,
    recover_model_usage,
)


DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5"
# The published Claude Sonnet 5 price table. Every input classification is
# listed separately because the Messages API reports the four counts
# separately, so the adapter charges the exact reported mix rather than
# assuming one classification for the whole prompt.
DEFAULT_MODEL_RATES = AnthropicTokenRates(
    input=Decimal("2.00"),
    cache_write_5m=Decimal("2.50"),
    cache_write_1h=Decimal("4.00"),
    cache_read=Decimal("0.20"),
    output=Decimal("10.00"),
)
# Claude Sonnet 5 prices its full context window at one flat rate, so this is a
# capacity bound rather than a pricing tier: a reservation larger than the
# window can never be satisfied, and refusing it costs nothing.
MAX_INPUT_TOKEN_RESERVATION = 1_000_000


@dataclass(frozen=True, slots=True)
class PinnedRunCommands:
    """The five package scripts consumed by ``RunEngineConfig``."""

    lint_argv: tuple[str, ...]
    static_argv: tuple[str, ...]
    unit_argv: tuple[str, ...]
    build_argv: tuple[str, ...]
    acceptance_argv: tuple[str, ...]

    @classmethod
    def for_toolchain(
        cls, toolchain: TrustedNodePnpmRuntime
    ) -> "PinnedRunCommands":
        if not isinstance(toolchain, TrustedNodePnpmRuntime):
            raise TypeError("toolchain must be a TrustedNodePnpmRuntime")
        return cls(
            lint_argv=toolchain.verification_argv("lint"),
            static_argv=toolchain.verification_argv("typecheck"),
            unit_argv=toolchain.verification_argv("test"),
            build_argv=toolchain.verification_argv("build"),
            acceptance_argv=toolchain.verification_argv("test:e2e"),
        )


@dataclass(frozen=True, slots=True)
class DefaultRunRuntime:
    """Components that can be wired directly into ``RunEngine``."""

    budget: RunBudget
    initial_usage: Usage
    ledger: BudgetLedger
    anthropic_provider: AnthropicMessagesProvider
    gateway: ModelGateway
    toolchain: TrustedNodePnpmRuntime
    bootstrapper: WorkspaceBootstrapper
    commands: PinnedRunCommands
    provider_name: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL

    @property
    def executor(self) -> BubblewrapExecutor:
        return self.toolchain.executor

    def verification_argv(self, script: str) -> tuple[str, ...]:
        return self.toolchain.verification_argv(script)


@dataclass(frozen=True, slots=True)
class DefaultRunRuntimeFactory:
    """Callable factory for restart-safe, exact-model run dependencies."""

    event_sink: EventSink
    api_key_source: ApiKeySource | None = None
    transport: AnthropicTransport | None = None
    toolchain_factory: Callable[[], TrustedNodePnpmRuntime] = (
        trusted_node_pnpm_runtime
    )

    def __post_init__(self) -> None:
        if not callable(self.event_sink):
            raise TypeError("event_sink must be callable")
        if self.api_key_source is not None and not (
            isinstance(self.api_key_source, str)
            or callable(self.api_key_source)
        ):
            raise TypeError("api_key_source must be a string or callable")
        if not callable(self.toolchain_factory):
            raise TypeError("toolchain_factory must be callable")

    def __call__(
        self,
        budget: RunBudget | Mapping[str, Any],
        *,
        event_history: Iterable[Mapping[str, Any]] = (),
    ) -> DefaultRunRuntime:
        approved_budget = (
            budget
            if isinstance(budget, RunBudget)
            else RunBudget.from_mapping(budget)
        )
        initial_usage = recover_model_usage(event_history)
        ledger = BudgetLedger(
            approved_budget,
            initial_usage=initial_usage,
        )
        provider = AnthropicMessagesProvider(
            (
                _environment_anthropic_api_key
                if self.api_key_source is None
                else self.api_key_source
            ),
            rates={DEFAULT_MODEL: DEFAULT_MODEL_RATES},
            transport=self.transport,
        )
        exact_provider = _ExactModelProvider(provider)
        gateway = ModelGateway(
            [exact_provider],
            ledger,
            event_sink=self.event_sink,
        )
        toolchain = self.toolchain_factory()
        if not isinstance(toolchain, TrustedNodePnpmRuntime):
            raise TypeError(
                "toolchain_factory must return TrustedNodePnpmRuntime"
            )
        return DefaultRunRuntime(
            budget=approved_budget,
            initial_usage=initial_usage,
            ledger=ledger,
            anthropic_provider=provider,
            gateway=gateway,
            toolchain=toolchain,
            bootstrapper=WorkspaceBootstrapper(toolchain),
            commands=PinnedRunCommands.for_toolchain(toolchain),
        )


def default_run_runtime(
    budget: RunBudget | Mapping[str, Any],
    *,
    event_history: Iterable[Mapping[str, Any]] = (),
    event_sink: EventSink,
    api_key_source: ApiKeySource | None = None,
    transport: AnthropicTransport | None = None,
    toolchain_factory: Callable[[], TrustedNodePnpmRuntime] = (
        trusted_node_pnpm_runtime
    ),
) -> DefaultRunRuntime:
    """Construct the default runtime through a convenient callable interface."""

    return DefaultRunRuntimeFactory(
        event_sink=event_sink,
        api_key_source=api_key_source,
        transport=transport,
        toolchain_factory=toolchain_factory,
    )(budget, event_history=event_history)


def _environment_anthropic_api_key() -> str:
    # AnthropicMessagesProvider performs validation and redacts all resolver
    # errors. Returning an empty string keeps construction lazy and fails
    # before HTTP.
    return os.environ.get("ANTHROPIC_API_KEY", "")


class _ExactModelProvider(ModelProvider):
    """Prevent unpriced or user-selected models from crossing this boundary."""

    name = DEFAULT_PROVIDER

    def __init__(self, delegate: AnthropicMessagesProvider):
        self._delegate = delegate

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.provider != DEFAULT_PROVIDER or request.model != DEFAULT_MODEL:
            raise ProviderFailure(
                f"default runtime only permits {DEFAULT_PROVIDER}/{DEFAULT_MODEL}",
                retryable=False,
                request_was_sent=False,
            )
        if request.max_input_tokens > MAX_INPUT_TOKEN_RESERVATION:
            # The reservation exceeds the model's context window, so no attempt
            # against it can succeed. Refuse before reserving budget for it.
            raise ProviderFailure(
                "default runtime input reservation exceeds the model context window",
                retryable=False,
                request_was_sent=False,
            )
        return self._delegate.generate(request)
