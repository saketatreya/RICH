import json
from decimal import Decimal
from pathlib import Path

import pytest

from richbuild.budget import BudgetExceeded, BudgetLedger, RunBudget, Usage
from richbuild.coding import DEFAULT_LIMITS
from richbuild.executor import (
    BubblewrapExecutor,
    ExecutionResult,
    SandboxUnavailable,
    TrustedNodePnpmRuntime,
    WorkspaceBootstrapError,
    WorkspaceBootstrapper,
    trusted_node_pnpm_runtime,
)
from richbuild.providers import (
    GenerationRole,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelUsageRecoveryError,
    ProviderFailure,
    recover_model_usage,
)
from richbuild.runtime import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_RATES,
    DEFAULT_PROVIDER,
    MAX_INPUT_TOKEN_RESERVATION,
    default_run_runtime,
)


def _budget_mapping(**overrides):
    value = {
        "max_model_attempts": 4,
        "max_input_tokens": 40_000,
        "max_output_tokens": 20_000,
        "max_cost_usd": "4.50",
        "max_execution_seconds": 900,
    }
    value.update(overrides)
    return value


def _request(**overrides):
    value = {
        "run_id": "run-1",
        "task_id": "task-1",
        "correlation_id": "correlation-1",
        "role": GenerationRole.IMPLEMENTER,
        "provider": "fake",
        "model": "fake-model",
        "system_prompt": "Implement the bounded contract.",
        "user_prompt": "Return files.",
        "response_schema": {
            "type": "object",
            "properties": {"files": {"type": "array"}},
            "required": ["files"],
            "additionalProperties": False,
        },
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_cost_usd": Decimal("0.50"),
        "timeout_seconds": 10,
    }
    value.update(overrides)
    return ModelRequest(**value)


