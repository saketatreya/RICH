"""Trusted production construction for one approved RICH v2 build run.

This module is intentionally configuration-light: one exact OpenAI model, one
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

from .budget import BudgetLedger, RunBudget, Usage
from .executor import (
    BubblewrapExecutor,
    TrustedNodePnpmRuntime,
    WorkspaceBootstrapper,
    trusted_node_pnpm_runtime,
)
from .openai_provider import (
    ApiKeySource,
    OpenAIResponsesProvider,
    OpenAITokenRates,
    OpenAITransport,
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


DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_MODEL_RATES = OpenAITokenRates(
    input=Decimal("2.50"),
    cached_input=Decimal("0.25"),
    output=Decimal("15.00"),
)
DEFAULT_CACHE_WRITE_INPUT_RATE = Decimal("3.125")
DEFAULT_BILLING_RATES = OpenAITokenRates(
    # GPT-5.6 cache writes cost 1.25x ordinary input. The current provider
    # exposes cached reads but does not retain a separate cache-write counter,
    # so charge every non-cached input token at the costlier classification.
    input=DEFAULT_CACHE_WRITE_INPUT_RATE,
    cached_input=DEFAULT_MODEL_RATES.cached_input,
    output=DEFAULT_MODEL_RATES.output,
)
MAX_BASE_RATE_INPUT_TOKENS = 272_000


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
    openai_provider: OpenAIResponsesProvider
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
    transport: OpenAITransport | None = None
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
        provider = OpenAIResponsesProvider(
            (
                _environment_openai_api_key
                if self.api_key_source is None
                else self.api_key_source
            ),
            rates={DEFAULT_MODEL: DEFAULT_BILLING_RATES},
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
            openai_provider=provider,
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
    transport: OpenAITransport | None = None,
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


def _environment_openai_api_key() -> str:
    # OpenAIResponsesProvider performs validation and redacts all resolver errors.
    # Returning an empty string keeps construction lazy and fails before HTTP.
    return os.environ.get("OPENAI_API_KEY", "")


class _ExactModelProvider(ModelProvider):
    """Prevent unpriced or user-selected models from crossing this boundary."""

    name = DEFAULT_PROVIDER

    def __init__(self, delegate: OpenAIResponsesProvider):
        self._delegate = delegate

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.provider != DEFAULT_PROVIDER or request.model != DEFAULT_MODEL:
            raise ProviderFailure(
                f"default runtime only permits {DEFAULT_PROVIDER}/{DEFAULT_MODEL}",
                retryable=False,
                request_was_sent=False,
            )
        if request.max_input_tokens > MAX_BASE_RATE_INPUT_TOKENS:
            # The published model contract applies a higher price tier above this
            # boundary. Refuse instead of using a base-rate reservation.
            raise ProviderFailure(
                "default runtime input reservation exceeds its priced tier",
                retryable=False,
                request_was_sent=False,
            )
        return self._delegate.generate(request)
