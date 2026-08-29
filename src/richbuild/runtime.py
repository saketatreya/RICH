"""Trusted production construction for one approved RICH build run.

This module is intentionally configuration-light: one exact Anthropic model, one
explicit price table, one pinned local Node/pnpm toolchain, and no fallback to
ambient package managers. Credentials remain lazy and are resolved only when
the provider is about to cross the network boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
import os
from typing import Any

from .architect import ModelArchitect
from .interviewer import ModelInterviewer
from .anthropic_provider import (
    AnthropicMessagesProvider,
    AnthropicTokenRates,
    AnthropicTransport,
    ApiKeySource,
)
from .budget import BudgetLedger, RunBudget, Usage
from .claude_code_provider import CLAUDE_CODE_PROVIDER, ClaudeCodeCliProvider
from .coding import DEFAULT_LIMITS, CodingLimits
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
# Two ways to reach one model policy, chosen explicitly. "api" needs an
# ANTHROPIC_API_KEY; "claude-code" spends an existing Claude Code login instead.
# Neither is ever a fallback for the other -- a route that cannot run fails the
# attempt rather than quietly changing who is paying or what is answering.
API_ROUTE = "api"
CLAUDE_CODE_ROUTE = "claude-code"
MODEL_ROUTES: dict[str, str] = {
    API_ROUTE: DEFAULT_PROVIDER,
    CLAUDE_CODE_ROUTE: CLAUDE_CODE_PROVIDER,
}
# The CLI route needs different bounds, from measurement rather than taste. On
# the reference project one task generated 9,275 output tokens against the
# 8,000 the API route reserves, and took 86 seconds against a 120-second
# attempt limit -- close enough that the same task timed out twice inside the
# run engine. Neither number is a defect in the model: this route adds harness
# startup and auxiliary calls on top of generation, and it cannot cap output
# before the fact, so it needs headroom the bounded HTTP route does not.
CLAUDE_CODE_LIMITS = replace(
    DEFAULT_LIMITS,
    max_output_tokens=24_000,
    # Not rate-derived: on this route the harness reports what an attempt
    # actually cost, so this is a per-attempt ceiling rather than an estimate.
    max_cost_usd=Decimal("1.00"),
    timeout_seconds=600,
)
CODING_LIMITS_BY_ROUTE: dict[str, CodingLimits] = {
    API_ROUTE: DEFAULT_LIMITS,
    CLAUDE_CODE_ROUTE: CLAUDE_CODE_LIMITS,
}


@dataclass(frozen=True, slots=True)
class PinnedRunCommands:
    """The verification commands consumed by ``RunEngineConfig``."""

    lint_argv: tuple[str, ...]
    static_argv: tuple[str, ...]
    unit_argv: tuple[str, ...]
    property_argv: tuple[str, ...]
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
            # vitest directly rather than the package script: a node is gated
            # on its own suite, and the script names the whole directory, so
            # appending one file would add a second filter rather than narrow
            # to the first.
            property_argv=toolchain.pnpm_argv(
                "exec", "vitest", "run", "--configLoader", "runner"
            ),
            build_argv=toolchain.verification_argv("build"),
            acceptance_argv=toolchain.verification_argv("test:e2e"),
        )


@dataclass(frozen=True, slots=True)
class DefaultRunRuntime:
    """Components that can be wired directly into ``RunEngine``."""

    budget: RunBudget
    initial_usage: Usage
    ledger: BudgetLedger
    model_provider: AnthropicMessagesProvider | ClaudeCodeCliProvider
    gateway: ModelGateway
    toolchain: TrustedNodePnpmRuntime
    bootstrapper: WorkspaceBootstrapper
    commands: PinnedRunCommands
    provider_name: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    route: str = API_ROUTE
    coding_limits: CodingLimits = DEFAULT_LIMITS

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
    route: str = API_ROUTE
    claude_code_provider_factory: Callable[[], ClaudeCodeCliProvider] = (
        ClaudeCodeCliProvider
    )

    def __post_init__(self) -> None:
        if not callable(self.event_sink):
            raise TypeError("event_sink must be callable")
        if self.route not in MODEL_ROUTES:
            raise ValueError(
                f"route must be one of {sorted(MODEL_ROUTES)}"
            )
        if not callable(self.claude_code_provider_factory):
            raise TypeError("claude_code_provider_factory must be callable")
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
        if self.route == CLAUDE_CODE_ROUTE:
            provider: AnthropicMessagesProvider | ClaudeCodeCliProvider = (
                self.claude_code_provider_factory()
            )
        else:
            provider = AnthropicMessagesProvider(
                (
                    _environment_anthropic_api_key
                    if self.api_key_source is None
                    else self.api_key_source
                ),
                rates={DEFAULT_MODEL: DEFAULT_MODEL_RATES},
                transport=self.transport,
            )
        exact_provider = _ExactModelProvider(provider, MODEL_ROUTES[self.route])
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
            model_provider=provider,
            gateway=gateway,
            toolchain=toolchain,
            bootstrapper=WorkspaceBootstrapper(toolchain),
            commands=PinnedRunCommands.for_toolchain(toolchain),
            provider_name=MODEL_ROUTES[self.route],
            route=self.route,
            coding_limits=CODING_LIMITS_BY_ROUTE[self.route],
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
    route: str = API_ROUTE,
) -> DefaultRunRuntime:
    """Construct the default runtime through a convenient callable interface."""

    return DefaultRunRuntimeFactory(
        event_sink=event_sink,
        api_key_source=api_key_source,
        transport=transport,
        toolchain_factory=toolchain_factory,
        route=route,
    )(budget, event_history=event_history)


def _environment_anthropic_api_key() -> str:
    # AnthropicMessagesProvider performs validation and redacts all resolver
    # errors. Returning an empty string keeps construction lazy and fails
    # before HTTP.
    return os.environ.get("ANTHROPIC_API_KEY", "")


class _ExactModelProvider(ModelProvider):
    """Prevent unpriced or user-selected models from crossing this boundary."""

    def __init__(
        self,
        delegate: AnthropicMessagesProvider | ClaudeCodeCliProvider,
        provider_name: str = DEFAULT_PROVIDER,
    ):
        if delegate.name != provider_name:
            raise ValueError("delegate does not serve the pinned provider name")
        self._delegate = delegate
        self.name = provider_name

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.provider != self.name or request.model != DEFAULT_MODEL:
            raise ProviderFailure(
                f"default runtime only permits {self.name}/{DEFAULT_MODEL}",
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


# An architecture proposal happens before any run exists, so it cannot draw on
# a run budget. This is its own ceiling, per proposal.
ARCHITECT_MAX_COST_USD = Decimal("2.00")


def default_architect(
    *,
    route: str = CLAUDE_CODE_ROUTE,
    event_sink: EventSink | None = None,
    max_cost_usd: Decimal = ARCHITECT_MAX_COST_USD,
    provider_factory: Callable[[], Any] | None = None,
) -> ModelArchitect | None:
    """Build the architect a surface should offer, or ``None`` if it cannot.

    Returning ``None`` rather than raising is deliberate: a Canvas with no
    Claude Code login should still start and still plan, deterministically,
    and say which one answered. An architect that fails at import time would
    take the whole control plane down with it.
    """

    if route not in MODEL_ROUTES:
        raise ValueError(f"route must be one of {sorted(MODEL_ROUTES)}")
    try:
        provider = (
            provider_factory()
            if provider_factory is not None
            else (
                ClaudeCodeCliProvider()
                if route == CLAUDE_CODE_ROUTE
                else AnthropicMessagesProvider(
                    _environment_anthropic_api_key,
                    rates={DEFAULT_MODEL: DEFAULT_MODEL_RATES},
                )
            )
        )
    except Exception:
        return None
    sink = event_sink or (lambda _kind, _payload: None)

    def gateway_factory() -> ModelGateway:
        # A fresh ledger per proposal: the ceiling bounds one design attempt,
        # and a human clicking Draft again should not be refused because an
        # earlier proposal already spent the process's whole allowance.
        return ModelGateway(
            [_ExactModelProvider(provider, MODEL_ROUTES[route])],
            BudgetLedger(
                RunBudget(
                    max_model_attempts=4,
                    max_input_tokens=400_000,
                    max_output_tokens=200_000,
                    max_cost_usd=max_cost_usd,
                    max_execution_seconds=3_600,
                )
            ),
            event_sink=sink,
        )

    return ModelArchitect(
        gateway_factory=gateway_factory,
        provider=MODEL_ROUTES[route],
        model=DEFAULT_MODEL,
        max_cost_usd=max_cost_usd,
    )


# One interview turn is smaller than an architecture proposal: a bounded prose
# exchange plus one structured answer.
INTERVIEWER_MAX_COST_USD = Decimal("1.00")


def default_interviewer(
    *,
    route: str = CLAUDE_CODE_ROUTE,
    event_sink: EventSink | None = None,
    max_cost_usd: Decimal = INTERVIEWER_MAX_COST_USD,
    provider_factory: Callable[[], Any] | None = None,
) -> ModelInterviewer | None:
    """Build the interviewer a surface should offer, or ``None`` if it cannot.

    Same shape and same reasoning as ``default_architect``: a canvas with no
    model route still starts, still asks the deterministic questions, and says
    which one answered.
    """

    if route not in MODEL_ROUTES:
        raise ValueError(f"route must be one of {sorted(MODEL_ROUTES)}")
    try:
        provider = (
            provider_factory()
            if provider_factory is not None
            else (
                ClaudeCodeCliProvider()
                if route == CLAUDE_CODE_ROUTE
                else AnthropicMessagesProvider(
                    _environment_anthropic_api_key,
                    rates={DEFAULT_MODEL: DEFAULT_MODEL_RATES},
                )
            )
        )
    except Exception:
        return None
    sink = event_sink or (lambda _kind, _payload: None)

    def gateway_factory() -> ModelGateway:
        return ModelGateway(
            [_ExactModelProvider(provider, MODEL_ROUTES[route])],
            BudgetLedger(
                RunBudget(
                    max_model_attempts=4,
                    max_input_tokens=200_000,
                    max_output_tokens=100_000,
                    max_cost_usd=max_cost_usd,
                    max_execution_seconds=3_600,
                )
            ),
            event_sink=sink,
        )

    return ModelInterviewer(
        gateway_factory=gateway_factory,
        provider=MODEL_ROUTES[route],
        model=DEFAULT_MODEL,
        max_cost_usd=max_cost_usd,
    )