class _Provider:
    name = "fake"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def generate(self, _request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def _success_response():
    return ModelResponse(
        text='{"files":[]}',
        parsed={"files": []},
        provider="fake",
        model="fake-model",
        usage=Usage(
            model_attempts=1,
            input_tokens=20,
            output_tokens=10,
            cost_usd=Decimal("0.10"),
            execution_seconds=2,
        ),
    )


def _ledger(initial_usage=None):
    return BudgetLedger(
        RunBudget.from_mapping(_budget_mapping()),
        initial_usage=initial_usage,
    )


def _envelopes(events):
    return [
        {"event_type": event_type, "payload": payload}
        for event_type, payload in events
    ]


def test_run_budget_mapping_is_complete_strict_and_canonical():
    budget = RunBudget.from_mapping(_budget_mapping(max_cost_usd="4.5000"))

    assert budget.max_cost_usd == Decimal("4.5000")
    assert budget.to_mapping() == {
        "max_model_attempts": 4,
        "max_input_tokens": 40_000,
        "max_output_tokens": 20_000,
        "max_cost_usd": "4.5",
        "max_execution_seconds": 900.0,
    }
    assert RunBudget.from_mapping(budget.to_mapping()) == budget

    with pytest.raises(ValueError, match="exactly"):
        incomplete = _budget_mapping()
        incomplete.pop("max_output_tokens")
        RunBudget.from_mapping(incomplete)
    with pytest.raises(ValueError, match="exactly"):
        RunBudget.from_mapping(_budget_mapping(unapproved_limit=1))
    with pytest.raises(ValueError, match="decimal string"):
        RunBudget.from_mapping(_budget_mapping(max_cost_usd=4.5))


def test_budget_ledger_retains_exact_recovered_overage_but_blocks_new_work():
    initial = Usage(
        model_attempts=1,
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal("0.1"),
        execution_seconds=1,
    )
    assert _ledger(initial).usage == initial

    recovered_overage = Usage(
        model_attempts=5,
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal("0.1"),
        execution_seconds=1,
    )
    breached = _ledger(recovered_overage)
    assert breached.usage == recovered_overage
    assert breached.breached is True
    with pytest.raises(BudgetExceeded, match="model attempts"):
        breached.reserve(
            "must-not-start",
            Usage(
                model_attempts=1,
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("0.01"),
                execution_seconds=1,
            ),
        )
    with pytest.raises(TypeError, match="initial_usage"):
        BudgetLedger(
            RunBudget.from_mapping(_budget_mapping()),
            initial_usage={},  # type: ignore[arg-type]
        )


def test_gateway_events_restore_settled_and_outstanding_usage_conservatively():
    provider = _Provider(response=_success_response())
    events = []
    gateway = ModelGateway(
        [provider],
        _ledger(),
        event_sink=lambda event, payload: events.append((event, dict(payload))),
    )

    gateway.generate(_request())

    started = events[0][1]
    terminal = events[1][1]
    assert started["reservation_id"] == terminal["reservation_id"]
    assert started["maximum_usage"] == _request().maximum_usage.to_mapping()
    assert terminal["reservation_state"] == "settled"
    assert terminal["settled_usage"] == _success_response().usage.to_mapping()
    assert recover_model_usage(_envelopes(events)) == _success_response().usage
    assert recover_model_usage(_envelopes(events[:1])) == _request().maximum_usage


def test_default_runtime_restores_exact_budget_breach_without_replenishing():
    maximum = Usage(
        model_attempts=1,
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0.50"),
        execution_seconds=10,
    )
    reported = Usage(
        model_attempts=1,
        input_tokens=140,
        output_tokens=50,
        cost_usd=Decimal("0.70"),
        execution_seconds=10,
    )
    identity = {
        "schema_version": "rich.model-attempt/v1",
        "reservation_id": "overage-reservation",
        "run_id": "run-1",
        "task_id": "task-1",
        "correlation_id": "overage-correlation",
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
        "role": "implementer",
        "attempt": 1,
        "maximum_usage": maximum.to_mapping(),
    }
    history = (
        {"event_type": "model.attempt.started", "payload": identity},
        {
            "event_type": "model.attempt.failed",
            "payload": {
                **identity,
                "reservation_state": "settled",
                "settled_usage": reported.to_mapping(),
                "retryable": False,
                "usage_known": True,
                "request_was_sent": True,
                "reported_usage_exceeded_reservation": True,
            },
        },
    )

    runtime = default_run_runtime(
        _budget_mapping(max_input_tokens=120),
        event_history=history,
        event_sink=lambda _event, _payload: None,
        transport=_NoNetworkTransport(),
        toolchain_factory=_toolchain,
    )

    assert runtime.initial_usage == reported
    assert runtime.ledger.usage == reported
    assert runtime.ledger.breached is True
    with pytest.raises(BudgetExceeded, match="input tokens"):
        runtime.gateway.generate(
            _request(provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL)
        )


def test_preflight_release_restores_zero_and_malformed_history_fails_closed():
    provider = _Provider(
        error=ProviderFailure(
            "local preflight",
            retryable=False,
            request_was_sent=False,
        )
    )
    events = []
    with pytest.raises(ProviderFailure):
        ModelGateway(
            [provider],
            _ledger(),
            event_sink=lambda event, payload: events.append(
                (event, dict(payload))
            ),
        ).generate(_request())

    assert events[-1][1]["reservation_state"] == "released"
    assert recover_model_usage(_envelopes(events)) == Usage()

    broken = _envelopes(events)
    broken[-1]["payload"] = {
        **broken[-1]["payload"],
        "settled_usage": {"model_attempts": 0},
    }
    with pytest.raises(ModelUsageRecoveryError):
        recover_model_usage(broken)


def test_gateway_requires_durable_start_before_calling_provider():
    provider = _Provider(response=_success_response())
    ledger = _ledger()

    def unavailable_sink(_event, _payload):
        raise RuntimeError("store unavailable")

    with pytest.raises(RuntimeError, match="store unavailable"):
        ModelGateway([provider], ledger, event_sink=unavailable_sink).generate(
            _request()
        )

    assert provider.calls == 0
    assert ledger.usage == Usage()


class _RecordingExecutor(BubblewrapExecutor):
    def __init__(self, results=None):
        super().__init__(executable="/usr/bin/bwrap")
        self.calls = []
        self.results = list(results or [])

    def run(
        self,
        workspace,
        argv,
        policy=None,
        *,
        cancellation=None,
        deadline=None,
    ):
        self.calls.append((Path(workspace), tuple(argv), policy))
        if self.results:
            return self.results.pop(0)
        return ExecutionResult(
            argv=tuple(argv),
            returncode=0,
            stdout="",
            stderr="",
            duration_seconds=0.01,
        )


def _toolchain(executor=None):
    return TrustedNodePnpmRuntime(
        executor=executor or _RecordingExecutor(),
        node_executable="/opt/rich-tools/node/bin/node",
        pnpm_script="/opt/rich-tools/pnpm/bin/pnpm.cjs",
    )


def _workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "apps" / "web").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@10.34.5"})
    )
    (root / "apps" / "web" / "package.json").write_text(
        json.dumps({"name": "@app/web"})
    )
    (root / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n"
        "importers:\n"
        "  .: {}\n"
        "  apps/web: {}\n"
    )
    return root


