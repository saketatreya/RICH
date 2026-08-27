"""Durable, deterministic DAG scheduling for compiled RICH build plans.

The scheduler deliberately keeps orchestration state in :class:`RichStore`.
Handlers receive no store handle and cannot accidentally publish a success
state themselves.  A task only becomes ``succeeded`` after its handler output
and execution evidence have been durably committed.

Handler timeouts use cooperative cancellation.  Threads are daemonized so an
uncooperative handler cannot trap the control plane forever; importantly, its
worker slot is not reused while it remains alive, preserving the configured
parallelism bound.  A short grace period then fails the run closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import math
import queue
import threading
import time
from typing import Any

from .canonical import canonical_json_bytes as _canonical_json
from .compiler import CompiledArchitecture, CompiledTask
from .models import (
    ArchitectureSpec,
    Artifact as DomainArtifact,
    ArtifactKind,
    ArtifactStatus,
    BuildRun,
    BuildTask,
    Evidence,
    EvidenceKind,
    EvidenceStatus,
    ModelValidationError,
    ProjectSpec,
    RunStatus,
    TaskKind,
    TaskStatus,
    validate_release_traceability,
)
from .store import RevisionConflict, RichStore


_DEPENDENCY_SUCCESS = frozenset({"succeeded", "cached"})
_TASK_TERMINAL = frozenset(
    {"succeeded", "cached", "failed", "blocked", "canceled"}
)
_DEPENDENCY_FAILURE = frozenset({"failed", "blocked", "canceled"})
_RUN_TERMINAL = frozenset({"succeeded", "failed", "canceled"})
_RESULT_EVIDENCE_STATUSES = frozenset(
    {
        EvidenceStatus.PASSED.value,
        EvidenceStatus.FAILED.value,
        EvidenceStatus.SKIPPED.value,
        EvidenceStatus.ERROR.value,
    }
)
_SOURCE_ROLE_ALIASES = frozenset({"source", "generated-source"})


class SchedulerError(RuntimeError):
    """The durable run and compiled plan cannot be scheduled safely."""


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    """Attempt and deadline policy for one task or the scheduler default."""

    max_attempts: int = 1
    timeout_seconds: float | None = None
    retry_backoff_seconds: float = 0.0
    required_evidence_kinds: tuple[str, ...] = ()
    required_artifact_roles: tuple[str, ...] = ("source",)
    required_acceptance_scenario_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive when provided")
        if (
            isinstance(self.retry_backoff_seconds, bool)
            or not isinstance(self.retry_backoff_seconds, (int, float))
            or not math.isfinite(self.retry_backoff_seconds)
            or self.retry_backoff_seconds < 0
        ):
            raise ValueError("retry_backoff_seconds must be non-negative")
        object.__setattr__(
            self,
            "required_evidence_kinds",
            _validated_evidence_kinds(
                self.required_evidence_kinds, "required_evidence_kinds"
            ),
        )
        object.__setattr__(
            self,
            "required_artifact_roles",
            _validated_strings(
                self.required_artifact_roles,
                "required_artifact_roles",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "required_acceptance_scenario_ids",
            _validated_strings(
                self.required_acceptance_scenario_ids,
                "required_acceptance_scenario_ids",
                allow_empty=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    """A handler output that is committed before the task can succeed."""

    content: bytes
    role: str
    media_type: str = "application/octet-stream"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("artifact content must be bytes")
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("artifact role cannot be empty")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("artifact media_type cannot be empty")
        _canonical_json(dict(self.metadata))


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    """Structured evidence emitted by a handler."""

    kind: str
    status: str
    summary: str
    blocking: bool = True
    requirement_ids: tuple[str, ...] = ()
    acceptance_scenario_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            kind = EvidenceKind(self.kind)
        except (TypeError, ValueError) as exc:
            choices = ", ".join(item.value for item in EvidenceKind)
            raise ValueError(f"evidence kind must be one of: {choices}") from exc
        object.__setattr__(self, "kind", kind.value)
        try:
            status = EvidenceStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "evidence status must be passed, failed, skipped, or error"
            ) from exc
        if status.value not in _RESULT_EVIDENCE_STATUSES:
            raise ValueError(
                "evidence status must be passed, failed, skipped, or error"
            )
        object.__setattr__(self, "status", status.value)
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("evidence summary cannot be empty")
        if not isinstance(self.blocking, bool):
            raise TypeError("evidence blocking must be a boolean")
        object.__setattr__(
            self,
            "requirement_ids",
            _validated_strings(
                self.requirement_ids, "evidence requirement_ids", allow_empty=True
            ),
        )
        object.__setattr__(
            self,
            "acceptance_scenario_ids",
            _validated_strings(
                self.acceptance_scenario_ids,
                "evidence acceptance_scenario_ids",
                allow_empty=True,
            ),
        )
        _canonical_json(dict(self.details))


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Handler result. A false ``succeeded`` value consumes an attempt."""

    succeeded: bool = True
    summary: str = "handler completed"
    evidence: tuple[TaskEvidence, ...] = ()
    artifacts: tuple[ProducedArtifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.succeeded, bool):
            raise TypeError("result succeeded must be a boolean")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("result summary cannot be empty")
        if any(not isinstance(item, TaskEvidence) for item in self.evidence):
            raise TypeError("result evidence must contain TaskEvidence values")
        if any(not isinstance(item, ProducedArtifact) for item in self.artifacts):
            raise TypeError("result artifacts must contain ProducedArtifact values")


