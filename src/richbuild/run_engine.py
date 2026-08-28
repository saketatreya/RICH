"""Verified public-build execution for approved RICH runs.

The execution boundary in this module is intentionally narrow:

* immutable project and architecture revisions must match the approval used to
  prepare the durable run;
* source generation is delegated to :class:`CodingWorker`, whose model output
  is never treated as verification;
* static, unit, and acceptance evidence is derived only from a separately
  injected command runner; and
* :class:`DagScheduler` remains the sole authority that can publish durable
  task and run success.

The command-runner protocol makes hermetic tests straightforward while the
``BubblewrapCommandRunner`` adapter provides the fail-closed production path.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Protocol

from .coding import (
    ApprovalWitness,
    CodingLimits,
    CodingWorker,
    DEFAULT_LIMITS,
    GeneratedFileBundle,
    PriorAttemptFailure,
    SourceCommit,
    SourceTransactionJournal,
    SourceTransactionSink,
    generated_source_artifact_bytes,
    is_protected_generation_path,
    redact_diagnostics,
    source_transaction_lock,
)
from .canonical import canonical_json_bytes
from .paths import UnsafePath, is_owned, safe_relative_path
from .budget import RunBudget
from .compiler import CompiledArchitecture, CompiledTask, compile_architecture
from .executor import (
    BubblewrapExecutor,
    ExecutionResult,
    SandboxPolicy,
)
from .models import ArchitectureSpec, EvidenceKind, ProjectSpec
from .providers import ModelGateway
from .preview import collect_deployment_files, create_deployment_snapshot
from .scheduler import (
    CancellationToken,
    DagScheduler,
    ProducedArtifact,
    SchedulerReport,
    TaskContext,
    TaskEvidence,
    TaskHandler,
    TaskPolicy,
    TaskResult,
)
from .store import NotFoundError, RichStore, StoreError


class RunEngineError(RuntimeError):
    """A durable run cannot be executed without weakening its proof chain."""


class ApprovalValidationError(RunEngineError, PermissionError):
    """The build approval does not cover the run's exact immutable inputs."""


class WorkspaceValidationError(RunEngineError, ValueError):
    """The requested workspace is not the recorded, protected scaffold."""


class ConcurrentExecutionError(RunEngineError):
    """Another fenced owner is already executing this durable run."""


_ACCEPTANCE_COVERAGE_PREFIX = "RICH_ACCEPTANCE_COVERAGE "
_NONCE_RE = re.compile(r"^[a-f0-9]{64}$")
DEFAULT_EXECUTION_LEASE_SECONDS = 60.0
DEFAULT_EXECUTION_HEARTBEAT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class AcceptanceCoverageContext:
    """Attempt-bound identity shared with the protected Playwright reporter."""

    run_id: str
    task_id: str
    attempt: int
    nonce: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, str)
            or not self.run_id.strip()
            or not isinstance(self.task_id, str)
            or not self.task_id.strip()
            or "\x00" in self.run_id
            or "\x00" in self.task_id
        ):
            raise ValueError("acceptance coverage run/task ids cannot be empty")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("acceptance coverage attempt must be positive")
        if not isinstance(self.nonce, str) or not _NONCE_RE.fullmatch(self.nonce):
            raise ValueError("acceptance coverage nonce must be 64 lowercase hex")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "nonce": self.nonce,
        }


def _observed_acceptance_coverage(
    result: ExecutionResult,
    *,
    expected_scenario_ids: Sequence[str],
    expected_context: AcceptanceCoverageContext,
) -> tuple[str, ...]:
    """Parse one attempt-bound report emitted by the protected reporter."""

    if not result.passed:
        raise RunEngineError(
            "a failed acceptance command cannot report passing coverage"
        )
    if "[RICH output truncated]" in result.stdout:
        raise RunEngineError(
            "acceptance output was truncated before coverage could be trusted"
        )
    reports = [
        line[len(_ACCEPTANCE_COVERAGE_PREFIX) :]
        for raw_line in result.stdout.splitlines()
        if (line := raw_line.strip()).startswith(_ACCEPTANCE_COVERAGE_PREFIX)
    ]
    if len(reports) != 1:
        raise RunEngineError(
            "acceptance command did not emit exactly one coverage report"
        )
    try:
        document = json.loads(reports[0])
    except json.JSONDecodeError as exc:
        raise RunEngineError(
            "acceptance coverage report is not valid JSON"
        ) from exc
    if (
        not isinstance(document, Mapping)
        or set(document) != {"schema_version", "context", "scenario_ids"}
        or document.get("schema_version") != "rich.acceptance-coverage/v1"
        or document.get("context") != expected_context.to_dict()
        or not isinstance(document.get("scenario_ids"), list)
    ):
        raise RunEngineError(
            "acceptance coverage report has an invalid schema or context"
        )
    scenario_ids = tuple(document["scenario_ids"])
    if (
        any(
            not isinstance(scenario_id, str) or not scenario_id.strip()
            for scenario_id in scenario_ids
        )
        or len(set(scenario_ids)) != len(scenario_ids)
        or tuple(sorted(scenario_ids)) != scenario_ids
    ):
        raise RunEngineError(
            "acceptance coverage report has invalid scenario identifiers"
        )
    if scenario_ids != tuple(sorted(expected_scenario_ids)):
        raise RunEngineError(
            "acceptance coverage does not exactly match the approved scenarios"
        )
    return scenario_ids


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """One independently executed verification gate."""

    kind: str
    argv: tuple[str, ...]
    expected_acceptance_scenario_ids: tuple[str, ...] = ()
    acceptance_context: AcceptanceCoverageContext | None = None

    def __post_init__(self) -> None:
        try:
            kind = EvidenceKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown verification kind {self.kind!r}") from exc
        if kind not in {
            EvidenceKind.STATIC,
            EvidenceKind.LINT,
            EvidenceKind.UNIT,
            EvidenceKind.PROPERTY,
            EvidenceKind.BUILD,
            EvidenceKind.ACCEPTANCE,
        }:
            raise ValueError(
                "verification commands must be lint, static, unit, build, "
                "or acceptance"
            )
        object.__setattr__(self, "kind", kind.value)
        if (
            isinstance(self.argv, (str, bytes))
            or not self.argv
            or any(
                not isinstance(value, str) or not value or "\x00" in value
                for value in self.argv
            )
        ):
            raise ValueError("verification argv must contain non-empty strings")
        object.__setattr__(self, "argv", tuple(self.argv))
        scenarios = tuple(self.expected_acceptance_scenario_ids)
        if any(
            not isinstance(value, str) or not value.strip() for value in scenarios
        ) or len(set(scenarios)) != len(scenarios):
            raise ValueError(
                "expected acceptance scenario ids must be unique non-empty strings"
            )
        if kind is EvidenceKind.ACCEPTANCE and not scenarios:
            raise ValueError("acceptance verification requires expected scenarios")
        if kind is not EvidenceKind.ACCEPTANCE and scenarios:
            raise ValueError(
                "only acceptance verification can claim scenario coverage"
            )
        object.__setattr__(
            self, "expected_acceptance_scenario_ids", scenarios
        )
        if kind is EvidenceKind.ACCEPTANCE:
            if not isinstance(
                self.acceptance_context, AcceptanceCoverageContext
            ):
                raise ValueError(
                    "acceptance verification requires an attempt-bound context"
                )
        elif self.acceptance_context is not None:
            raise ValueError(
                "only acceptance verification can carry a coverage context"
            )