def test_workspace_bootstrap_uses_pinned_commands_and_bounded_network(tmp_path):
    executor = _RecordingExecutor()
    bootstrapper = WorkspaceBootstrapper(_toolchain(executor))

    result = bootstrapper.bootstrap(_workspace(tmp_path))

    assert result.passed
    assert len(executor.calls) == 2
    _, install_argv, install_policy = executor.calls[0]
    assert install_argv[:2] == (
        "/opt/rich-tools/node/bin/node",
        "/opt/rich-tools/pnpm/bin/pnpm.cjs",
    )
    assert install_argv[2:5] == (
        "install",
        "--frozen-lockfile",
        "--ignore-scripts",
    )
    assert "--network-concurrency" in install_argv
    assert install_policy.network is True
    assert install_policy.timeout_seconds == bootstrapper.timeout_seconds == 1800
    assert install_policy.environment["CI"] == "1"
    assert install_policy.environment["PNPM_MAX_WORKERS"] == "1"
    assert install_policy.environment["NODE_OPTIONS"] == (
        "--disable-wasm-trap-handler --max-old-space-size=1536"
    )
    assert install_policy.max_memory_bytes == 3_221_225_472
    assert install_policy.environment["PLAYWRIGHT_BROWSERS_PATH"] == (
        "/workspace/.rich/runtime/playwright"
    )
    assert install_policy.writable_paths == (
        ".rich/runtime",
        "node_modules",
        "apps/web/node_modules",
    )
    _, browser_argv, browser_policy = executor.calls[1]
    assert browser_argv[-4:] == ("exec", "playwright", "install", "chromium")
    assert browser_policy == install_policy


def test_workspace_bootstrap_rejects_unpinned_or_failed_installs(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@latest"})
    )
    executor = _RecordingExecutor()
    with pytest.raises(WorkspaceBootstrapError, match="must pin"):
        WorkspaceBootstrapper(_toolchain(executor)).bootstrap(workspace)
    assert executor.calls == []

    (workspace / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@10.34.5"})
    )
    failed = ExecutionResult(
        argv=("pnpm",),
        returncode=1,
        stdout="registry output must not enter the error",
        stderr="secret-looking output",
        duration_seconds=0.1,
    )
    executor = _RecordingExecutor([failed])
    with pytest.raises(WorkspaceBootstrapError) as caught:
        WorkspaceBootstrapper(_toolchain(executor)).bootstrap(workspace)
    assert "secret-looking" not in str(caught.value)
    assert len(executor.calls) == 1