class CancellationToken:
    """Thread-safe cooperative cancellation shared by a complete run."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    def cancel(self, reason: str = "canceled by caller") -> None:
        normalized = str(reason).strip() or "canceled by caller"
        with self._lock:
            if self._reason is None:
                self._reason = normalized
            self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Read-only task context supplied to an execution handler."""

    run_id: str
    task_id: str
    node_id: str
    attempt: int
    compiled_task: CompiledTask
    _run_cancellation: CancellationToken = field(repr=False)
    _attempt_cancellation: threading.Event = field(repr=False)
    deadline_monotonic: float | None = None

    @property
    def is_cancelled(self) -> bool:
        return (
            self._run_cancellation.is_cancelled
            or self._attempt_cancellation.is_set()
        )

    def wait_for_cancellation(self, timeout: float | None = None) -> bool:
        """Wait in short increments so either cancellation source is observed."""

        if self.is_cancelled:
            return True
        if timeout is not None and timeout <= 0:
            return False
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_cancelled:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                return False
            self._attempt_cancellation.wait(
                0.05 if remaining is None else min(0.05, remaining)
            )
        return True

    @property
    def remaining_seconds(self) -> float | None:
        """Return the scheduler's exact remaining attempt budget.

        The value uses the process monotonic clock and is clamped at zero so
        handlers can pass a safe upper bound to interruptible child work.
        """

        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - time.monotonic())


TaskHandler = Callable[[TaskContext], TaskResult | None]


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    run_id: str
    status: str
    task_statuses: tuple[tuple[str, str], ...]
    task_attempts: tuple[tuple[str, int], ...]

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


@dataclass(slots=True)
class _RunningAttempt:
    compiled_task: CompiledTask
    durable_task_id: str
    attempt: int
    started_at: float
    deadline: float | None
    attempt_cancellation: threading.Event


@dataclass(frozen=True, slots=True)
class _WorkerResult:
    task_id: str
    attempt: int
    result: TaskResult | None = None
    error_type: str | None = None
    error_message: str | None = None




def _validated_strings(
    values: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be a sequence of strings")
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{label} must be a sequence of strings") from exc
    if not allow_empty and not normalized:
        raise ValueError(f"{label} cannot be empty")
    if any(not isinstance(value, str) or not value.strip() for value in normalized):
        raise ValueError(f"{label} must contain non-empty strings")
    normalized = tuple(value.strip() for value in normalized)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} cannot contain duplicates")
    return normalized


def _validated_evidence_kinds(
    values: tuple[str, ...], label: str
) -> tuple[str, ...]:
    normalized = _validated_strings(values, label, allow_empty=True)
    try:
        return tuple(EvidenceKind(value).value for value in normalized)
    except ValueError as exc:
        choices = ", ".join(item.value for item in EvidenceKind)
        raise ValueError(f"{label} must use evidence kinds: {choices}") from exc