class CommandRunner(Protocol):
    """Sandbox boundary used by the trusted verifier."""

    def run(
        self,
        workspace: Path,
        command: VerificationCommand,
        *,
        cancellation: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> ExecutionResult:
        """Run one command and return its observed process result."""


class WorkspacePreparer(Protocol):
    """Trusted dependency/tool bootstrap boundary run after source validation."""

    def bootstrap(
        self,
        workspace: Path,
        *,
        cancellation: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> object:
        """Prepare the exact workspace and return an object with ``passed``."""


@dataclass(frozen=True, slots=True)
class BubblewrapCommandRunner:
    """Run verification with no network and only explicit output directories."""

    executor: BubblewrapExecutor
    timeout_seconds: float = 300.0
    writable_paths: tuple[str, ...] = (
        "apps/web/.next",
        "apps/web/next-env.d.ts",
        "apps/web/test-results",
        "playwright-report",
        "test-results",
    )
    max_output_bytes: int = 1_048_576
    # RLIMIT_AS measures reserved virtual address space, not resident memory.
    # A single ARM64 Node child reserves an 8 GiB pointer-compression cage.
    # Leave finite address-space headroom for shared libraries and Next's trace
    # collector; NODE_OPTIONS below separately caps its V8 heap at 1.5 GiB.
    max_memory_bytes: int = 17_179_869_184
    # Chromium's PartitionAlloc cage reserves much more address space than it
    # commits. Acceptance is sequential and separately heap/process bounded.
    acceptance_max_address_space_bytes: int = 137_438_953_472
    max_processes: int = 128
    playwright_browsers_path: str = (
        "/workspace/.rich/runtime/playwright"
    )

    def __post_init__(self) -> None:
        if not isinstance(self.executor, BubblewrapExecutor):
            raise TypeError("executor must be a BubblewrapExecutor")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or self.max_output_bytes < 1
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        if (
            isinstance(self.max_memory_bytes, bool)
            or not isinstance(self.max_memory_bytes, int)
            or self.max_memory_bytes < 1
            or isinstance(self.acceptance_max_address_space_bytes, bool)
            or not isinstance(self.acceptance_max_address_space_bytes, int)
            or self.acceptance_max_address_space_bytes < 1
            or isinstance(self.max_processes, bool)
            or not isinstance(self.max_processes, int)
            or self.max_processes < 1
        ):
            raise ValueError(
                "verification resource limits must be positive integers"
            )
        if (
            not isinstance(self.playwright_browsers_path, str)
            or not self.playwright_browsers_path.startswith("/workspace/")
            or "\x00" in self.playwright_browsers_path
        ):
            raise ValueError(
                "Playwright browser path must be inside the workspace"
            )

    def run(
        self,
        workspace: Path,
        command: VerificationCommand,
        *,
        cancellation: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> ExecutionResult:
        context_path: Path | None = None
        environment = {
            "CI": "1",
            "NEXT_TELEMETRY_DISABLED": "1",
            "NODE_OPTIONS": (
                "--disable-wasm-trap-handler "
                "--max-old-space-size=1536"
            ),
            "RAYON_NUM_THREADS": "2",
            "PLAYWRIGHT_BROWSERS_PATH": self.playwright_browsers_path,
        }
        if command.acceptance_context is not None:
            root = Path(workspace).resolve(strict=True)
            context_directory = root / "test-results"
            if context_directory.is_symlink():
                raise RunEngineError(
                    "acceptance context directory cannot be a symlink"
                )
            context_directory.mkdir(parents=True, exist_ok=True)
            fd, raw_path = tempfile.mkstemp(
                prefix=".rich-acceptance-context-",
                suffix=".json",
                dir=context_directory,
            )
            context_path = Path(raw_path)
            try:
                payload = json.dumps(
                    command.acceptance_context.to_dict(),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                with os.fdopen(fd, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                context_path.unlink(missing_ok=True)
                raise
            environment["RICH_ACCEPTANCE_CONTEXT_FILE"] = (
                "/workspace/"
                + context_path.relative_to(root).as_posix()
            )
        try:
            return self.executor.run(
                workspace,
                command.argv,
                SandboxPolicy(
                    writable_paths=self.writable_paths,
                    network=False,
                    environment=environment,
                    timeout_seconds=self.timeout_seconds,
                    max_memory_bytes=(
                        self.acceptance_max_address_space_bytes
                        if command.kind == EvidenceKind.ACCEPTANCE.value
                        else self.max_memory_bytes
                    ),
                    max_processes=self.max_processes,
                    max_cpu_seconds=max(1, int(self.timeout_seconds)),
                    max_output_bytes=self.max_output_bytes,
                ),
                cancellation=cancellation,
                deadline=deadline,
            )
        finally:
            if context_path is not None:
                context_path.unlink(missing_ok=True)

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Wait until every process group launched by this runner is reaped."""

        return self.executor.wait_for_idle(timeout)


@dataclass(frozen=True, slots=True)
class RunEngineConfig:
    """Bounded model, scheduler, and verification policy."""

    max_workers: int = 1
    max_task_attempts: int = 2
    max_provider_attempts: int = 1
    task_timeout_seconds: float = 600.0
    retry_backoff_seconds: float = 0.0
    lint_argv: tuple[str, ...] = ("pnpm", "run", "lint")
    static_argv: tuple[str, ...] = ("pnpm", "run", "typecheck")
    unit_argv: tuple[str, ...] = ("pnpm", "run", "test")
    # vitest directly, not `pnpm run test:properties`: the script names the
    # whole directory, and appending one file to it adds a second filter rather
    # than narrowing to the first. A node is gated on its own suite, so the
    # suite path has to be the only path.
    property_argv: tuple[str, ...] = (
        "pnpm",
        "exec",
        "vitest",
        "run",
        "--configLoader",
        "runner",
    )
    build_argv: tuple[str, ...] = ("pnpm", "run", "build")
    acceptance_argv: tuple[str, ...] = ("pnpm", "run", "test:e2e")
    max_verification_log_bytes: int = 256 * 1024
    coding_limits: CodingLimits = DEFAULT_LIMITS
    execution_lease_seconds: float = DEFAULT_EXECUTION_LEASE_SECONDS
    execution_heartbeat_seconds: float = (
        DEFAULT_EXECUTION_HEARTBEAT_SECONDS
    )

    def __post_init__(self) -> None:
        for name in (
            "max_workers",
            "max_task_attempts",
            "max_provider_attempts",
            "max_verification_log_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "task_timeout_seconds",
            "retry_backoff_seconds",
            "execution_lease_seconds",
            "execution_heartbeat_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (
                    name != "retry_backoff_seconds"
                    and value == 0
                )
            ):
                raise ValueError(f"{name} is outside its allowed range")
        if self.execution_heartbeat_seconds * 2 >= self.execution_lease_seconds:
            raise ValueError(
                "execution heartbeat must be less than half the lease duration"
            )
        if self.max_workers != 1:
            raise ValueError(
                "live-workspace execution requires max_workers=1 until "
                "task-isolated worktrees are available"
            )
        for name in (
            "lint_argv",
            "static_argv",
            "unit_argv",
            "property_argv",
            "build_argv",
            "acceptance_argv",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, (str, bytes))
                or not value
                or any(
                    not isinstance(item, str) or not item or "\x00" in item
                    for item in value
                )
            ):
                raise ValueError(f"{name} must contain non-empty argv strings")
            object.__setattr__(self, name, tuple(value))
        if not isinstance(self.coding_limits, CodingLimits):
            raise TypeError("coding_limits must be CodingLimits")


class ModelWorkerFactory(Protocol):
    """Injection seam for a task-scoped model worker."""

    def __call__(
        self,
        gateway: ModelGateway,
        *,
        workspace: Path,
        project: ProjectSpec,
        architecture: ArchitectureSpec,
        approval: ApprovalWitness,
        provider: str,
        model: str,
        limits: CodingLimits,
        max_attempts: int,
        mutation_guard: Callable[[], bool],
        transaction_sink: SourceTransactionSink,
    ) -> TaskHandler:
        """Create a handler for the exact approved build inputs."""


@dataclass(frozen=True, slots=True)
class _DurableSourceTransactionSink:
    """Lease-fenced CAS/write-ahead coordinator for model source changes."""

    store: RichStore
    run_id: str
    owner_token: str

    def _validate_run(self, run_id: str) -> None:
        if run_id != self.run_id:
            raise RunEngineError(
                "source transaction sink received a different durable run"
            )

    def prepare(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        bundle: GeneratedFileBundle,
        journal: SourceTransactionJournal,
    ) -> None:
        self._validate_run(run_id)
        generated = self.store.put_artifact(
            generated_source_artifact_bytes(bundle, journal),
            media_type="application/vnd.rich.generated-source+json",
            metadata={
                "run_id": run_id,
                "task_id": task_id,
                "attempt": attempt,
                "source_digest": journal.source_digest,
                "file_count": len(bundle.files),
                "total_bytes": bundle.total_bytes,
                "verification_status": "not_run",
                "acceptance_status": "not_evaluated",
            },
        )
        durable_journal = self.store.put_artifact(
            journal.artifact_bytes(
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                generated_artifact_digest=generated.digest,
            ),
            media_type=(
                "application/vnd.rich.source-transaction-journal+json"
            ),
            metadata={
                "run_id": run_id,
                "task_id": task_id,
                "attempt": attempt,
                "source_digest": journal.source_digest,
                "generated_digest": generated.digest,
            },
        )
        self.store.prepare_source_transaction(
            run_id,
            task_id=task_id,
            attempt=attempt,
            owner_token=self.owner_token,
            journal_digest=durable_journal.digest,
            generated_digest=generated.digest,
        )

    def commit(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        journal: SourceTransactionJournal,
        commit: SourceCommit,
    ) -> None:
        self._validate_run(run_id)
        if (
            commit.source_digest != journal.source_digest
            or commit.paths != journal.paths
        ):
            raise RunEngineError(
                "applied source does not match its durable transaction journal"
            )
        try:
            self.store.commit_source_transaction(
                run_id,
                task_id=task_id,
                attempt=attempt,
                owner_token=self.owner_token,
            )
        except Exception:
            # Resolve only a definitely-uncommitted transaction. If status
            # cannot be read, or ownership moved, leave the journal and bytes
            # untouched for the fenced successor instead of guessing.
            try:
                record = next(
                    item
                    for item in self.store.list_source_transactions(run_id)
                    if item["task_id"] == task_id
                    and item["attempt"] == attempt
                )
            except Exception:
                raise
            if record["status"] == "committed":
                return
            if (
                record["status"] == "prepared"
                and self.store.is_run_execution_owner(
                    run_id, self.owner_token
                )
            ):
                commit.rollback()
                self.store.rollback_source_transaction(
                    run_id,
                    task_id=task_id,
                    attempt=attempt,
                    owner_token=self.owner_token,
                    reason="durable_commit_failed",
                )
            raise

    def abort(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        journal: SourceTransactionJournal,
    ) -> None:
        self._validate_run(run_id)
        try:
            self.store.rollback_source_transaction(
                run_id,
                task_id=task_id,
                attempt=attempt,
                owner_token=self.owner_token,
                reason="worker_rollback",
            )
        except NotFoundError:
            # The prepare callback can fail before its atomic database insert.
            # No destination changed, so there is no durable record to close.
            return


class _DurableGenerationMemo:
    """Remember a generation across runs, keyed by the request that produced it.

    Only the model call is skipped. The bundle is revalidated by the same
    parser, applied through the same write-ahead transaction, and verified by
    the same independent gates -- reusing an answer is never reusing a verdict,
    because a verdict is bound to the run, task, attempt and nonce that
    observed it."""

    def __init__(self, store: RichStore) -> None:
        self.store = store

    def get(self, cache_key: str) -> dict[str, Any] | None:
        record = self.store.get_generation_memo(cache_key)
        if record is None:
            return None
        try:
            document = json.loads(record["payload"])
        except (ValueError, UnicodeError):
            return None
        if not isinstance(document, Mapping):
            return None
        bundle = document.get("bundle")
        if not isinstance(bundle, Mapping):
            return None
        return {
            "bundle": bundle,
            "provider": document.get("provider"),
            "model": document.get("model"),
            "origin_run_id": record["origin_run_id"],
            "origin_task_id": record["origin_task_id"],
        }

    def put(
        self,
        cache_key: str,
        *,
        document: Mapping[str, Any],
        project_id: str,
        node_id: str,
        provider: str,
        model: str,
        run_id: str,
        task_id: str,
    ) -> None:
        self.store.put_generation_memo(
            cache_key,
            payload=canonical_json_bytes(dict(document)),
            project_id=project_id,
            node_id=node_id,
            provider=provider,
            model=model,
            run_id=run_id,
            task_id=task_id,
        )


def _prior_failure_source(
    store: RichStore,
    run_id: str,
) -> Callable[[CompiledTask, int], tuple[PriorAttemptFailure, ...]]:
    """Read earlier attempts' gate failures for a task out of the artifact store.

    The verification artifacts already hold every gate's exact stdout/stderr;
    until now nothing read them back, so each retry regenerated blind against a
    failure the harness had watched happen and written down.  Only observed
    command results are returned -- a model's own claims are never a source
    here, and feeding these forward does not let one become evidence."""

    def read(task: CompiledTask, attempt: int) -> tuple[PriorAttemptFailure, ...]:
        failures: list[PriorAttemptFailure] = []
        for record in store.list_run_artifacts(run_id):
            role = record.get("role") or ""
            if not role.startswith("verification:"):
                continue
            metadata = record.get("metadata") or {}
            if metadata.get("node_id") != task.node_id:
                continue
            if metadata.get("status") == "passed":
                continue
            earlier = metadata.get("attempt")
            if (
                isinstance(earlier, bool)
                or not isinstance(earlier, int)
                or earlier >= attempt
            ):
                continue
            try:
                artifact = store.get_artifact(record["digest"])
                document = json.loads(artifact.path.read_bytes())
            except (StoreError, NotFoundError, ValueError, OSError):
                # A missing or unreadable log must not fail the retry it was
                # only meant to inform.
                continue
            observed = "\n".join(
                part
                for part in (document.get("stdout"), document.get("stderr"))
                if isinstance(part, str) and part.strip()
            )
            diagnostics, withheld = redact_diagnostics(
                observed, owned_paths=task.owned_paths
            )
            failures.append(
                PriorAttemptFailure(
                    attempt=earlier,
                    gate=str(document.get("kind") or metadata.get("kind") or role),
                    summary=(
                        f"{document.get('kind', 'gate')} exited with "
                        f"{document.get('returncode')}"
                    ),
                    diagnostics=diagnostics,
                    withheld_line_count=withheld,
                )
            )
        failures.sort(key=lambda item: (item.attempt, item.gate))
        return tuple(failures)

    return read


def _coding_worker_factory(
    gateway: ModelGateway,
    *,
    workspace: Path,
    project: ProjectSpec,
    architecture: ArchitectureSpec,
    approval: ApprovalWitness,
    provider: str,
    model: str,
    limits: CodingLimits,
    max_attempts: int,
    mutation_guard: Callable[[], bool],
    transaction_sink: SourceTransactionSink,
    prior_failures: Callable[..., Any] | None = None,
    memo: Any | None = None,
) -> TaskHandler:
    return CodingWorker(
        gateway,
        workspace=workspace,
        project=project,
        architecture=architecture,
        approval=approval,
        provider=provider,
        model=model,
        limits=limits,
        max_attempts=max_attempts,
        mutation_guard=mutation_guard,
        transaction_sink=transaction_sink,
        prior_failures=prior_failures,
        memo=memo,
    )


@dataclass(frozen=True, slots=True)
class _LoadedRun:
    project: ProjectSpec
    architecture: ArchitectureSpec
    plan: CompiledArchitecture
    approval: ApprovalWitness
    workspace: Path


class _VerifiedCodingHandler:
    """Compose model generation with independent process verification."""

    _FORBIDDEN_MODEL_EVIDENCE = frozenset(
        {
            EvidenceKind.STATIC.value,
            EvidenceKind.LINT.value,
            EvidenceKind.UNIT.value,
            EvidenceKind.BUILD.value,
            EvidenceKind.ACCEPTANCE.value,
        }
    )

    def __init__(
        self,
        model_worker: TaskHandler,
        *,
        command_runner: CommandRunner,
        workspace: Path,
        project: ProjectSpec,
        root_node_id: str,
        config: RunEngineConfig,
    ):
        self.model_worker = model_worker
        self.command_runner = command_runner
        self.workspace = workspace
        self.project = project
        self.root_node_id = root_node_id
        self.config = config
        self._properties = workspace / "tests" / "properties"

    def __call__(self, context: TaskContext) -> TaskResult:
        if context.is_cancelled:
            raise RunEngineError("task was canceled before generation")
        generated = self.model_worker(context)
        if context.is_cancelled:
            raise RunEngineError("task was canceled before verification")
        if generated is None:
            raise RunEngineError("model worker returned no generation result")
        if not isinstance(generated, TaskResult):
            raise TypeError("model worker must return TaskResult")
        if any(
            evidence.kind in self._FORBIDDEN_MODEL_EVIDENCE
            or evidence.blocking
            for evidence in generated.evidence
        ):
            raise RunEngineError(
                "model generation attempted to self-certify blocking verification"
            )
        if not generated.succeeded:
            return generated

        node_id = context.compiled_task.node_id
        is_root = node_id == self.root_node_id
        scenario_ids = tuple(sorted(self.project.scenario_index))
        release_inputs = (
            _deployment_file_digests(self.workspace) if is_root else None
        )
        commands = [
            VerificationCommand(EvidenceKind.STATIC.value, self.config.static_argv),
            VerificationCommand(EvidenceKind.UNIT.value, self.config.unit_argv),
        ]
        # The obligations an approved contract declares, executed. Without this
        # the architect emits claims that nothing ever checks, which reads like
        # assurance and is not.
        #
        # A node is gated on its own contract's suite and no other. Running the
        # whole directory would fail a node because a component built later has
        # not written its module yet -- judging one node by another's absence.
        suite = self._property_suite(context.compiled_task.contract_id)
        if suite is not None:
            commands.append(
                VerificationCommand(
                    EvidenceKind.PROPERTY.value,
                    (*self.config.property_argv, suite),
                )
            )
        if is_root:
            commands = [
                VerificationCommand(
                    EvidenceKind.LINT.value, self.config.lint_argv
                ),
                *commands,
                VerificationCommand(
                    EvidenceKind.BUILD.value, self.config.build_argv
                ),
                VerificationCommand(
                    EvidenceKind.ACCEPTANCE.value,
                    self.config.acceptance_argv,
                    expected_acceptance_scenario_ids=scenario_ids,
                    acceptance_context=AcceptanceCoverageContext(
                        run_id=context.run_id,
                        task_id=context.task_id,
                        attempt=context.attempt,
                        nonce=secrets.token_hex(32),
                    ),
                ),
            ]

        verification_evidence: list[TaskEvidence] = []
        verification_artifacts: list[ProducedArtifact] = []
        all_passed = True
        for command in commands:
            if context.is_cancelled:
                raise RunEngineError("task was canceled during verification")
            evidence, artifact = self._run_command(context, command)
            verification_evidence.append(evidence)
            verification_artifacts.append(artifact)
            all_passed = all_passed and evidence.status == "passed"

        if context.is_cancelled:
            raise RunEngineError("task was canceled after verification")
        if is_root and all_passed:
            current_inputs = _deployment_file_digests(self.workspace)
            changed_inputs = {
                path
                for path in set(release_inputs or {}) | set(current_inputs)
                if (release_inputs or {}).get(path) != current_inputs.get(path)
            }
            if changed_inputs - {"apps/web/next-env.d.ts"}:
                raise RunEngineError(
                    "release source changed while verification was running"
                )
            if "apps/web/next-env.d.ts" in changed_inputs:
                _validate_next_managed_declaration(
                    self.workspace / "apps/web/next-env.d.ts",
                    "apps/web/next-env.d.ts",
                )
            snapshot = create_deployment_snapshot(self.workspace)
            snapshot_digest = hashlib.sha256(snapshot).hexdigest()
            verification_artifacts.append(
                ProducedArtifact(
                    content=snapshot,
                    role="source:release-snapshot",
                    media_type="application/vnd.rich.release-source+zip",
                    metadata={
                        "node_id": node_id,
                        "attempt": context.attempt,
                        "sha256": snapshot_digest,
                    },
                )
            )
            verification_evidence = [
                (
                    TaskEvidence(
                        kind=evidence.kind,
                        status=evidence.status,
                        summary=evidence.summary,
                        blocking=evidence.blocking,
                        requirement_ids=evidence.requirement_ids,
                        acceptance_scenario_ids=evidence.acceptance_scenario_ids,
                        details={
                            **dict(evidence.details),
                            "release_source_sha256": snapshot_digest,
                        },
                    )
                    if evidence.kind == EvidenceKind.ACCEPTANCE.value
                    else evidence
                )
                for evidence in verification_evidence
            ]

        if all_passed:
            # The gates accepted it, so it is now worth replaying. The worker
            # staged this and deliberately cannot commit it itself.
            commit = getattr(self.model_worker, "commit_memo", None)
            if callable(commit):
                commit()

        summary = (
            f"{generated.summary}; independent verification passed"
            if all_passed
            else f"{generated.summary}; independent verification failed"
        )
        return TaskResult(
            succeeded=generated.succeeded and all_passed,
            summary=summary,
            evidence=generated.evidence + tuple(verification_evidence),
            artifacts=generated.artifacts + tuple(verification_artifacts),
        )

    def _property_suite(self, contract_id: str | None) -> str | None:
        """This node's own obligation suite, if the scaffold emitted one.

        A property run over nothing passes, and a passing check that checked
        nothing is the failure mode this whole design exists to avoid.
        """

        if not contract_id or not self._properties.is_dir():
            return None
        slug = "".join(
            character if character.isalnum() else "-" for character in contract_id.lower()
        )
        slug = "-".join(part for part in slug.split("-") if part) or "contract"
        relative = f"tests/properties/{slug}.test.ts"
        return relative if (self.workspace / relative).is_file() else None

    def _run_command(
        self,
        context: TaskContext,
        command: VerificationCommand,
    ) -> tuple[TaskEvidence, ProducedArtifact]:
        error: Exception | None = None
        result: ExecutionResult | None = None
        observed_scenario_ids: tuple[str, ...] = ()
        try:
            candidate = self.command_runner.run(
                self.workspace,
                command,
                cancellation=lambda: context.is_cancelled,
                deadline=context.deadline_monotonic,
            )
            validated = _validated_execution_result(candidate, command)
            if (
                command.kind == EvidenceKind.ACCEPTANCE.value
                and validated.passed
            ):
                assert command.acceptance_context is not None
                observed_scenario_ids = _observed_acceptance_coverage(
                    validated,
                    expected_scenario_ids=(
                        command.expected_acceptance_scenario_ids
                    ),
                    expected_context=command.acceptance_context,
                )
            result = validated
        except Exception as exc:
            error = exc

        if result is None:
            status = "error"
            summary = (
                f"{command.kind} verification was not observed: "
                f"{type(error).__name__}"
            )
            result_document: dict[str, Any] = {
                "schema_version": "rich.command-verification/v1",
                "run_id": context.run_id,
                "task_id": context.task_id,
                "node_id": context.compiled_task.node_id,
                "attempt": context.attempt,
                "kind": command.kind,
                "argv": list(command.argv),
                "status": status,
                "error_type": type(error).__name__,
                "error_message": str(error)[:2000],
                "expected_acceptance_scenario_ids": list(
                    command.expected_acceptance_scenario_ids
                ),
                "observed_acceptance_scenario_ids": [],
            }
        else:
            if result.passed:
                status = "passed"
            elif result.timed_out or result.cancelled:
                status = "error"
            else:
                status = "failed"
            summary = (
                f"{command.kind} command passed"
                if status == "passed"
                else (
                    (
                        f"{command.kind} command canceled"
                        if result.cancelled
                        else f"{command.kind} command timed out"
                    )
                    if result.timed_out or result.cancelled
                    else (
                        f"{command.kind} command exited with "
                        f"{result.returncode}"
                    )
                )
            )
            result_document = {
                "schema_version": "rich.command-verification/v1",
                "run_id": context.run_id,
                "task_id": context.task_id,
                "node_id": context.compiled_task.node_id,
                "attempt": context.attempt,
                "kind": command.kind,
                "argv": list(result.argv),
                "status": status,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "duration_seconds": result.duration_seconds,
                "stdout": _bounded_text(
                    result.stdout, self.config.max_verification_log_bytes
                ),
                "stderr": _bounded_text(
                    result.stderr, self.config.max_verification_log_bytes
                ),
                "expected_acceptance_scenario_ids": list(
                    command.expected_acceptance_scenario_ids
                ),
                "observed_acceptance_scenario_ids": list(
                    observed_scenario_ids
                ),
            }

        artifact_content = _canonical_json_bytes(result_document)
        log_digest = hashlib.sha256(artifact_content).hexdigest()
        scenario_coverage = (
            observed_scenario_ids
            if command.kind == EvidenceKind.ACCEPTANCE.value
            and status == "passed"
            else ()
        )
        requirement_ids = (
            tuple(sorted(self.project.requirement_index))
            if scenario_coverage
            else context.compiled_task.requirement_ids
        )
        evidence = TaskEvidence(
            kind=command.kind,
            status=status,
            summary=summary,
            blocking=True,
            requirement_ids=tuple(requirement_ids),
            acceptance_scenario_ids=tuple(scenario_coverage),
            details={
                "argv": list(command.argv),
                "log_sha256": log_digest,
                "result_source": "sandbox_command",
                "observed_acceptance_scenario_ids": list(
                    scenario_coverage
                ),
            },
        )
        artifact = ProducedArtifact(
            content=artifact_content,
            role=f"verification:{command.kind}",
            media_type="application/vnd.rich.command-verification+json",
            metadata={
                "kind": command.kind,
                "status": status,
                "node_id": context.compiled_task.node_id,
                "attempt": context.attempt,
                "log_sha256": log_digest,
            },
        )
        return evidence, artifact


class RunExecutionOwner:
    """Heartbeat-backed ownership spanning runtime recovery and execution."""

    def __init__(
        self,
        store: RichStore,
        *,
        run_id: str,
        owner_token: str,
        cancellation: CancellationToken,
        lease_seconds: float,
        heartbeat_seconds: float,
    ):
        self.store = store
        self.run_id = run_id
        self.owner_token = owner_token
        self.cancellation = cancellation
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat,
            name=f"rich-run-lease-{run_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    @classmethod
    def claim(
        cls,
        store: RichStore,
        *,
        run_id: str,
        lease_seconds: float = DEFAULT_EXECUTION_LEASE_SECONDS,
        heartbeat_seconds: float = (
            DEFAULT_EXECUTION_HEARTBEAT_SECONDS
        ),
        cancellation: CancellationToken | None = None,
    ) -> "RunExecutionOwner":
        if not isinstance(store, RichStore):
            raise TypeError("store must be a RichStore")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id cannot be empty")
        for name, value in (
            ("lease_seconds", lease_seconds),
            ("heartbeat_seconds", heartbeat_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive")
        if heartbeat_seconds * 2 >= lease_seconds:
            raise ValueError(
                "execution heartbeat must be less than half the lease duration"
            )
        token = cancellation or CancellationToken()
        if not isinstance(token, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")
        if token.is_cancelled:
            raise RunEngineError(
                "run was canceled before execution ownership was claimed"
            )
        try:
            lease = store.claim_run_execution(
                run_id,
                lease_seconds=lease_seconds,
            )
        except StoreError as exc:
            if "active execution owner" in str(exc):
                raise ConcurrentExecutionError(str(exc)) from exc
            raise
        try:
            return cls(
                store,
                run_id=run_id,
                owner_token=lease.owner_token,
                cancellation=token,
                lease_seconds=float(lease_seconds),
                heartbeat_seconds=float(heartbeat_seconds),
            )
        except BaseException:
            store.release_run_execution(
                run_id,
                owner_token=lease.owner_token,
            )
            raise

    @property
    def is_closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def require_active(self, store: RichStore, run_id: str) -> None:
        if store is not self.store or run_id != self.run_id:
            raise ValueError(
                "execution owner belongs to a different store or run"
            )
        if (
            self.is_closed
            or self.cancellation.is_cancelled
            or not self.store.is_run_execution_owner(
                self.run_id,
                self.owner_token,
            )
        ):
            self.cancellation.cancel("durable execution lease was lost")
            raise ConcurrentExecutionError(
                "preclaimed durable execution ownership is no longer active"
            )

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._heartbeat_thread.join(
            timeout=min(1.0, self.heartbeat_seconds)
        )
        if not self.cancellation.is_cancelled:
            self.store.release_run_execution(
                self.run_id,
                owner_token=self.owner_token,
            )

    def __enter__(self) -> "RunExecutionOwner":
        try:
            self.require_active(self.store, self.run_id)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.store.renew_run_execution(
                    self.run_id,
                    owner_token=self.owner_token,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                self.cancellation.cancel(
                    "durable execution lease was lost"
                )
                return


class RunEngine:
    """Execute or resume one prepared public build with release-grade evidence."""

    def __init__(
        self,
        store: RichStore,
        *,
        gateway: ModelGateway,
        command_runner: CommandRunner,
        provider: str,
        model: str,
        config: RunEngineConfig | None = None,
        worker_factory: ModelWorkerFactory | None = None,
        workspace_preparer: WorkspacePreparer | None = None,
        execution_owner_binding: Callable[[str], None] | None = None,
    ):
        if not isinstance(store, RichStore):
            raise TypeError("store must be a RichStore")
        if not isinstance(gateway, ModelGateway):
            raise TypeError("gateway must be a ModelGateway")
        if not callable(getattr(command_runner, "run", None)):
            raise TypeError("command_runner must implement run()")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider cannot be empty")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model cannot be empty")
        self.store = store
        self.gateway = gateway
        self.command_runner = command_runner
        self.provider = provider.strip()
        self.model = model.strip()
        self.config = config or RunEngineConfig()
        self.worker_factory = worker_factory or _coding_worker_factory
        if workspace_preparer is not None and not callable(
            getattr(workspace_preparer, "bootstrap", None)
        ):
            raise TypeError("workspace_preparer must implement bootstrap()")
        self.workspace_preparer = workspace_preparer
        if execution_owner_binding is not None and not callable(
            execution_owner_binding
        ):
            raise TypeError("execution_owner_binding must be callable")
        self.execution_owner_binding = execution_owner_binding

    def execute(
        self,
        *,
        run_id: str,
        workspace: str | Path,
        architecture_approval_id: str | None = None,
        cancellation: CancellationToken | None = None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> SchedulerReport:
        if execution_owner is None:
            token = cancellation or CancellationToken()
            with RunExecutionOwner.claim(
                self.store,
                run_id=run_id,
                lease_seconds=self.config.execution_lease_seconds,
                heartbeat_seconds=(
                    self.config.execution_heartbeat_seconds
                ),
                cancellation=token,
            ) as claimed_owner:
                return self._execute_with_owner(
                    run_id=run_id,
                    workspace=workspace,
                    architecture_approval_id=architecture_approval_id,
                    execution_owner=claimed_owner,
                )
        if not isinstance(execution_owner, RunExecutionOwner):
            raise TypeError(
                "execution_owner must be a RunExecutionOwner"
            )
        if (
            cancellation is not None
            and cancellation is not execution_owner.cancellation
        ):
            raise ValueError(
                "cancellation must match the preclaimed execution owner"
            )
        return self._execute_with_owner(
            run_id=run_id,
            workspace=workspace,
            architecture_approval_id=architecture_approval_id,
            execution_owner=execution_owner,
        )

    def _execute_with_owner(
        self,
        *,
        run_id: str,
        workspace: str | Path,
        architecture_approval_id: str | None,
        execution_owner: RunExecutionOwner,
    ) -> SchedulerReport:
        execution_owner.require_active(self.store, run_id)
        if self.execution_owner_binding is not None:
            self.execution_owner_binding(execution_owner.owner_token)
        try:
            return self._execute_owned(
                run_id=run_id,
                workspace=workspace,
                architecture_approval_id=architecture_approval_id,
                cancellation=execution_owner.cancellation,
                owner_token=execution_owner.owner_token,
            )
        finally:
            self._wait_for_owned_processes()

    def _execute_owned(
        self,
        *,
        run_id: str,
        workspace: str | Path,
        architecture_approval_id: str | None,
        cancellation: CancellationToken,
        owner_token: str,
    ) -> SchedulerReport:
        loaded = self._load_and_validate(
            run_id=run_id,
            workspace=workspace,
            architecture_approval_id=architecture_approval_id,
            owner_token=owner_token,
        )
        if cancellation.is_cancelled:
            raise RunEngineError("run was canceled before workspace preparation")
        if self.workspace_preparer is not None:
            preparation_deadline = (
                time.monotonic() + self.config.task_timeout_seconds
            )
            prepared = self.workspace_preparer.bootstrap(
                loaded.workspace,
                cancellation=lambda: cancellation.is_cancelled,
                deadline=preparation_deadline,
            )
            if not isinstance(getattr(prepared, "passed", None), bool):
                raise RunEngineError(
                    "workspace preparer returned no trustworthy result"
                )
            self._record_workspace_preparation(
                run_id,
                prepared,
                owner_token=owner_token,
            )
            if not prepared.passed:
                raise RunEngineError(
                    "trusted workspace dependency preparation failed"
                )
            # Bootstrap may write only ignored runtime/dependency outputs. Recheck
            # the complete recorded source tree before granting model authority.
            loaded = self._load_and_validate(
                run_id=run_id,
                workspace=workspace,
                architecture_approval_id=architecture_approval_id,
                owner_token=owner_token,
            )
        if cancellation.is_cancelled:
            raise RunEngineError("run was canceled before task execution")
        model_worker = self.worker_factory(
            self.gateway,
            workspace=loaded.workspace,
            project=loaded.project,
            architecture=loaded.architecture,
            approval=loaded.approval,
            provider=self.provider,
            model=self.model,
            limits=self.config.coding_limits,
            max_attempts=self.config.max_provider_attempts,
            mutation_guard=lambda: self.store.is_run_execution_owner(
                run_id, owner_token
            ),
            transaction_sink=_DurableSourceTransactionSink(
                self.store,
                run_id=run_id,
                owner_token=owner_token,
            ),
            prior_failures=_prior_failure_source(self.store, run_id),
            memo=_DurableGenerationMemo(self.store),
        )
        if not callable(model_worker):
            raise TypeError("worker_factory must return a callable task handler")
        handler = _VerifiedCodingHandler(
            model_worker,
            command_runner=self.command_runner,
            workspace=loaded.workspace,
            project=loaded.project,
            root_node_id=loaded.plan.root_node_id,
            config=self.config,
        )
        common_policy = TaskPolicy(
            max_attempts=self.config.max_task_attempts,
            timeout_seconds=self.config.task_timeout_seconds,
            retry_backoff_seconds=self.config.retry_backoff_seconds,
            required_evidence_kinds=(
                EvidenceKind.STATIC.value,
                EvidenceKind.UNIT.value,
            ),
            required_artifact_roles=("source",),
        )
        root_policy = TaskPolicy(
            max_attempts=self.config.max_task_attempts,
            timeout_seconds=self.config.task_timeout_seconds,
            retry_backoff_seconds=self.config.retry_backoff_seconds,
            required_evidence_kinds=(
                EvidenceKind.LINT.value,
                EvidenceKind.STATIC.value,
                EvidenceKind.UNIT.value,
                EvidenceKind.BUILD.value,
                EvidenceKind.ACCEPTANCE.value,
            ),
            required_artifact_roles=("source", "source:release-snapshot"),
            required_acceptance_scenario_ids=tuple(
                sorted(loaded.project.scenario_index)
            ),
        )
        scheduler = DagScheduler(
            self.store,
            run_id=run_id,
            plan=loaded.plan,
            handlers={"*": handler},
            max_workers=self.config.max_workers,
            default_policy=common_policy,
            task_policies={loaded.plan.root_node_id: root_policy},
            owner_token=owner_token,
        )
        return scheduler.run(cancellation)

    def _wait_for_owned_processes(self) -> None:
        """Do not release execution ownership while trusted children are live."""

        waiters: list[Callable[[float | None], bool]] = []
        for owner in (self.workspace_preparer, self.command_runner):
            waiter = getattr(owner, "wait_for_idle", None)
            if callable(waiter) and waiter not in waiters:
                waiters.append(waiter)
        for waiter in waiters:
            if not waiter(None):
                raise RunEngineError(
                    "trusted process runner did not reach an idle state"
                )

    def _record_workspace_preparation(
        self,
        run_id: str,
        prepared: object,
        *,
        owner_token: str,
    ) -> None:
        for name in ("dependency_install", "browser_install"):
            result = getattr(prepared, name, None)
            if result is None:
                continue
            if not isinstance(result, ExecutionResult):
                raise RunEngineError(
                    "workspace preparer returned a malformed command result"
                )
            document = {
                "schema_version": "rich.workspace-preparation/v1",
                "kind": name,
                "argv": list(result.argv),
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "duration_seconds": result.duration_seconds,
                "stdout": _bounded_text(
                    result.stdout, self.config.max_verification_log_bytes
                ),
                "stderr": _bounded_text(
                    result.stderr, self.config.max_verification_log_bytes
                ),
            }
            artifact = self.store.put_artifact(
                _canonical_json_bytes(document),
                media_type=(
                    "application/vnd.rich.workspace-preparation+json"
                ),
                metadata={
                    "kind": name,
                    "passed": result.passed,
                },
            )
            self.store.attach_artifact(
                run_id,
                artifact.digest,
                role=f"bootstrap:{name}",
                owner_token=owner_token,
            )
            self.store.append_event(
                run_id,
                "workspace.prepared",
                {
                    "kind": name,
                    "passed": result.passed,
                    "artifact_digest": artifact.digest,
                },
                owner_token=owner_token,
            )

    def _load_and_validate(
        self,
        *,
        run_id: str,
        workspace: str | Path,
        architecture_approval_id: str | None,
        owner_token: str,
    ) -> _LoadedRun:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id cannot be empty")
        run = self.store.get_run(run_id)
        try:
            approved_budget = RunBudget.from_mapping(run["budget"])
        except (TypeError, ValueError) as exc:
            raise RunEngineError(
                "run has no complete approved execution budget"
            ) from exc
        if self.gateway.budget != approved_budget:
            raise RunEngineError(
                "model gateway budget does not match the durable approved run"
            )
        spec_revision_id = run.get("spec_revision_id")
        architecture_revision_id = run.get("architecture_revision_id")
        if not isinstance(spec_revision_id, str) or not spec_revision_id:
            raise RunEngineError("run has no immutable product-spec revision")
        if (
            not isinstance(architecture_revision_id, str)
            or not architecture_revision_id
        ):
            raise RunEngineError("run has no immutable architecture revision")
        spec_revision = self.store.get_revision(spec_revision_id)
        architecture_revision = self.store.get_revision(
            architecture_revision_id
        )
        if (
            spec_revision.project_id != run["project_id"]
            or architecture_revision.project_id != run["project_id"]
            or spec_revision.kind not in {"product_spec", "project_spec"}
            or architecture_revision.kind != "architecture"
        ):
            raise RunEngineError(
                "run revisions do not belong to its project and expected kinds"
            )
        project = ProjectSpec.from_dict(spec_revision.document)
        architecture = ArchitectureSpec.from_dict(
            architecture_revision.document
        )
        if project.id != run["project_id"]:
            raise RunEngineError("project spec id does not match the durable run")
        architecture.validate_against_project(project)
        plan = compile_architecture(architecture, project)
        self._validate_durable_tasks(run_id, plan)

        events = _all_events(self.store, run_id)
        prepared_approval_id = _prepared_approval_id(events)
        if (
            architecture_approval_id is not None
            and architecture_approval_id != prepared_approval_id
        ):
            raise ApprovalValidationError(
                "explicit approval is not the approval bound to run preparation"
            )
        approval_id = architecture_approval_id or prepared_approval_id
        approval_record = self.store.get_approval(approval_id)
        _validate_approval(
            approval_record,
            run=run,
            spec_revision_id=spec_revision_id,
            architecture_revision_id=architecture_revision_id,
            architecture=architecture,
        )
        approval = ApprovalWitness(
            project_id=project.id,
            project_revision=project.revision,
            architecture_id=architecture.id,
            architecture_revision=architecture.revision,
        )

        resolved_workspace = Path(workspace).resolve(strict=True)
        if not resolved_workspace.is_dir():
            raise WorkspaceValidationError("workspace must be an existing directory")
        scaffold_event = _scaffold_event(events)
        recorded_destination = scaffold_event["payload"].get("destination")
        if not isinstance(recorded_destination, str) or not recorded_destination:
            raise WorkspaceValidationError(
                "scaffold event has no recorded destination"
            )
        try:
            resolved_recorded = Path(recorded_destination).resolve(strict=True)
        except OSError as exc:
            raise WorkspaceValidationError(
                "recorded scaffold destination no longer exists"
            ) from exc
        if resolved_workspace != resolved_recorded:
            raise WorkspaceValidationError(
                "workspace is not the destination recorded by scaffold.completed"
            )
        _recover_prepared_source_transactions(
            self.store,
            run_id=run_id,
            workspace=resolved_workspace,
            plan=plan,
            owner_token=owner_token,
        )
        _validate_protected_scaffold(
            self.store,
            run_id=run_id,
            workspace=resolved_workspace,
            architecture=architecture,
            plan=plan,
            scaffold_event=scaffold_event,
        )
        return _LoadedRun(
            project=project,
            architecture=architecture,
            plan=plan,
            approval=approval,
            workspace=resolved_workspace,
        )

    def _validate_durable_tasks(
        self, run_id: str, plan: CompiledArchitecture
    ) -> None:
        actual = {record["id"]: record for record in self.store.list_tasks(run_id)}
        expected_ids = {
            f"{run_id}:{task.task_id}" for task in plan.tasks
        }
        if set(actual) != expected_ids:
            raise RunEngineError(
                "durable task set does not exactly match the compiled architecture"
            )
        for task in plan.tasks:
            task_id = f"{run_id}:{task.task_id}"
            record = actual[task_id]
            expected_dependencies = tuple(
                f"{run_id}:implement:{dependency_id}"
                for dependency_id in task.dependency_ids
            )
            if (
                record["node_id"] != task.node_id
                or record["kind"] != "implement"
                or tuple(record["dependency_task_ids"]) != expected_dependencies
            ):
                raise RunEngineError(
                    f"durable task {task_id!r} does not match the compiled plan"
                )


def _all_events(store: RichStore, run_id: str) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    after = 0
    while True:
        page = store.list_events(run_id, after_sequence=after, limit=1000)
        if not page:
            break
        events.extend(page)
        after = int(page[-1]["sequence"])
        if len(page) < 1000:
            break
    return tuple(events)


def _prepared_approval_id(events: Sequence[Mapping[str, Any]]) -> str:
    prepared = [
        event for event in events if event.get("event_type") == "run.prepared"
    ]
    if len(prepared) != 1:
        raise ApprovalValidationError(
            "run must have exactly one durable preparation event"
        )
    approval_id = prepared[0].get("payload", {}).get(
        "architecture_approval_id"
    )
    if not isinstance(approval_id, str) or not approval_id:
        raise ApprovalValidationError(
            "run preparation is not bound to an architecture approval"
        )
    return approval_id


def _validate_approval(
    approval: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    spec_revision_id: str,
    architecture_revision_id: str,
    architecture: ArchitectureSpec,
) -> None:
    failures: list[str] = []
    if approval.get("status") != "approved":
        failures.append("approval is not approved")
    if approval.get("gate") != "architecture":
        failures.append("approval gate is not architecture")
    if approval.get("project_id") != run.get("project_id"):
        failures.append("approval belongs to a different project")
    if approval.get("run_id") not in {None, run.get("id")}:
        failures.append("approval is bound to a different run")
    request = approval.get("request")
    if not isinstance(request, Mapping):
        failures.append("approval has no immutable request")
    else:
        if request.get("revision_id") != architecture_revision_id:
            failures.append("approval covers a different architecture revision")
        if request.get("spec_revision_id") != spec_revision_id:
            failures.append("approval covers a different product-spec revision")
        if request.get("target_pack") != architecture.target_pack:
            failures.append("approval covers a different target pack")
        if request.get("node_ids") != sorted(architecture.node_index):
            failures.append("approval covers a different architecture node set")
    if failures:
        raise ApprovalValidationError("; ".join(failures))


def _scaffold_event(
    events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    scaffolded = [
        event
        for event in events
        if event.get("event_type") == "scaffold.completed"
    ]
    if not scaffolded:
        raise WorkspaceValidationError(
            "run has no completed scaffold destination"
        )
    destinations = {
        event.get("payload", {}).get("destination") for event in scaffolded
    }
    if len(destinations) != 1:
        raise WorkspaceValidationError(
            "run has conflicting recorded scaffold destinations"
        )
    return scaffolded[-1]


@dataclass(frozen=True, slots=True)
class _RecoverySourceFile:
    path: PurePosixPath
    operation: str
    intended: bytes
    original_existed: bool
    original: bytes | None
    original_mode: int | None


def _recover_prepared_source_transactions(
    store: RichStore,
    *,
    run_id: str,
    workspace: Path,
    plan: CompiledArchitecture,
    owner_token: str,
) -> None:
    """Roll back every uncommitted write-ahead record before source validation."""

    with source_transaction_lock(
        workspace,
        authority_check=lambda: store.is_run_execution_owner(
            run_id, owner_token
        ),
    ):
        _require_recovery_owner(store, run_id, owner_token)
        _recover_prepared_source_transactions_locked(
            store,
            run_id=run_id,
            workspace=workspace,
            plan=plan,
            owner_token=owner_token,
        )


def _recover_prepared_source_transactions_locked(
    store: RichStore,
    *,
    run_id: str,
    workspace: Path,
    plan: CompiledArchitecture,
    owner_token: str,
) -> None:
    owned_by_task = {
        f"{run_id}:{task.task_id}": tuple(
            PurePosixPath(path) for path in task.owned_paths
        )
        for task in plan.tasks
    }
    # Newest-first preserves stack semantics if a process somehow prepared
    # more than one mutation before an earlier journal was resolved.
    transactions = reversed(
        store.list_source_transactions(run_id, status="prepared")
    )
    for transaction in transactions:
        _require_recovery_owner(store, run_id, owner_token)
        task_id = transaction["task_id"]
        owned_paths = owned_by_task.get(task_id)
        if owned_paths is None:
            raise WorkspaceValidationError(
                "prepared source transaction is not bound to a compiled task"
            )
        files = _load_recovery_source_files(
            store,
            transaction=transaction,
            owned_paths=owned_paths,
        )
        _restore_original_source_files(
            store,
            run_id=run_id,
            workspace=workspace,
            owner_token=owner_token,
            files=files,
        )
        # If the process dies after restoration but before this transition, a
        # successor sees the same journal and original bytes; restoration is a
        # no-op and this resolution is safely retried.
        store.rollback_source_transaction(
            run_id,
            task_id=task_id,
            attempt=int(transaction["attempt"]),
            owner_token=owner_token,
            reason="interrupted_process_recovery",
        )


def _load_recovery_source_files(
    store: RichStore,
    *,
    transaction: Mapping[str, Any],
    owned_paths: Sequence[PurePosixPath],
) -> tuple[_RecoverySourceFile, ...]:
    journal_artifact = store.get_artifact(str(transaction["journal_digest"]))
    generated_artifact = store.get_artifact(
        str(transaction["generated_digest"])
    )
    if journal_artifact.media_type != (
        "application/vnd.rich.source-transaction-journal+json"
    ):
        raise WorkspaceValidationError(
            "prepared source transaction journal has an unsupported media type"
        )
    if generated_artifact.media_type != (
        "application/vnd.rich.generated-source+json"
    ):
        raise WorkspaceValidationError(
            "prepared generated source has an unsupported media type"
        )
    journal = _read_source_transaction_json(
        journal_artifact.path, "source transaction journal"
    )
    generated = _read_source_transaction_json(
        generated_artifact.path, "prepared generated source"
    )
    expected_journal_keys = {
        "schema_version",
        "run_id",
        "task_id",
        "attempt",
        "source_digest",
        "generated_artifact_digest",
        "files",
    }
    if (
        set(journal) != expected_journal_keys
        or journal.get("schema_version")
        != "rich.source-transaction-journal/v1"
        or journal.get("run_id") != transaction["run_id"]
        or journal.get("task_id") != transaction["task_id"]
        or journal.get("attempt") != transaction["attempt"]
        or journal.get("generated_artifact_digest")
        != transaction["generated_digest"]
        or not isinstance(journal.get("files"), list)
    ):
        raise WorkspaceValidationError(
            "prepared source transaction journal identity is invalid"
        )
    if (
        set(generated)
        != {"schema_version", "source_digest", "summary", "files"}
        or generated.get("schema_version") != "rich.generated-source/v1"
        or not isinstance(generated.get("summary"), str)
        or not isinstance(generated.get("files"), list)
    ):
        raise WorkspaceValidationError(
            "prepared generated source has an unsupported schema"
        )
    journal_files = journal["files"]
    generated_files = generated["files"]
    if not journal_files or len(journal_files) != len(generated_files):
        raise WorkspaceValidationError(
            "prepared source journal and generated source do not align"
        )

    generated_by_path: dict[str, tuple[str, bytes]] = {}
    digest = hashlib.sha256()
    for index, entry in enumerate(generated_files):
        if (
            not isinstance(entry, Mapping)
            or set(entry)
            != {"operation", "path", "size", "sha256", "content"}
        ):
            raise WorkspaceValidationError(
                f"prepared generated source file {index} is malformed"
            )
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            raise WorkspaceValidationError(
                f"prepared generated source file {index} has no path"
            )
        path = _safe_manifest_path(path_value)
        if (
            path_value in generated_by_path
            or not _is_owned(path, owned_paths)
            or is_protected_generation_path(path)
        ):
            raise WorkspaceValidationError(
                f"prepared generated source has unauthorized path: {path_value}"
            )
        operation = entry.get("operation")
        content = entry.get("content")
        if operation not in {"create", "replace"} or not isinstance(
            content, str
        ):
            raise WorkspaceValidationError(
                f"prepared generated source file {path_value!r} is invalid"
            )
        content_bytes = content.encode("utf-8")
        content_digest = hashlib.sha256(content_bytes).hexdigest()
        if (
            entry.get("size") != len(content_bytes)
            or entry.get("sha256") != content_digest
        ):
            raise WorkspaceValidationError(
                f"prepared generated source integrity failed: {path_value}"
            )
        generated_by_path[path_value] = (operation, content_bytes)

    for path_value in sorted(generated_by_path):
        content = generated_by_path[path_value][1]
        path_bytes = path_value.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    source_digest = digest.hexdigest()
    if (
        generated.get("source_digest") != source_digest
        or journal.get("source_digest") != source_digest
    ):
        raise WorkspaceValidationError(
            "prepared source transaction has a mismatched source digest"
        )

    recovered: list[_RecoverySourceFile] = []
    seen: set[str] = set()
    for index, entry in enumerate(journal_files):
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"path", "operation", "intended", "original"}
        ):
            raise WorkspaceValidationError(
                f"source transaction journal file {index} is malformed"
            )
        path_value = entry.get("path")
        if not isinstance(path_value, str) or path_value in seen:
            raise WorkspaceValidationError(
                f"source transaction journal file {index} repeats its path"
            )
        seen.add(path_value)
        generated_entry = generated_by_path.get(path_value)
        if generated_entry is None or entry.get("operation") != generated_entry[0]:
            raise WorkspaceValidationError(
                f"source transaction journal path is not generated: {path_value}"
            )
        intended = entry.get("intended")
        original = entry.get("original")
        if (
            not isinstance(intended, Mapping)
            or set(intended) != {"size", "sha256"}
            or intended.get("size") != len(generated_entry[1])
            or intended.get("sha256")
            != hashlib.sha256(generated_entry[1]).hexdigest()
            or not isinstance(original, Mapping)
            or set(original)
            != {"existed", "mode", "size", "sha256", "content_base64"}
        ):
            raise WorkspaceValidationError(
                f"source transaction journal integrity failed: {path_value}"
            )
        existed = original.get("existed")
        mode = original.get("mode")
        if (
            not isinstance(existed, bool)
            or (entry["operation"] == "create" and existed)
            or (entry["operation"] == "replace" and not existed)
        ):
            raise WorkspaceValidationError(
                f"source transaction original state is invalid: {path_value}"
            )
        if existed:
            encoded = original.get("content_base64")
            if (
                not isinstance(encoded, str)
                or isinstance(mode, bool)
                or not isinstance(mode, int)
                or mode < 0
                or mode > 0o7777
            ):
                raise WorkspaceValidationError(
                    f"source transaction original bytes are invalid: {path_value}"
                )
            try:
                original_bytes = base64.b64decode(
                    encoded.encode("ascii"), validate=True
                )
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise WorkspaceValidationError(
                    f"source transaction original bytes are invalid: {path_value}"
                ) from exc
            if base64.b64encode(original_bytes).decode("ascii") != encoded:
                raise WorkspaceValidationError(
                    f"source transaction original encoding is not canonical: "
                    f"{path_value}"
                )
            if (
                original.get("size") != len(original_bytes)
                or original.get("sha256")
                != hashlib.sha256(original_bytes).hexdigest()
            ):
                raise WorkspaceValidationError(
                    f"source transaction original integrity failed: {path_value}"
                )
        else:
            original_bytes = None
            if (
                mode is not None
                or original.get("size") != 0
                or original.get("sha256") is not None
                or original.get("content_base64") is not None
            ):
                raise WorkspaceValidationError(
                    f"source transaction absent state is invalid: {path_value}"
                )
        recovered.append(
            _RecoverySourceFile(
                path=_safe_manifest_path(path_value),
                operation=str(entry["operation"]),
                intended=generated_entry[1],
                original_existed=existed,
                original=original_bytes,
                original_mode=mode,
            )
        )
    if seen != set(generated_by_path):
        raise WorkspaceValidationError(
            "source transaction journal does not cover every generated path"
        )
    return tuple(recovered)


def _read_source_transaction_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceValidationError(
            f"{label} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(document, Mapping):
        raise WorkspaceValidationError(f"{label} must be a JSON object")
    return document


def _restore_original_source_files(
    store: RichStore,
    *,
    run_id: str,
    workspace: Path,
    owner_token: str,
    files: Sequence[_RecoverySourceFile],
) -> None:
    # Validate the entire interrupted tree before restoring any one path.  Each
    # file may be either its journaled original or intended value because a
    # process can die between any two os.replace calls.
    for item in files:
        current = _read_recovery_destination(workspace, item.path)
        if item.original_existed:
            assert item.original is not None
            if current not in {item.original, item.intended}:
                raise WorkspaceValidationError(
                    f"interrupted source path has unjournaled bytes: {item.path}"
                )
        elif current is not None and current != item.intended:
            raise WorkspaceValidationError(
                f"interrupted source path has unjournaled bytes: {item.path}"
            )

    for item in reversed(tuple(files)):
        _require_recovery_owner(store, run_id, owner_token)
        destination = workspace.joinpath(*item.path.parts)
        current = _read_recovery_destination(workspace, item.path)
        if item.original_existed:
            assert item.original is not None
            if current == item.original:
                continue
            if current != item.intended:
                raise WorkspaceValidationError(
                    f"interrupted source changed during recovery: {item.path}"
                )
            _replace_recovery_file(
                workspace,
                destination,
                item.original,
                item.original_mode or 0o644,
            )
        else:
            if current is None:
                continue
            if current != item.intended:
                raise WorkspaceValidationError(
                    f"interrupted source changed during recovery: {item.path}"
                )
            try:
                destination.unlink()
                _fsync_directory(destination.parent)
            except OSError as exc:
                raise WorkspaceValidationError(
                    f"interrupted source could not be removed: {item.path}"
                ) from exc

    _require_recovery_owner(store, run_id, owner_token)
    for item in files:
        current = _read_recovery_destination(workspace, item.path)
        if item.original_existed:
            if current != item.original:
                raise WorkspaceValidationError(
                    f"source recovery did not restore: {item.path}"
                )
        elif current is not None:
            raise WorkspaceValidationError(
                f"source recovery did not remove: {item.path}"
            )


def _read_recovery_destination(
    workspace: Path, path: PurePosixPath
) -> bytes | None:
    destination = workspace.joinpath(*path.parts)
    cursor = workspace
    for component in path.parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceValidationError(
                f"interrupted source path cannot be inspected: {path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceValidationError(
                f"interrupted source path traverses a symlink: {path}"
            )
    if not stat.S_ISREG(metadata.st_mode):
        raise WorkspaceValidationError(
            f"interrupted source path is not a regular file: {path}"
        )
    try:
        return destination.read_bytes()
    except OSError as exc:
        raise WorkspaceValidationError(
            f"interrupted source path cannot be read: {path}"
        ) from exc


def _replace_recovery_file(
    workspace: Path,
    destination: Path,
    content: bytes,
    mode: int,
) -> None:
    staging = workspace / ".rich" / "runtime" / "source-transactions"
    cursor = workspace
    for component in staging.relative_to(workspace).parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise WorkspaceValidationError(
                "source recovery staging cannot be inspected"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceValidationError(
                "source recovery staging traverses a symlink"
            )
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceValidationError(
            "source recovery staging cannot be prepared"
        ) from exc
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".rich-source-recovery-",
        dir=staging,
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.fchmod(temporary.fileno(), mode)
        os.replace(temporary_name, destination)
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise WorkspaceValidationError(
            f"interrupted source could not be restored: {destination.name}"
        ) from exc
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_recovery_owner(
    store: RichStore, run_id: str, owner_token: str
) -> None:
    if not store.is_run_execution_owner(run_id, owner_token):
        raise ConcurrentExecutionError(
            "durable execution lease was lost during source recovery"
        )


def _validate_protected_scaffold(
    store: RichStore,
    *,
    run_id: str,
    workspace: Path,
    architecture: ArchitectureSpec,
    plan: CompiledArchitecture,
    scaffold_event: Mapping[str, Any],
) -> None:
    manifest_digest = scaffold_event.get("payload", {}).get("manifest_digest")
    if not isinstance(manifest_digest, str) or not manifest_digest:
        raise WorkspaceValidationError(
            "scaffold event is not bound to a manifest artifact"
        )
    attachments = store.list_run_artifacts(run_id)
    if not any(
        record["role"] == "scaffold_manifest"
        and record["digest"] == manifest_digest
        for record in attachments
    ):
        raise WorkspaceValidationError(
            "recorded scaffold manifest is not attached to the run"
        )
    manifest_artifact = store.get_artifact(manifest_digest)
    try:
        manifest = json.loads(manifest_artifact.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceValidationError(
            "scaffold manifest is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise WorkspaceValidationError("scaffold manifest must be an object")
    if manifest.get("target_pack") != architecture.target_pack:
        raise WorkspaceValidationError(
            "scaffold manifest target pack does not match architecture"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise WorkspaceValidationError("scaffold manifest files must be a list")
    expected: dict[PurePosixPath, tuple[int, str, bool]] = {
        PurePosixPath(".rich/target-pack.json"): (
            manifest_artifact.size,
            manifest_artifact.digest,
            False,
        )
    }
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping):
            raise WorkspaceValidationError(
                f"scaffold manifest file {index} must be an object"
            )
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            raise WorkspaceValidationError(
                f"scaffold manifest file {index} has no path"
            )
        path = _safe_manifest_path(path_value)
        if path in expected:
            raise WorkspaceValidationError(
                f"scaffold manifest repeats path {path_value!r}"
            )
        expected_size = entry.get("size")
        expected_digest = _manifest_sha256(entry.get("sha256"), path_value)
        mutable = entry.get("mutable", False)
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(mutable, bool)
        ):
            raise WorkspaceValidationError(
                f"scaffold manifest file {path_value!r} has invalid integrity data"
            )
        if mutable and (
            path.as_posix() != "apps/web/next-env.d.ts"
            or entry.get("managed_by") != "nextjs"
        ):
            raise WorkspaceValidationError(
                f"scaffold manifest file {path_value!r} has an unsupported "
                "mutable declaration"
            )
        expected[path] = (expected_size, expected_digest, mutable)

    task_owned_paths = {
        f"{run_id}:{task.task_id}": tuple(
            PurePosixPath(path) for path in task.owned_paths
        )
        for task in plan.tasks
    }
    for attachment in attachments:
        if attachment["role"] != "generated-source":
            continue
        task_id = attachment.get("task_id")
        owned_paths = task_owned_paths.get(task_id)
        if owned_paths is None:
            raise WorkspaceValidationError(
                "generated source artifact is not bound to a compiled task"
            )
        generated = store.get_artifact(str(attachment["digest"]))
        try:
            document = json.loads(generated.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceValidationError(
                "generated source artifact is not valid UTF-8 JSON"
            ) from exc
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != "rich.generated-source/v1"
            or not isinstance(document.get("files"), list)
        ):
            raise WorkspaceValidationError(
                "generated source artifact has an unsupported schema"
            )
        for file_index, generated_entry in enumerate(document["files"]):
            if not isinstance(generated_entry, Mapping):
                raise WorkspaceValidationError(
                    f"generated source file {file_index} must be an object"
                )
            generated_path_value = generated_entry.get("path")
            if not isinstance(generated_path_value, str):
                raise WorkspaceValidationError(
                    f"generated source file {file_index} has no path"
                )
            generated_path = _safe_manifest_path(generated_path_value)
            if not _is_owned(generated_path, owned_paths):
                raise WorkspaceValidationError(
                    f"generated source path is outside its task ownership: "
                    f"{generated_path_value}"
                )
            if is_protected_generation_path(generated_path):
                raise WorkspaceValidationError(
                    f"generated source artifact modifies protected verification "
                    f"or toolchain input: {generated_path_value}"
                )
            operation = generated_entry.get("operation")
            if operation not in {"create", "replace"}:
                raise WorkspaceValidationError(
                    f"generated source file {generated_path_value!r} has an "
                    "unsupported operation"
                )
            if operation == "create" and generated_path in expected:
                raise WorkspaceValidationError(
                    f"generated source create collides with an existing path: "
                    f"{generated_path_value}"
                )
            if operation == "replace" and generated_path not in expected:
                raise WorkspaceValidationError(
                    f"generated source replace targets an unknown path: "
                    f"{generated_path_value}"
                )
            content = generated_entry.get("content")
            if not isinstance(content, str):
                raise WorkspaceValidationError(
                    f"generated source file {generated_path_value!r} has no text content"
                )
            content_bytes = content.encode("utf-8")
            actual_digest = hashlib.sha256(content_bytes).hexdigest()
            if (
                generated_entry.get("size") != len(content_bytes)
                or generated_entry.get("sha256") != actual_digest
            ):
                raise WorkspaceValidationError(
                    f"generated source artifact integrity failed: "
                    f"{generated_path_value}"
                )
            expected[generated_path] = (
                len(content_bytes),
                actual_digest,
                False,
            )

    for path, (expected_size, expected_digest, mutable) in sorted(
        expected.items(), key=lambda item: item[0].as_posix()
    ):
        path_value = path.as_posix()
        destination = workspace.joinpath(*path.parts)
        cursor = workspace
        unsafe_symlink = False
        for component in path.parts:
            cursor = cursor / component
            if cursor.is_symlink():
                unsafe_symlink = True
                break
        if unsafe_symlink or not destination.is_file():
            raise WorkspaceValidationError(
                f"protected scaffold file is missing or unsafe: {path_value}"
            )
        if mutable:
            _validate_next_managed_declaration(destination, path_value)
            continue
        digest = hashlib.sha256()
        size = 0
        try:
            with destination.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise WorkspaceValidationError(
                f"protected scaffold file cannot be read: {path_value}"
            ) from exc
        if size != expected_size or digest.hexdigest() != expected_digest:
            raise WorkspaceValidationError(
                f"protected scaffold file changed: {path_value}"
            )

    known_paths = set(expected)
    for parent, directory_names, file_names in os.walk(workspace):
        prefix = Path(parent).relative_to(workspace)
        entries = sorted(directory_names) + sorted(file_names)
        # Prune runtime output before descending rather than after. A pnpm
        # workspace is a link farm -- `apps/web/node_modules/@scope/pkg` points
        # back at `packages/pkg` -- so walking into it can loop, and can leave
        # the workspace entirely through a store link.
        directory_names[:] = [
            name
            for name in directory_names
            if not _is_runtime_output(_relative_posix(prefix, name))
        ]
        for name in entries:
            relative = _relative_posix(prefix, name)
            # node_modules and .next are installed and generated by the trusted
            # bootstrapper from a frozen, integrity-verified lockfile. They are
            # not source, and the symlinks inside them are how pnpm works;
            # rejecting those made every real bootstrapped run unvalidatable.
            if _is_runtime_output(relative):
                continue
            candidate = Path(parent) / name
            if candidate.is_symlink():
                raise WorkspaceValidationError(
                    f"workspace contains an unsafe symbolic link: {relative}"
                )
            if candidate.is_dir():
                continue
            if relative not in known_paths:
                raise WorkspaceValidationError(
                    f"workspace contains an unrecorded file: {relative}"
                )


def _relative_posix(prefix: Path, name: str) -> PurePosixPath:
    return PurePosixPath(
        name if prefix == Path(".") else f"{prefix.as_posix()}/{name}"
    )


def _safe_manifest_path(value: str) -> PurePosixPath:
    try:
        return safe_relative_path(value, label="scaffold manifest path")
    except UnsafePath as exc:
        raise WorkspaceValidationError(
            f"scaffold manifest path escapes the workspace: {value!r}"
        ) from exc


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NEXT_ENV_IMPORT = re.compile(
    r'^import "\./\.next/(?:dev/)?types/routes\.d\.ts";$'
)
_RUNTIME_DIRECTORY_NAMES = frozenset(
    {
        ".next",
        "node_modules",
        "coverage",
        "playwright-report",
        "test-results",
    }
)


def _manifest_sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise WorkspaceValidationError(
            f"scaffold manifest file {path!r} must use a sha256 digest"
        )
    digest = value.removeprefix("sha256:")
    if not _SHA256.fullmatch(digest):
        raise WorkspaceValidationError(
            f"scaffold manifest file {path!r} has an invalid sha256 digest"
        )
    return digest


def _validate_next_managed_declaration(path: Path, display_path: str) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceValidationError(
            f"Next-managed declaration cannot be read: {display_path}"
        ) from exc
    if len(content.encode("utf-8")) > 4096:
        raise WorkspaceValidationError(
            f"Next-managed declaration is too large: {display_path}"
        )
    allowed_references = {
        '/// <reference types="next" />',
        '/// <reference types="next/image-types/global" />',
    }
    allowed_comments = {
        "// This file is generated by Next.js and should not be edited.",
        "// NOTE: This file should not be edited",
        (
            "// see https://nextjs.org/docs/app/api-reference/config/"
            "typescript for more information."
        ),
    }
    seen_lines: set[str] = set()
    import_count = 0
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and stripped in seen_lines:
            raise WorkspaceValidationError(
                f"Next-managed declaration repeats a line: {display_path}"
            )
        if stripped:
            seen_lines.add(stripped)
        if stripped.startswith("///"):
            allowed = stripped in allowed_references
        else:
            allowed = (
                not stripped
                or stripped in allowed_comments
                or _NEXT_ENV_IMPORT.fullmatch(stripped) is not None
            )
        if _NEXT_ENV_IMPORT.fullmatch(stripped) is not None:
            import_count += 1
        if (
            allowed
        ):
            continue
        raise WorkspaceValidationError(
            f"Next-managed declaration contains unsupported code: {display_path}"
        )
    if not allowed_references <= seen_lines or import_count > 1:
        raise WorkspaceValidationError(
            f"Next-managed declaration is incomplete or ambiguous: {display_path}"
        )


def _is_runtime_output(path: PurePosixPath) -> bool:
    if path.parts[:2] == (".rich", "runtime"):
        return True
    return any(part in _RUNTIME_DIRECTORY_NAMES for part in path.parts)


def _is_owned(
    path: PurePosixPath, owned_paths: Sequence[PurePosixPath]
) -> bool:
    return is_owned(path, owned_paths)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    # Coerced here rather than in the shared encoder: json.dumps serializes only
    # dict, and widening the shared form to accept any Mapping would quietly
    # change what it does with everything else.
    return canonical_json_bytes(dict(value))


def _validated_execution_result(
    value: object, command: VerificationCommand
) -> ExecutionResult:
    if not isinstance(value, ExecutionResult):
        raise TypeError("command runner returned no observed ExecutionResult")
    if value.argv != command.argv:
        raise ValueError("command runner result argv does not match its request")
    if (
        isinstance(value.returncode, bool)
        or not isinstance(value.returncode, int)
        or not isinstance(value.stdout, str)
        or not isinstance(value.stderr, str)
        or isinstance(value.duration_seconds, bool)
        or not isinstance(value.duration_seconds, (int, float))
        or not math.isfinite(value.duration_seconds)
        or value.duration_seconds < 0
        or not isinstance(value.timed_out, bool)
        or not isinstance(value.cancelled, bool)
    ):
        raise TypeError("command runner returned a malformed ExecutionResult")
    return value


def _bounded_text(value: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        value = str(value)
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    marker = b"\n...[truncated by RICH verifier]\n"
    return (encoded[: max(0, max_bytes - len(marker))] + marker).decode(
        "utf-8", errors="replace"
    )


def _deployment_file_digests(workspace: Path) -> dict[str, tuple[str, int]]:
    return {
        file.file: (file.sha, file.size)
        for file in collect_deployment_files(workspace)
    }