def test_trusted_toolchain_factory_validates_exact_local_versions(tmp_path):
    node_root = tmp_path / "node"
    (node_root / "bin").mkdir(parents=True)
    (node_root / "include" / "node").mkdir(parents=True)
    (node_root / "bin" / "node").write_bytes(b"node")
    (node_root / "include" / "node" / "node_version.h").write_text(
        "#define NODE_MAJOR_VERSION 22\n"
        "#define NODE_MINOR_VERSION 23\n"
        "#define NODE_PATCH_VERSION 2\n"
    )
    pnpm_root = tmp_path / "pnpm"
    (pnpm_root / "bin").mkdir(parents=True)
    (pnpm_root / "bin" / "pnpm.cjs").write_text("/* pinned */")
    (pnpm_root / "package.json").write_text(
        json.dumps({"name": "pnpm", "version": "10.34.5"})
    )
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("stub")

    runtime = trusted_node_pnpm_runtime(
        node_root=node_root,
        pnpm_root=pnpm_root,
        bubblewrap_executable=str(bwrap),
    )

    assert runtime.node_version == "22.23.2"
    assert runtime.pnpm_version == "10.34.5"
    assert runtime.executor.tool_mounts[0].host_path == node_root.resolve()
    assert runtime.executor.tool_aliases[0].guest_path == (
        "/opt/rich-tools/bin/pnpm"
    )
    assert runtime.verification_argv("typecheck")[-3:] == (
        "/opt/rich-tools/pnpm/bin/pnpm.cjs",
        "run",
        "typecheck",
    )

    (node_root / "include" / "node" / "node_version.h").write_text(
        "#define NODE_MAJOR_VERSION 20\n"
        "#define NODE_MINOR_VERSION 0\n"
        "#define NODE_PATCH_VERSION 0\n"
    )
    with pytest.raises(SandboxUnavailable, match="version mismatch"):
        trusted_node_pnpm_runtime(
            node_root=node_root,
            pnpm_root=pnpm_root,
            bubblewrap_executable=str(bwrap),
        )


class _NoNetworkTransport:
    def __init__(self):
        self.calls = []

    def post_json(self, **call):
        self.calls.append(call)
        raise AssertionError("HTTP must not be called")