class DagScheduler:
    """Execute a compiled architecture against durable run/task records.

    Handler lookup is deterministic: node id, compiled task id, durable task
    kind, then ``"*"``. Policies use the same order except for task kind,
    which is always ``"implement"`` for compiled architecture tasks.
    """

    def __init__(
        self,
        store: RichStore,
        *,
        run_id: str,
        plan: CompiledArchitecture,
        handlers: Mapping[str, TaskHandler],
        max_workers: int = 4,
        default_policy: TaskPolicy | None = None,
        task_policies: Mapping[str, TaskPolicy] | None = None,
        poll_interval_seconds: float = 0.02,
        cancellation_grace_seconds: float = 1.0,
        owner_token: str | None = None,
    ):
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers < 1
        ):
            raise ValueError("max_workers must be a positive integer")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if (
            not math.isfinite(cancellation_grace_seconds)
            or cancellation_grace_seconds <= 0
        ):
            raise ValueError("cancellation_grace_seconds must be positive")
        if not plan.tasks:
            raise SchedulerError("compiled plan contains no tasks")
        if owner_token is not None and (
            not isinstance(owner_token, str) or not owner_token
        ):
            raise ValueError("owner_token cannot be empty")

        self.store = store
        self.run_id = run_id
        self.owner_token = owner_token
        self.plan = plan
        self.handlers = dict(handlers)
        self.max_workers = max_workers
        self.default_policy = default_policy or TaskPolicy()
        self.task_policies = dict(task_policies or {})
        self.poll_interval_seconds = poll_interval_seconds
        self.cancellation_grace_seconds = cancellation_grace_seconds
        self._results: queue.Queue[_WorkerResult] = queue.Queue()
        self._compiled_by_node = {task.node_id: task for task in plan.tasks}
        self._durable_ids = {
            task.node_id: f"{run_id}:{task.task_id}" for task in plan.tasks
        }
        self._validate_plan()

    def _set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        return self.store.set_run_status(
            run_id,
            status,
            expected_status=expected_status,
            owner_token=self.owner_token,
        )

    def _set_task_status(
        self,
        task_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        return self.store.set_task_status(
            task_id,
            status,
            expected_status=expected_status,
            increment_attempt=increment_attempt,
            owner_token=self.owner_token,
        )

    def _append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store.append_event(
            run_id,
            event_type,
            payload,
            task_id=task_id,
            owner_token=self.owner_token,
        )

    def _attach_artifact(
        self,
        run_id: str,
        digest: str,
        *,
        role: str,
        task_id: str | None = None,
    ) -> None:
        self.store.attach_artifact(
            run_id,
            digest,
            role=role,
            task_id=task_id,
            owner_token=self.owner_token,
        )

    def run(self, cancellation: CancellationToken | None = None) -> SchedulerReport:
        token = cancellation or CancellationToken()
        run = self.store.get_run(self.run_id)
        self._validate_durable_records(run)
        if run["status"] in _RUN_TERMINAL:
            return self._report()
        if run["status"] not in {"ready", "running", "verifying"}:
            raise SchedulerError(
                f"run {self.run_id!r} is {run['status']!r}, not schedulable"
            )

        resumed = run["status"] != "ready"
        if run["status"] == "ready":
            self._set_run_status(
                self.run_id, "running", expected_status="ready"
            )
        self._append_event(
            self.run_id,
            "scheduler.started",
            {
                "max_workers": self.max_workers,
                "resumed": resumed,
                "task_count": len(self.plan.tasks),
            },
        )

        running: dict[str, _RunningAttempt] = {}
        worker_keys: set[tuple[str, int]] = set()
        abandoned_workers: dict[tuple[str, int], float] = {}
        retry_not_before: dict[str, float] = {}
        if resumed:
            self._recover_interrupted(retry_not_before)
            self._restore_retry_deadlines(retry_not_before)

        while True:
            if token.is_cancelled:
                for attempt in running.values():
                    attempt.attempt_cancellation.set()
                self._cancel_remaining(token.reason or "canceled by caller")
                return self._finish("canceled")

            self._drain_results(
                running, worker_keys, abandoned_workers, retry_not_before
            )
            now = time.monotonic()
            self._expire_attempts(
                now, running, abandoned_workers, retry_not_before
            )

            if any(
                now >= grace_deadline
                for grace_deadline in abandoned_workers.values()
            ):
                self._fail_uncooperative_workers(
                    abandoned_workers, running
                )
                return self._finish("failed")

            self._block_failed_dependencies()
            states = self._states()
            if all(state["status"] in _TASK_TERMINAL for state in states.values()):
                if all(
                    state["status"] in _DEPENDENCY_SUCCESS
                    for state in states.values()
                ):
                    return self._finish("succeeded")
                return self._finish("failed")

            capacity = self.max_workers - len(worker_keys)
            if capacity > 0:
                ready = self._ready_tasks(states, retry_not_before, now)
                for compiled_task in ready[:capacity]:
                    attempt = self._launch(compiled_task, token)
                    running[attempt.durable_task_id] = attempt
                    worker_keys.add(
                        (attempt.durable_task_id, attempt.attempt)
                    )

            if not running and not worker_keys:
                # A validated DAG can only land here if durable state was
                # externally corrupted while the scheduler was active.
                self._block_stranded_tasks()
                return self._finish("failed")

            try:
                envelope = self._results.get(
                    timeout=self.poll_interval_seconds
                )
            except queue.Empty:
                continue
            self._consume_result(
                envelope,
                running,
                worker_keys,
                abandoned_workers,
                retry_not_before,
            )

    def _validate_plan(self) -> None:
        if self.plan.project_id != self.store.get_run(self.run_id)["project_id"]:
            raise SchedulerError("compiled plan belongs to a different project")
        if len(self._compiled_by_node) != len(self.plan.tasks):
            raise SchedulerError("compiled plan has duplicate node ids")
        task_ids = [task.task_id for task in self.plan.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise SchedulerError("compiled plan has duplicate task ids")
        known = set(self._compiled_by_node)
        for task in self.plan.tasks:
            unknown = set(task.dependency_ids) - known
            if unknown:
                raise SchedulerError(
                    f"task {task.node_id!r} has unknown dependencies "
                    f"{sorted(unknown)}"
                )
            self._handler_for(task)
            self._policy_for(task)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise SchedulerError(
                    f"compiled task dependencies contain a cycle at {node_id!r}"
                )
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in self._compiled_by_node[node_id].dependency_ids:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in sorted(known):
            visit(node_id)

    def _validate_durable_records(self, run: Mapping[str, Any]) -> None:
        for compiled_task in self.plan.tasks:
            durable_id = self._durable_ids[compiled_task.node_id]
            task = self.store.get_task(durable_id)
            if task["run_id"] != self.run_id:
                raise SchedulerError(
                    f"task {durable_id!r} belongs to another run"
                )
            if task["node_id"] != compiled_task.node_id:
                raise SchedulerError(
                    f"task {durable_id!r} does not match compiled node "
                    f"{compiled_task.node_id!r}"
                )
            if task["kind"] != "implement":
                raise SchedulerError(
                    f"compiled task {durable_id!r} must have kind 'implement'"
                )
            if task["status"] not in {
                "pending",
                "ready",
                "running",
                "verifying",
                *_TASK_TERMINAL,
            }:
                raise SchedulerError(
                    f"task {durable_id!r} has unknown status "
                    f"{task['status']!r}"
                )

    def _handler_for(self, task: CompiledTask) -> TaskHandler:
        for key in (task.node_id, task.task_id, "implement", "*"):
            handler = self.handlers.get(key)
            if handler is not None:
                if not callable(handler):
                    raise TypeError(f"handler {key!r} is not callable")
                return handler
        raise SchedulerError(f"no handler is registered for task {task.node_id!r}")

    def _policy_for(self, task: CompiledTask) -> TaskPolicy:
        for key in (task.node_id, task.task_id, "implement", "*"):
            policy = self.task_policies.get(key)
            if policy is not None:
                if not isinstance(policy, TaskPolicy):
                    raise TypeError(f"task policy {key!r} is not a TaskPolicy")
                return policy
        return self.default_policy

    def _states(self) -> dict[str, dict[str, Any]]:
        return {
            task.node_id: self.store.get_task(self._durable_ids[task.node_id])
            for task in self.plan.tasks
        }

    def _recover_interrupted(
        self, retry_not_before: dict[str, float]
    ) -> None:
        for task in sorted(self.plan.tasks, key=self._task_sort_key):
            durable_id = self._durable_ids[task.node_id]
            record = self.store.get_task(durable_id)
            if record["status"] not in {"running", "verifying"}:
                continue
            self._set_task_status(
                durable_id, "failed", expected_status=record["status"]
            )
            self._append_event(
                self.run_id,
                "task.interrupted",
                {
                    "attempt": record["attempt"],
                    "previous_status": record["status"],
                },
                task_id=durable_id,
            )
            self._record_evidence(
                task,
                record["attempt"],
                TaskEvidence(
                    kind="execution",
                    status="error",
                    summary="worker interrupted before durable completion",
                    details={"reason": "scheduler_restart"},
                ),
            )
            self._schedule_retry_if_available(
                task, record["attempt"], retry_not_before
            )
        # A crash can happen after an attempt is durably marked failed but
        # before the retry transition is committed. Resume that transition.
        for task in sorted(self.plan.tasks, key=self._task_sort_key):
            durable_id = self._durable_ids[task.node_id]
            record = self.store.get_task(durable_id)
            if record["status"] == "failed":
                self._schedule_retry_if_available(
                    task, int(record["attempt"]), retry_not_before
                )

        # If a dependent had already been blocked before a now-retryable
        # dependency was restored, make it eligible again.
        changed = True
        while changed:
            changed = False
            states = self._states()
            for task in sorted(self.plan.tasks, key=self._task_sort_key):
                record = states[task.node_id]
                if record["status"] != "blocked":
                    continue
                if all(
                    states[dependency]["status"] not in _DEPENDENCY_FAILURE
                    for dependency in task.dependency_ids
                ):
                    self._set_task_status(
                        record["id"], "ready", expected_status="blocked"
                    )
                    self._append_event(
                        self.run_id,
                        "task.unblocked",
                        {"reason": "dependency retry restored during resume"},
                        task_id=record["id"],
                    )
                    changed = True

    def _restore_retry_deadlines(
        self, retry_not_before: dict[str, float]
    ) -> None:
        latest: dict[str, float] = {}
        after_sequence = 0
        while True:
            events = self.store.list_events(
                self.run_id, after_sequence=after_sequence, limit=1000
            )
            if not events:
                break
            for event in events:
                if (
                    event["event_type"] == "task.retry_scheduled"
                    and event["task_id"] is not None
                ):
                    raw_deadline = event["payload"].get("not_before_epoch")
                    if isinstance(raw_deadline, (int, float)) and math.isfinite(
                        raw_deadline
                    ):
                        latest[event["task_id"]] = float(raw_deadline)
            after_sequence = int(events[-1]["sequence"])
            if len(events) < 1000:
                break
        wall_now = time.time()
        monotonic_now = time.monotonic()
        for task_id, wall_deadline in latest.items():
            if self.store.get_task(task_id)["status"] != "ready":
                continue
            retry_not_before[task_id] = monotonic_now + max(
                0.0, wall_deadline - wall_now
            )

    def _ready_tasks(
        self,
        states: Mapping[str, Mapping[str, Any]],
        retry_not_before: Mapping[str, float],
        now: float,
    ) -> list[CompiledTask]:
        ready: list[CompiledTask] = []
        for task in sorted(self.plan.tasks, key=self._task_sort_key):
            record = states[task.node_id]
            if record["status"] == "pending":
                dependencies = [
                    states[node_id]["status"]
                    for node_id in task.dependency_ids
                ]
                if all(status in _DEPENDENCY_SUCCESS for status in dependencies):
                    record = self._set_task_status(
                        record["id"], "ready", expected_status="pending"
                    )
                    self._append_event(
                        self.run_id,
                        "task.ready",
                        {"dependency_ids": list(task.dependency_ids)},
                        task_id=record["id"],
                    )
            if record["status"] != "ready":
                continue
            if now < retry_not_before.get(record["id"], 0.0):
                continue
            if all(
                states[node_id]["status"] in _DEPENDENCY_SUCCESS
                for node_id in task.dependency_ids
            ):
                ready.append(task)
        return ready

    @staticmethod
    def _task_sort_key(task: CompiledTask) -> tuple[int, str, str]:
        return task.order, task.task_id, task.node_id

    def _launch(
        self, task: CompiledTask, token: CancellationToken
    ) -> _RunningAttempt:
        durable_id = self._durable_ids[task.node_id]
        record = self._set_task_status(
            durable_id,
            "running",
            expected_status="ready",
            increment_attempt=True,
        )
        attempt = int(record["attempt"])
        policy = self._policy_for(task)
        started_at = time.monotonic()
        deadline = (
            None
            if policy.timeout_seconds is None
            else started_at + policy.timeout_seconds
        )
        cancellation = threading.Event()
        context = TaskContext(
            run_id=self.run_id,
            task_id=durable_id,
            node_id=task.node_id,
            attempt=attempt,
            compiled_task=task,
            _run_cancellation=token,
            _attempt_cancellation=cancellation,
            deadline_monotonic=deadline,
        )
        self._append_event(
            self.run_id,
            "task.started",
            {
                "attempt": attempt,
                "node_id": task.node_id,
                "timeout_seconds": policy.timeout_seconds,
            },
            task_id=durable_id,
        )
        thread = threading.Thread(
            target=self._invoke_handler,
            args=(self._handler_for(task), context),
            name=f"rich-{task.node_id}-{attempt}",
            daemon=True,
        )
        thread.start()
        return _RunningAttempt(
            compiled_task=task,
            durable_task_id=durable_id,
            attempt=attempt,
            started_at=started_at,
            deadline=deadline,
            attempt_cancellation=cancellation,
        )

    def _invoke_handler(
        self, handler: TaskHandler, context: TaskContext
    ) -> None:
        try:
            result = handler(context)
            if result is None:
                result = TaskResult()
            if not isinstance(result, TaskResult):
                raise TypeError("task handler must return TaskResult or None")
            envelope = _WorkerResult(
                task_id=context.task_id,
                attempt=context.attempt,
                result=result,
            )
        except BaseException as exc:  # worker must always report terminal output
            envelope = _WorkerResult(
                task_id=context.task_id,
                attempt=context.attempt,
                error_type=type(exc).__name__,
                error_message=str(exc)[:2000],
            )
        self._results.put(envelope)

    def _drain_results(
        self,
        running: dict[str, _RunningAttempt],
        worker_keys: set[tuple[str, int]],
        abandoned_workers: dict[tuple[str, int], float],
        retry_not_before: dict[str, float],
    ) -> None:
        while True:
            try:
                envelope = self._results.get_nowait()
            except queue.Empty:
                return
            self._consume_result(
                envelope,
                running,
                worker_keys,
                abandoned_workers,
                retry_not_before,
            )

    def _consume_result(
        self,
        envelope: _WorkerResult,
        running: dict[str, _RunningAttempt],
        worker_keys: set[tuple[str, int]],
        abandoned_workers: dict[tuple[str, int], float],
        retry_not_before: dict[str, float],
    ) -> None:
        key = (envelope.task_id, envelope.attempt)
        worker_keys.discard(key)
        abandoned_workers.pop(key, None)
        attempt = running.get(envelope.task_id)
        if attempt is None or attempt.attempt != envelope.attempt:
            self._append_event(
                self.run_id,
                "task.result_ignored",
                {
                    "attempt": envelope.attempt,
                    "reason": "attempt_already_timed_out_or_canceled",
                },
                task_id=envelope.task_id,
            )
            return
        running.pop(envelope.task_id, None)

        if envelope.error_type is not None:
            self._fail_attempt(
                attempt,
                status="error",
                summary=f"handler raised {envelope.error_type}",
                details={
                    "error_type": envelope.error_type,
                    "message": envelope.error_message or "",
                },
                retry_not_before=retry_not_before,
            )
            return

        result = envelope.result
        if result is None:
            raise RuntimeError("worker result is unexpectedly empty")
        try:
            committed_roles: list[str] = []
            for artifact_payload in result.artifacts:
                artifact = self.store.put_artifact(
                    artifact_payload.content,
                    media_type=artifact_payload.media_type,
                    metadata={
                        **dict(artifact_payload.metadata),
                        "run_id": self.run_id,
                        "task_id": attempt.durable_task_id,
                        "attempt": attempt.attempt,
                    },
                )
                # A task cannot claim a durable output merely because the
                # metadata row was written. Reading it back re-hashes the CAS
                # object and fails closed on missing or modified bytes.
                artifact = self.store.get_artifact(artifact.digest)
                self._attach_artifact(
                    self.run_id,
                    artifact.digest,
                    role=artifact_payload.role,
                    task_id=attempt.durable_task_id,
                )
                self._append_event(
                    self.run_id,
                    "artifact.produced",
                    {
                        "attempt": attempt.attempt,
                        "digest": artifact.digest,
                        "media_type": artifact.media_type,
                        "role": artifact_payload.role,
                        "size": artifact.size,
                    },
                    task_id=attempt.durable_task_id,
                )
                committed_roles.append(artifact_payload.role)
            for ordinal, evidence in enumerate(result.evidence, start=1):
                self._record_evidence(
                    attempt.compiled_task,
                    attempt.attempt,
                    evidence,
                    ordinal=ordinal,
                )
        except Exception as exc:
            self._fail_attempt(
                attempt,
                status="error",
                summary="handler output could not be committed",
                details={
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:2000],
                },
                retry_not_before=retry_not_before,
            )
            return

        policy = self._policy_for(attempt.compiled_task)
        unsatisfied = next(
            (
                evidence
                for evidence in result.evidence
                if evidence.blocking and evidence.status != "passed"
            ),
            None,
        )
        if unsatisfied is not None:
            self._fail_attempt(
                attempt,
                status=(
                    "error" if unsatisfied.status == "error" else "failed"
                ),
                summary=(
                    f"blocking {unsatisfied.kind} evidence was "
                    f"{unsatisfied.status}: {unsatisfied.summary}"
                ),
                details={"source": "blocking_handler_evidence"},
                retry_not_before=retry_not_before,
            )
            return

        if not result.succeeded:
            self._fail_attempt(
                attempt,
                status="failed",
                summary=result.summary,
                details={"source": "handler_result"},
                retry_not_before=retry_not_before,
            )
            return

        passed_blocking = tuple(
            evidence
            for evidence in result.evidence
            if evidence.blocking and evidence.status == EvidenceStatus.PASSED.value
        )
        if not passed_blocking:
            self._fail_attempt(
                attempt,
                status="failed",
                summary=(
                    "handler claimed success without explicit passed blocking "
                    "evidence"
                ),
                details={"source": "missing_blocking_evidence"},
                retry_not_before=retry_not_before,
            )
            return

        passed_kinds = {evidence.kind for evidence in passed_blocking}
        missing_kinds = sorted(
            set(policy.required_evidence_kinds) - passed_kinds
        )
        if missing_kinds:
            self._fail_attempt(
                attempt,
                status="failed",
                summary=f"required blocking evidence is missing: {missing_kinds}",
                details={
                    "source": "missing_required_evidence",
                    "missing_kinds": missing_kinds,
                },
                retry_not_before=retry_not_before,
            )
            return

        missing_roles = sorted(
            required
            for required in policy.required_artifact_roles
            if not any(
                self._artifact_role_matches(required, actual)
                for actual in committed_roles
            )
        )
        if missing_roles:
            self._fail_attempt(
                attempt,
                status="failed",
                summary=f"required durable artifacts are missing: {missing_roles}",
                details={
                    "source": "missing_required_artifact",
                    "missing_roles": missing_roles,
                },
                retry_not_before=retry_not_before,
            )
            return

        covered_scenarios = {
            scenario_id
            for evidence in passed_blocking
            if evidence.kind == EvidenceKind.ACCEPTANCE.value
            for scenario_id in evidence.acceptance_scenario_ids
        }
        missing_scenarios = sorted(
            set(policy.required_acceptance_scenario_ids) - covered_scenarios
        )
        if missing_scenarios:
            self._fail_attempt(
                attempt,
                status="failed",
                summary=(
                    "required acceptance scenarios are not covered: "
                    f"{missing_scenarios}"
                ),
                details={
                    "source": "missing_acceptance_traceability",
                    "missing_scenario_ids": missing_scenarios,
                },
                retry_not_before=retry_not_before,
            )
            return

        self._set_task_status(
            attempt.durable_task_id,
            "verifying",
            expected_status="running",
        )
        self._record_evidence(
            attempt.compiled_task,
            attempt.attempt,
            TaskEvidence(
                kind="execution",
                status="passed",
                summary=result.summary,
                blocking=False,
                details={"duration_seconds": time.monotonic() - attempt.started_at},
            ),
            ordinal=len(result.evidence) + 1,
        )
        self._set_task_status(
            attempt.durable_task_id,
            "succeeded",
            expected_status="verifying",
        )
        self._append_event(
            self.run_id,
            "task.succeeded",
            {
                "attempt": attempt.attempt,
                "duration_seconds": time.monotonic() - attempt.started_at,
            },
            task_id=attempt.durable_task_id,
        )

    @staticmethod
    def _artifact_role_matches(required: str, actual: str) -> bool:
        if required == actual:
            return True
        if required == "source":
            return actual in _SOURCE_ROLE_ALIASES or actual.startswith("source:")
        return actual.startswith(f"{required}:")

    def _expire_attempts(
        self,
        now: float,
        running: dict[str, _RunningAttempt],
        abandoned_workers: dict[tuple[str, int], float],
        retry_not_before: dict[str, float],
    ) -> None:
        expired = sorted(
            (
                attempt
                for attempt in running.values()
                if attempt.deadline is not None and now >= attempt.deadline
            ),
            key=lambda item: self._task_sort_key(item.compiled_task),
        )
        for attempt in expired:
            running.pop(attempt.durable_task_id, None)
            attempt.attempt_cancellation.set()
            key = (attempt.durable_task_id, attempt.attempt)
            abandoned_workers[key] = now + self.cancellation_grace_seconds
            policy = self._policy_for(attempt.compiled_task)
            self._fail_attempt(
                attempt,
                status="error",
                summary="handler exceeded its execution deadline",
                details={"timeout_seconds": policy.timeout_seconds},
                retry_not_before=retry_not_before,
            )

    def _fail_attempt(
        self,
        attempt: _RunningAttempt,
        *,
        status: str,
        summary: str,
        details: Mapping[str, Any],
        retry_not_before: dict[str, float],
    ) -> None:
        self._record_evidence(
            attempt.compiled_task,
            attempt.attempt,
            TaskEvidence(
                kind="execution",
                status=status,
                summary=summary,
                details=details,
            ),
        )
        try:
            self._set_task_status(
                attempt.durable_task_id,
                "failed",
                expected_status="running",
            )
        except RevisionConflict:
            # A cancellation can win a race with a worker result.
            current = self.store.get_task(attempt.durable_task_id)
            if current["status"] == "canceled":
                return
            raise
        self._append_event(
            self.run_id,
            "task.failed",
            {
                "attempt": attempt.attempt,
                "summary": summary,
                "will_retry": (
                    attempt.attempt
                    < self._policy_for(attempt.compiled_task).max_attempts
                ),
            },
            task_id=attempt.durable_task_id,
        )
        self._schedule_retry_if_available(
            attempt.compiled_task,
            attempt.attempt,
            retry_not_before,
        )

    def _schedule_retry_if_available(
        self,
        task: CompiledTask,
        attempt: int,
        retry_not_before: dict[str, float],
    ) -> None:
        policy = self._policy_for(task)
        if attempt >= policy.max_attempts:
            return
        durable_id = self._durable_ids[task.node_id]
        self._set_task_status(
            durable_id, "ready", expected_status="failed"
        )
        retry_not_before[durable_id] = (
            time.monotonic() + policy.retry_backoff_seconds
        )
        not_before_epoch = time.time() + policy.retry_backoff_seconds
        self._append_event(
            self.run_id,
            "task.retry_scheduled",
            {
                "completed_attempt": attempt,
                "next_attempt": attempt + 1,
                "backoff_seconds": policy.retry_backoff_seconds,
                "not_before_epoch": not_before_epoch,
            },
            task_id=durable_id,
        )

    def _record_evidence(
        self,
        task: CompiledTask,
        attempt: int,
        evidence: TaskEvidence,
        *,
        ordinal: int = 0,
    ) -> str:
        durable_id = self._durable_ids[task.node_id]
        requirement_ids = evidence.requirement_ids or task.requirement_ids
        result_document = {
            "schema": "rich.evidence-result/v1",
            "run_id": self.run_id,
            "task_id": durable_id,
            "node_id": task.node_id,
            "attempt": attempt,
            "ordinal": ordinal,
            "kind": evidence.kind,
            "status": evidence.status,
            "summary": evidence.summary,
            "blocking": evidence.blocking,
            "requirement_ids": list(requirement_ids),
            "acceptance_scenario_ids": list(
                evidence.acceptance_scenario_ids
            ),
            "details": dict(evidence.details),
        }
        result_artifact = self.store.put_artifact(
            _canonical_json(result_document),
            media_type="application/vnd.rich.evidence-result+json",
            metadata={
                "run_id": self.run_id,
                "task_id": durable_id,
                "attempt": attempt,
                "ordinal": ordinal,
                "kind": evidence.kind,
                "status": evidence.status,
                "blocking": evidence.blocking,
            },
        )
        result_artifact = self.store.get_artifact(result_artifact.digest)
        self._attach_artifact(
            self.run_id,
            result_artifact.digest,
            role=f"evidence-result:{evidence.kind}",
            task_id=durable_id,
        )

        record = Evidence(
            id=f"evidence.{result_artifact.digest}",
            run_id=self.run_id,
            task_id=durable_id,
            node_id=task.node_id,
            kind=evidence.kind,
            status=evidence.status,
            blocking=evidence.blocking,
            requirement_ids=tuple(requirement_ids),
            acceptance_scenario_ids=evidence.acceptance_scenario_ids,
            artifact_ids=(result_artifact.digest,),
            metadata={
                "attempt": attempt,
                "ordinal": ordinal,
                "summary": evidence.summary,
                "details": dict(evidence.details),
            },
        )
        record_artifact = self.store.put_artifact(
            _canonical_json(record.to_dict()),
            media_type="application/vnd.rich.evidence+json",
            metadata={
                "run_id": self.run_id,
                "task_id": durable_id,
                "attempt": attempt,
                "ordinal": ordinal,
                "evidence_id": record.id,
                "kind": evidence.kind,
                "status": evidence.status,
                "blocking": evidence.blocking,
            },
        )
        record_artifact = self.store.get_artifact(record_artifact.digest)
        self._attach_artifact(
            self.run_id,
            record_artifact.digest,
            role=f"evidence:{evidence.kind}",
            task_id=durable_id,
        )
        self._append_event(
            self.run_id,
            "evidence.recorded",
            {
                "attempt": attempt,
                "blocking": evidence.blocking,
                "digest": record_artifact.digest,
                "evidence_id": record.id,
                "kind": evidence.kind,
                "result_digest": result_artifact.digest,
                "status": evidence.status,
                "summary": evidence.summary,
            },
            task_id=durable_id,
        )
        return record_artifact.digest

    def _block_failed_dependencies(self) -> None:
        changed = True
        while changed:
            changed = False
            states = self._states()
            for task in sorted(self.plan.tasks, key=self._task_sort_key):
                record = states[task.node_id]
                if record["status"] not in {"pending", "ready"}:
                    continue
                failed = sorted(
                    dependency
                    for dependency in task.dependency_ids
                    if states[dependency]["status"] in _DEPENDENCY_FAILURE
                )
                if not failed:
                    continue
                self._set_task_status(
                    record["id"],
                    "blocked",
                    expected_status=record["status"],
                )
                self._append_event(
                    self.run_id,
                    "task.blocked",
                    {"failed_dependency_ids": failed},
                    task_id=record["id"],
                )
                changed = True

    def _cancel_remaining(self, reason: str) -> None:
        for task in sorted(self.plan.tasks, key=self._task_sort_key):
            record = self.store.get_task(self._durable_ids[task.node_id])
            if record["status"] in _TASK_TERMINAL:
                continue
            self._set_task_status(
                record["id"],
                "canceled",
                expected_status=record["status"],
            )
            self._append_event(
                self.run_id,
                "task.canceled",
                {"attempt": record["attempt"], "reason": reason},
                task_id=record["id"],
            )

    def _fail_uncooperative_workers(
        self,
        abandoned: Mapping[tuple[str, int], float],
        running: Mapping[str, _RunningAttempt],
    ) -> None:
        worker_ids = [
            {"task_id": task_id, "attempt": attempt}
            for task_id, attempt in sorted(abandoned)
        ]
        self._append_event(
            self.run_id,
            "scheduler.uncooperative_handlers",
            {"workers": worker_ids},
        )
        for attempt in running.values():
            attempt.attempt_cancellation.set()
        for task in sorted(self.plan.tasks, key=self._task_sort_key):
            record = self.store.get_task(self._durable_ids[task.node_id])
            if record["status"] in _TASK_TERMINAL:
                continue
            self._set_task_status(
                record["id"],
                "blocked",
                expected_status=record["status"],
            )
            self._append_event(
                self.run_id,
                "task.blocked",
                {"reason": "worker did not stop after cancellation"},
                task_id=record["id"],
            )

    def _block_stranded_tasks(self) -> None:
        for task in sorted(self.plan.tasks, key=self._task_sort_key):
            record = self.store.get_task(self._durable_ids[task.node_id])
            if record["status"] not in {"pending", "ready"}:
                continue
            self._set_task_status(
                record["id"],
                "blocked",
                expected_status=record["status"],
            )
            self._append_event(
                self.run_id,
                "task.blocked",
                {"reason": "no runnable path remains"},
                task_id=record["id"],
            )

    def _finish(self, status: str) -> SchedulerReport:
        run = self.store.get_run(self.run_id)
        if run["status"] in _RUN_TERMINAL:
            return self._report()
        if status == "succeeded":
            if run["status"] == "running":
                self._set_run_status(
                    self.run_id, "verifying", expected_status="running"
                )
                self._append_event(
                    self.run_id,
                    "run.verifying",
                    {"task_count": len(self.plan.tasks)},
                )
            try:
                self._validate_release_candidate()
            except Exception as exc:
                self._append_event(
                    self.run_id,
                    "run.release_validation_failed",
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:2000],
                    },
                )
                self._set_run_status(
                    self.run_id, "failed", expected_status="verifying"
                )
                status = "failed"
            else:
                self._set_run_status(
                    self.run_id, "succeeded", expected_status="verifying"
                )
        else:
            self._set_run_status(
                self.run_id, status, expected_status=run["status"]
            )
        report = self._report()
        self._append_event(
            self.run_id,
            f"run.{status}",
            {
                "task_attempts": dict(report.task_attempts),
                "task_statuses": dict(report.task_statuses),
            },
        )
        self._append_event(
            self.run_id,
            "scheduler.completed",
            {"status": status},
        )
        return report

    def _validate_release_candidate(self) -> None:
        durable_run = self.store.get_run(self.run_id)
        spec_revision_id = durable_run.get("spec_revision_id")
        architecture_revision_id = durable_run.get("architecture_revision_id")
        if not isinstance(spec_revision_id, str) or not spec_revision_id:
            raise ModelValidationError(
                "release validation requires an immutable project spec revision"
            )
        if (
            not isinstance(architecture_revision_id, str)
            or not architecture_revision_id
        ):
            raise ModelValidationError(
                "release validation requires an immutable architecture revision"
            )

        project = ProjectSpec.from_dict(
            self.store.get_revision(spec_revision_id).document
        )
        architecture = ArchitectureSpec.from_dict(
            self.store.get_revision(architecture_revision_id).document
        )
        if project.revision != self.plan.project_revision:
            raise ModelValidationError(
                "compiled plan project revision does not match the release spec"
            )
        if (
            architecture.id != self.plan.architecture_id
            or architecture.revision != self.plan.architecture_revision
        ):
            raise ModelValidationError(
                "compiled plan does not match the release architecture revision"
            )
        if architecture.target_pack != self.plan.target_pack:
            raise ModelValidationError(
                "compiled plan target pack does not match the release architecture"
            )

        tasks = self._release_tasks()
        task_index = {task.id: task for task in tasks}
        evidence, artifacts = self._release_evidence_and_artifacts(task_index)
        candidate = BuildRun(
            id=self.run_id,
            project_id=durable_run["project_id"],
            spec_revision_id=spec_revision_id,
            architecture_revision_id=architecture_revision_id,
            status=RunStatus.SUCCEEDED,
            task_ids=tuple(task.id for task in tasks),
            evidence_ids=tuple(record.id for record in evidence),
            artifact_ids=tuple(artifact.id for artifact in artifacts),
            metadata={"scheduler": "rich.dag/v2"},
        )
        validate_release_traceability(
            project=project,
            architecture=architecture,
            run=candidate,
            tasks=tasks,
            evidence=evidence,
            artifacts=artifacts,
        )

    def _release_tasks(self) -> tuple[BuildTask, ...]:
        records: list[BuildTask] = []
        for compiled in sorted(self.plan.tasks, key=self._task_sort_key):
            durable = self.store.get_task(self._durable_ids[compiled.node_id])
            records.append(
                BuildTask(
                    id=durable["id"],
                    run_id=self.run_id,
                    node_id=compiled.node_id,
                    kind=TaskKind.IMPLEMENT,
                    status=TaskStatus(durable["status"]),
                    dependency_task_ids=tuple(
                        self._durable_ids[node_id]
                        for node_id in compiled.dependency_ids
                    ),
                    attempt=int(durable["attempt"]),
                    cache_key=durable.get("cache_key"),
                )
            )
        return tuple(records)

    def _release_evidence_and_artifacts(
        self, task_index: Mapping[str, BuildTask]
    ) -> tuple[tuple[Evidence, ...], tuple[DomainArtifact, ...]]:
        attachments = self.store.list_run_artifacts(self.run_id)
        evidence_by_id: dict[str, Evidence] = {}
        artifacts_by_id: dict[str, DomainArtifact] = {}

        for attachment in attachments:
            role = str(attachment["role"])
            stored = self.store.get_artifact(str(attachment["digest"]))
            if role.startswith("evidence:"):
                document = json.loads(stored.path.read_text(encoding="utf-8"))
                record = Evidence.from_dict(document)
                task = (
                    task_index.get(record.task_id)
                    if record.task_id is not None
                    else None
                )
                if task is None:
                    raise ModelValidationError(
                        f"evidence {record.id!r} has no release task"
                    )
                if record.metadata.get("attempt") != task.attempt:
                    # Failed evidence from an earlier retry remains auditable,
                    # but it is not part of the final-attempt release proof.
                    continue
                if record.id in evidence_by_id:
                    raise ModelValidationError(
                        f"release contains duplicate evidence id {record.id!r}"
                    )
                evidence_by_id[record.id] = record
                continue

            task_id = attachment.get("task_id")
            task = task_index.get(task_id) if task_id is not None else None
            requirement_ids = (
                self._compiled_by_node[task.node_id].requirement_ids
                if task is not None
                else ()
            )
            kind = self._artifact_kind_for_role(role)
            artifact = DomainArtifact(
                id=stored.digest,
                run_id=self.run_id,
                kind=kind,
                status=ArtifactStatus.AVAILABLE,
                digest=stored.digest,
                uri=f"cas://sha256/{stored.digest}",
                media_type=stored.media_type,
                produced_by_task_id=task_id,
                requirement_ids=tuple(requirement_ids),
                metadata={**dict(stored.metadata), "role": role},
            )
            previous = artifacts_by_id.get(artifact.id)
            if previous is not None and previous != artifact:
                raise ModelValidationError(
                    f"artifact {artifact.id!r} has conflicting release bindings"
                )
            artifacts_by_id[artifact.id] = artifact

        return (
            tuple(evidence_by_id[key] for key in sorted(evidence_by_id)),
            tuple(artifacts_by_id[key] for key in sorted(artifacts_by_id)),
        )

    @staticmethod
    def _artifact_kind_for_role(role: str) -> ArtifactKind:
        if (
            role in _SOURCE_ROLE_ALIASES
            or role.startswith("source:")
            or role.startswith("generated-source:")
        ):
            return ArtifactKind.SOURCE
        if role == "bundle" or role.startswith("bundle:"):
            return ArtifactKind.BUNDLE
        if role == "test" or role.startswith("test:"):
            return ArtifactKind.TEST
        if role == "log" or role.startswith("log:"):
            return ArtifactKind.LOG
        if role == "preview" or role.startswith("preview:"):
            return ArtifactKind.PREVIEW
        return ArtifactKind.REPORT

    def _report(self) -> SchedulerReport:
        run = self.store.get_run(self.run_id)
        records = [
            self.store.get_task(self._durable_ids[task.node_id])
            for task in sorted(self.plan.tasks, key=self._task_sort_key)
        ]
        return SchedulerReport(
            run_id=self.run_id,
            status=run["status"],
            task_statuses=tuple(
                (record["node_id"], record["status"]) for record in records
            ),
            task_attempts=tuple(
                (record["node_id"], int(record["attempt"]))
                for record in records
            ),
        )
