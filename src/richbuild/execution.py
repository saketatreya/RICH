"""Trusted construction of the public RICH run-execution path."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from .executor import SandboxUnavailable, sandbox_availability, trusted_node_pnpm_runtime
from .run_engine import (
    CancellationToken,
    BubblewrapCommandRunner,
    DEFAULT_EXECUTION_HEARTBEAT_SECONDS,
    DEFAULT_EXECUTION_LEASE_SECONDS,
    RunEngine,
    RunEngineConfig,
    RunExecutionOwner,
    SchedulerReport,
)
from .runtime import (
    API_ROUTE,
    MODEL_ROUTES,
    DefaultRunRuntime,
    default_run_runtime,
)
from .store import RichStore, StoreError
from .target_packs.nextjs import exercised_pages


RuntimeBuilder = Callable[..., DefaultRunRuntime]


@dataclass(slots=True)
class _LeaseBoundModelEventSink:
    """Persist model-attempt events only for one exact execution owner."""

    store: RichStore
    run_id: str
    _owner_token: str | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.store, RichStore):
            raise TypeError("store must be a RichStore")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id cannot be empty")

    def bind(self, owner_token: str) -> None:
        if not isinstance(owner_token, str) or not owner_token:
            raise ValueError("owner_token cannot be empty")
        with self._lock:
            if (
                self._owner_token is not None
                and self._owner_token != owner_token
            ):
                raise RuntimeError(
                    "model event sink is already bound to an execution owner"
                )
            self._owner_token = owner_token

    def __call__(
        self,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if payload.get("run_id") != self.run_id:
            raise ValueError("model event belongs to a different run")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("model event has no durable task id")
        with self._lock:
            owner_token = self._owner_token
        if owner_token is None:
            raise StoreError(
                "model event sink has no bound execution owner"
            )
        self.store.append_event(
            self.run_id,
            event_type,
            dict(payload),
            task_id=task_id,
            owner_token=owner_token,
        )


@dataclass(frozen=True, slots=True)
class DefaultRunExecutor:
    """Build one restart-safe runtime from the run's durable approved budget."""

    store: RichStore
    runtime_builder: RuntimeBuilder = default_run_runtime
    # Which way to reach the pinned model. Explicit and never substituted: the
    # two routes spend different accounts, and picking one silently because the
    # other's credential is missing would change who pays without saying so.
    route: str = API_ROUTE

    def __post_init__(self) -> None:
        if not isinstance(self.store, RichStore):
            raise TypeError("store must be a RichStore")
        if not callable(self.runtime_builder):
            raise TypeError("runtime_builder must be callable")
        if self.route not in MODEL_ROUTES:
            raise ValueError(f"route must be one of {sorted(MODEL_ROUTES)}")

    def availability(self) -> str | None:
        """Why this host cannot execute a run right now, or None if it can.

        Asked before a run is accepted, so a build that cannot start is refused
        with the reason rather than accepted, backgrounded, and left as a run
        whose status never moves. The probe is the sandbox itself: Bubblewrap
        on PATH, and a user namespace the kernel and any outer sandbox permit.
        Nothing here is a fallback; a host that fails the probe cannot build.
        """

        reason = sandbox_availability()
        if reason:
            return reason
        # The toolchain is resolved the way a run resolves it -- exact Node and
        # pnpm identities, never downloaded -- so a version that drifted is a
        # refusal that names the drift, not a run that dies under a name that
        # says "sandbox".
        try:
            trusted_node_pnpm_runtime()
        except SandboxUnavailable as exc:
            return f"the pinned toolchain is not on this host: {exc}; run `rich doctor`"
        return None

    def execute(
        self,
        *,
        run_id: str,
        workspace: str | Path,
        architecture_approval_id: str | None = None,
    ) -> SchedulerReport:
        # Installed on the owner's own token rather than beside it: the
        # engine requires the run and its lease to share one cancellation, so
        # that losing the lease stops the run. A durable request now travels
        # the same path.
        with RunExecutionOwner.claim(
            self.store,
            run_id=run_id,
            lease_seconds=DEFAULT_EXECUTION_LEASE_SECONDS,
            heartbeat_seconds=DEFAULT_EXECUTION_HEARTBEAT_SECONDS,
            cancellation=_DurableCancellation(self.store, run_id),
        ) as execution_owner:
            run = self.store.get_run(run_id)
            event_history = self.store.all_events(run_id)
            model_events = _LeaseBoundModelEventSink(
                self.store,
                run_id,
            )
            model_events.bind(execution_owner.owner_token)

            # One shared cache per state directory, beside the workspaces: the
            # pnpm store and the browsers survive across runs, so a second
            # build installs from what the first one downloaded.
            cache_root = self.store.root.parent / "cache"
            runtime = self.runtime_builder(
                run["budget"],
                event_history=event_history,
                event_sink=model_events,
                route=self.route,
                cache_root=cache_root,
            )
            commands = runtime.commands
            config = RunEngineConfig(
                lint_argv=commands.lint_argv,
                static_argv=commands.static_argv,
                unit_argv=commands.unit_argv,
                property_argv=commands.property_argv,
                build_argv=commands.build_argv,
                acceptance_argv=commands.acceptance_argv,
                database_argv=commands.database_argv,
                probe_argv=commands.probe_argv,
                exercised_paths=exercised_pages,
                execution_lease_seconds=(
                    DEFAULT_EXECUTION_LEASE_SECONDS
                ),
                execution_heartbeat_seconds=(
                    DEFAULT_EXECUTION_HEARTBEAT_SECONDS
                ),
            )
            engine = RunEngine(
                self.store,
                gateway=runtime.gateway,
                command_runner=BubblewrapCommandRunner(
                    runtime.executor,
                    timeout_seconds=600,
                    cache_root=cache_root,
                ),
                provider=runtime.provider_name,
                model=runtime.model,
                config=config,
                workspace_preparer=runtime.bootstrapper,
                execution_owner_binding=model_events.bind,
            )
            return engine.execute(
                run_id=run_id,
                workspace=workspace,
                architecture_approval_id=architecture_approval_id,
                execution_owner=execution_owner,
            )


class _DurableCancellation(CancellationToken):
    """A cancellation token that also watches the durable record.

    The engine already checks a token at every attempt and command boundary;
    what it lacked was anyone able to set one. Reading the run's standing
    cancellation here means a request made through any surface -- this server,
    another server, or the CLI -- reaches the process actually doing the work.

    The store read is throttled because ``is_cancelled`` is checked on hot
    paths, and a cancellation that lands a second late costs nothing.
    """

    _POLL_SECONDS = 1.0

    def __init__(self, store: RichStore, run_id: str) -> None:
        super().__init__()
        self._store = store
        self._run_id = run_id
        self._checked_at = 0.0

    @property
    def is_cancelled(self) -> bool:
        if super().is_cancelled:
            return True
        now = time.monotonic()
        if now - self._checked_at < self._POLL_SECONDS:
            return False
        self._checked_at = now
        try:
            standing = self._store.run_cancellation(self._run_id)
        except Exception:
            # A cancellation check must never be the thing that fails a run.
            return False
        if standing is None:
            return False
        self.cancel(standing.get("reason") or "canceled by operator")
        return True