def test_default_runtime_is_lazy_exact_priced_and_restart_aware(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    transport = _NoNetworkTransport()
    outstanding = {
        "event_type": "model.attempt.started",
        "payload": {
            "schema_version": "rich.model-attempt/v1",
            "reservation_id": "old-reservation",
            "run_id": "run-1",
            "task_id": "task-1",
            "correlation_id": "old-correlation",
            "provider": DEFAULT_PROVIDER,
            "model": DEFAULT_MODEL,
            "role": "implementer",
            "attempt": 1,
            "maximum_usage": Usage(
                model_attempts=1,
                input_tokens=100,
                output_tokens=50,
                cost_usd=Decimal("0.50"),
                execution_seconds=10,
            ).to_mapping(),
        },
    }
    runtime = default_run_runtime(
        _budget_mapping(max_input_tokens=MAX_INPUT_TOKEN_RESERVATION + 100_000),
        event_history=(outstanding,),
        event_sink=lambda _event, _payload: None,
        transport=transport,
        toolchain_factory=_toolchain,
    )

    assert runtime.model == "claude-sonnet-5"
    assert DEFAULT_MODEL_RATES.input == Decimal("2.00")
    assert DEFAULT_MODEL_RATES.cache_write_5m == Decimal("2.50")
    assert DEFAULT_MODEL_RATES.cache_write_1h == Decimal("4.00")
    assert DEFAULT_MODEL_RATES.cache_read == Decimal("0.20")
    assert DEFAULT_MODEL_RATES.output == Decimal("10.00")
    assert DEFAULT_MODEL_RATES.costliest_input == Decimal("4.00")
    assert DEFAULT_LIMITS.max_cost_usd == (
        Decimal(DEFAULT_LIMITS.max_input_tokens)
        * DEFAULT_MODEL_RATES.costliest_input
        + Decimal(DEFAULT_LIMITS.max_output_tokens)
        * DEFAULT_MODEL_RATES.output
    ) / Decimal(1_000_000)
    assert runtime.ledger.usage == Usage(
        model_attempts=1,
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0.50"),
        execution_seconds=10,
    )
    assert {
        runtime.commands.lint_argv[-1],
        runtime.commands.static_argv[-1],
        runtime.commands.unit_argv[-1],
        runtime.commands.build_argv[-1],
        runtime.commands.acceptance_argv[-1],
    } == {"lint", "typecheck", "test", "build", "test:e2e"}
    for argv in (
        runtime.commands.lint_argv,
        runtime.commands.static_argv,
        runtime.commands.unit_argv,
        runtime.commands.build_argv,
        runtime.commands.acceptance_argv,
    ):
        assert argv[:2] == (
            "/opt/rich-tools/node/bin/node",
            "/opt/rich-tools/pnpm/bin/pnpm.cjs",
        )
    assert transport.calls == []

    request = _request(
        provider=DEFAULT_PROVIDER,
        model=DEFAULT_MODEL,
        max_input_tokens=2_000,
        max_output_tokens=5,
        max_cost_usd=Decimal("0.10"),
    )
    with pytest.raises(
        ProviderFailure, match="credential is missing"
    ) as missing_key:
        runtime.gateway.generate(request)
    assert missing_key.value.request_was_sent is False
    assert transport.calls == []

    with pytest.raises(ProviderFailure, match="only permits") as wrong_model:
        runtime.gateway.generate(
            _request(
                correlation_id="wrong-model",
                provider=DEFAULT_PROVIDER,
                model="claude-opus-5",
            )
        )
    assert wrong_model.value.request_was_sent is False
    assert transport.calls == []

    with pytest.raises(ProviderFailure, match="context window") as oversized:
        runtime.gateway.generate(
            _request(
                correlation_id="oversized-input",
                provider=DEFAULT_PROVIDER,
                model=DEFAULT_MODEL,
                max_input_tokens=MAX_INPUT_TOKEN_RESERVATION + 1,
            )
        )
    assert oversized.value.request_was_sent is False
    assert transport.calls == []


def test_the_cli_route_gives_the_prompt_the_headroom_the_proof_measured():
    from richbuild.coding import DEFAULT_LIMITS
    from richbuild.runtime import CLAUDE_CODE_LIMITS

    # Two live measurements, one bound. M7's proof measured the web task of a
    # four-component persisting application at 25,186 bytes with no failure
    # carried; the M4 drive's reopened web retry, carrying the failure it was
    # shown, at 29,332. Both routes now carry the larger number, so the CLI
    # route pins nothing of its own here.
    assert DEFAULT_LIMITS.max_prompt_bytes == 48_000
    assert CLAUDE_CODE_LIMITS.max_prompt_bytes == DEFAULT_LIMITS.max_prompt_bytes
    assert CLAUDE_CODE_LIMITS.max_prompt_bytes > 29_332
    assert CLAUDE_CODE_LIMITS.max_prompt_bytes <= CLAUDE_CODE_LIMITS.max_input_tokens

    # The output reservation is not a bound on this route -- nothing here can
    # cap output before the fact -- so it has to sit above what the model
    # actually produces, not where the model happens to land. The ninth M4
    # live drive measured 24,042 output tokens against a 24,000 reservation and
    # then 26,437: two attempts that had done the work, discarded for an
    # overage of 0.2%, and a run dead with the task exhausted. What bounds an
    # attempt here is the dollar ceiling, which those same attempts settled at
    # $0.29 against $1.00.
    assert CLAUDE_CODE_LIMITS.max_output_tokens == 64_000
    assert CLAUDE_CODE_LIMITS.max_output_tokens > 26_437 * 2
    assert CLAUDE_CODE_LIMITS.max_cost_usd == Decimal("1.00")
