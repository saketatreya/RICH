"""Durable local state and content-addressed artifacts for the RICH control plane.

The store deliberately persists JSON documents instead of importing the model layer.
That keeps persistence usable during schema migrations: callers validate a document with
the model version that owns it, while the store preserves the exact accepted payload.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from .canonical import canonical_json_text as _canonical_json
from .models import ApprovalGate
from uuid import uuid4


SCHEMA_VERSION = 11
_UNSET = object()

_RUN_TRANSITIONS = {
    "draft": {"awaiting_approval", "canceled"},
    "awaiting_approval": {"ready", "blocked", "canceled"},
    "ready": {"running", "canceled"},
    "running": {"verifying", "failed", "blocked", "canceled"},
    "verifying": {"succeeded", "failed", "running", "blocked", "canceled"},
    "blocked": {
        "awaiting_approval",
        "ready",
        "running",
        "failed",
        "canceled",
    },
    "succeeded": set(),
    "failed": set(),
    "canceled": set(),
}
_TASK_TRANSITIONS = {
    "pending": {"ready", "blocked", "canceled"},
    "ready": {"running", "cached", "blocked", "canceled"},
    "running": {"verifying", "succeeded", "failed", "blocked", "canceled"},
    "verifying": {"succeeded", "failed", "running", "blocked", "canceled"},
    "failed": {"ready", "canceled"},
    "blocked": {"ready", "canceled"},
    "succeeded": set(),
    "cached": set(),
    "canceled": set(),
}
_PREVIEW_TRANSITIONS = {
    "awaiting_approval": {"deploying", "failed"},
    "deploying": {"ready", "failed", "destroying"},
    "ready": {"destroying"},
    "failed": {"destroying"},
    "destroying": {"destroyed", "destroy_failed"},
    "destroy_failed": {"destroying"},
    "destroyed": set(),
}


class StoreError(RuntimeError):
    """Base class for durable-store failures."""


class NotFoundError(StoreError):
    """Requested durable object does not exist."""


class RevisionConflict(StoreError):
    """An optimistic write was based on a stale project revision."""


@dataclass(frozen=True, slots=True)
class Revision:
    id: str
    project_id: str
    number: int
    kind: str
    schema_version: str
    document: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class Artifact:
    digest: str
    size: int
    media_type: str
    path: Path
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    status_code: int
    response: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IdempotencyLease:
    owner_token: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    owner_token: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class PreviewLease:
    owner_token: str
    expires_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_lease_seconds(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
        or value > 86_400
    ):
        raise ValueError("lease must be positive and no longer than one day")


def _parsed_timestamp(value: object) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if timestamp.tzinfo is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return timestamp




def _decode_json(value: str | None) -> Any:
    if value is None or value == "":
        return None
    return json.loads(value)


class RichStore:
    """SQLite-backed project state plus an immutable artifact directory.

    A fresh connection is used for every transaction so the object is safe to share
    between the local HTTP server and worker threads. SQLite performs serialization;
    callers use ``expected_revision`` for user-visible optimistic concurrency.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.db_path = self.root / "rich.sqlite3"
        self.artifact_root = self.root / "artifacts" / "sha256"
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in conn.execute("SELECT version FROM schema_migrations")
            }
            if 1 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE projects (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        current_revision INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE revisions (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        number INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(project_id, number)
                    );
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        status TEXT NOT NULL,
                        spec_revision_id TEXT REFERENCES revisions(id),
                        architecture_revision_id TEXT REFERENCES revisions(id),
                        budget_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        node_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        cache_key TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(run_id, node_id, kind)
                    );
                    CREATE TABLE events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX events_run_sequence ON events(run_id, sequence);
                    CREATE TABLE artifacts (
                        digest TEXT PRIMARY KEY,
                        size INTEGER NOT NULL,
                        media_type TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE run_artifacts (
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                        digest TEXT NOT NULL REFERENCES artifacts(digest),
                        role TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(run_id, digest, role)
                    );
                    CREATE TABLE approvals (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
                        gate TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        decision_json TEXT,
                        created_at TEXT NOT NULL,
                        decided_at TEXT
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (1, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 2 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE idempotency_keys (
                        key TEXT PRIMARY KEY,
                        operation TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        state TEXT NOT NULL,
                        status_code INTEGER,
                        response_json TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (2, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 3 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE previews (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        approval_id TEXT NOT NULL UNIQUE
                            REFERENCES approvals(id) ON DELETE CASCADE,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        result_json TEXT,
                        error_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        destroyed_at TEXT
                    );
                    CREATE INDEX previews_run_created
                        ON previews(run_id, created_at);
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (3, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 4 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    UPDATE approvals
                    SET status = 'requested'
                    WHERE status = 'pending';
                    ALTER TABLE tasks
                    ADD COLUMN dependency_task_ids_json TEXT NOT NULL DEFAULT '[]';
                    ALTER TABLE run_artifacts RENAME TO run_artifacts_v3;
                    CREATE TABLE run_artifacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL
                            REFERENCES runs(id) ON DELETE CASCADE,
                        task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                        digest TEXT NOT NULL REFERENCES artifacts(digest),
                        role TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO run_artifacts(
                        run_id, task_id, digest, role, created_at
                    )
                    SELECT run_id, task_id, digest, role, created_at
                    FROM run_artifacts_v3;
                    DROP TABLE run_artifacts_v3;
                    CREATE INDEX run_artifacts_run
                        ON run_artifacts(run_id, created_at, id);
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (4, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 5 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE idempotency_keys
                    ADD COLUMN owner_token TEXT;
                    ALTER TABLE idempotency_keys
                    ADD COLUMN lease_expires_at TEXT;
                    UPDATE idempotency_keys
                    SET lease_expires_at = created_at
                    WHERE state = 'in_progress';
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (5, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 6 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE run_execution_leases (
                        run_id TEXT PRIMARY KEY
                            REFERENCES runs(id) ON DELETE CASCADE,
                        owner_token TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        lease_expires_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (6, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 7 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE source_transactions (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL
                            REFERENCES runs(id) ON DELETE CASCADE,
                        task_id TEXT NOT NULL
                            REFERENCES tasks(id) ON DELETE CASCADE,
                        attempt INTEGER NOT NULL,
                        prepared_owner_token TEXT NOT NULL,
                        resolved_owner_token TEXT,
                        journal_digest TEXT NOT NULL
                            REFERENCES artifacts(digest),
                        generated_digest TEXT NOT NULL
                            REFERENCES artifacts(digest),
                        status TEXT NOT NULL,
                        resolution TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(run_id, task_id, attempt),
                        CHECK(attempt > 0),
                        CHECK(status IN (
                            'prepared', 'committed', 'rolled_back'
                        ))
                    );
                    CREATE INDEX source_transactions_run_status
                        ON source_transactions(run_id, status, created_at, id);
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (7, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 8 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE previews
                    ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}';
                    ALTER TABLE previews
                    ADD COLUMN operation_owner_token TEXT;
                    ALTER TABLE previews
                    ADD COLUMN operation_lease_expires_at TEXT;
                    CREATE INDEX previews_status_updated
                        ON previews(status, updated_at, id);
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (8, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 9 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE generation_memos (
                        cache_key TEXT PRIMARY KEY,
                        payload_digest TEXT NOT NULL
                            REFERENCES artifacts(digest),
                        node_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        origin_run_id TEXT NOT NULL,
                        origin_task_id TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX generation_memos_origin
                        ON generation_memos(origin_run_id, created_at);
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (9, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 10 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE generation_memos
                    ADD COLUMN project_id TEXT NOT NULL DEFAULT '';
                    CREATE INDEX generation_memos_node
                        ON generation_memos(project_id, node_id);
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (10, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            if 11 not in applied:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE runs ADD COLUMN cancellation_requested_at TEXT;
                    ALTER TABLE runs ADD COLUMN cancellation_reason TEXT;
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (11, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            current = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()["version"]
            if current > SCHEMA_VERSION:
                raise StoreError(
                    f"database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
        finally:
            conn.close()

    def create_project(self, name: str, *, project_id: str | None = None) -> dict[str, Any]:
        project_id = project_id or f"project_{uuid4().hex}"
        now = _now()
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO projects(id, name, current_revision, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (project_id, name, now, now),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"project {project_id!r} does not exist")
        return dict(row)

    def list_projects(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Every project in this state directory, most recently touched first.

        The control plane offered to "create or select a project" and could
        only do the first, because nothing could enumerate them: a second
        project was reachable only by remembering its id.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM projects
                ORDER BY updated_at DESC, id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_revision(
        self,
        project_id: str,
        *,
        kind: str,
        schema_version: str,
        document: Mapping[str, Any],
        expected_revision: int,
    ) -> Revision:
        now = _now()
        revision_id = f"revision_{uuid4().hex}"
        with self._transaction(immediate=True) as conn:
            project = conn.execute(
                "SELECT current_revision FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise NotFoundError(f"project {project_id!r} does not exist")
            actual = int(project["current_revision"])
            if actual != expected_revision:
                raise RevisionConflict(
                    f"project {project_id!r} is at revision {actual}, "
                    f"not expected revision {expected_revision}"
                )
            number = actual + 1
            conn.execute(
                """
                INSERT INTO revisions(
                    id, project_id, number, kind, schema_version, document_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    project_id,
                    number,
                    kind,
                    schema_version,
                    _canonical_json(dict(document)),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE projects
                SET current_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (number, now, project_id),
            )
        return self.get_revision(revision_id)

    def get_revision(self, revision_id: str) -> Revision:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"revision {revision_id!r} does not exist")
        return Revision(
            id=row["id"],
            project_id=row["project_id"],
            number=row["number"],
            kind=row["kind"],
            schema_version=row["schema_version"],
            document=_decode_json(row["document_json"]),
            created_at=row["created_at"],
        )

    def list_revisions(self, project_id: str, *, kind: str | None = None) -> list[Revision]:
        sql = "SELECT id FROM revisions WHERE project_id = ?"
        params: list[Any] = [project_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY number"
        with self._connect() as conn:
            ids = [row["id"] for row in conn.execute(sql, params)]
        return [self.get_revision(revision_id) for revision_id in ids]

    def create_run(
        self,
        project_id: str,
        *,
        spec_revision_id: str | None,
        architecture_revision_id: str | None,
        budget: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        status: str = "awaiting_approval",
    ) -> dict[str, Any]:
        run_id = run_id or f"run_{uuid4().hex}"
        _validate_initial_status(status, _RUN_TRANSITIONS, "run")
        now = _now()
        with self._transaction(immediate=True) as conn:
            if conn.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone() is None:
                raise NotFoundError(f"project {project_id!r} does not exist")
            conn.execute(
                """
                INSERT INTO runs(
                    id, project_id, status, spec_revision_id,
                    architecture_revision_id, budget_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    status,
                    spec_revision_id,
                    architecture_revision_id,
                    _canonical_json(dict(budget or {})),
                    now,
                    now,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id!r} does not exist")
        result = dict(row)
        result["budget"] = _decode_json(result.pop("budget_json"))
        return result

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM runs WHERE project_id = ? ORDER BY created_at, id",
                    (project_id,),
                )
            ]
        return [self.get_run(run_id) for run_id in ids]

    def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"run {run_id!r} does not exist")
            _require_execution_owner_if_requested(
                conn,
                run_id=run_id,
                owner_token=owner_token,
            )
            if expected_status is not None and row["status"] != expected_status:
                raise RevisionConflict(
                    f"run {run_id!r} is {row['status']!r}, not {expected_status!r}"
                )
            _validate_transition(
                row["status"], status, _RUN_TRANSITIONS, "run"
            )
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), run_id),
            )
        return self.get_run(run_id)

    def create_task(
        self,
        run_id: str,
        *,
        node_id: str,
        kind: str,
        task_id: str | None = None,
        status: str = "ready",
        cache_key: str | None = None,
        dependency_task_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        task_id = task_id or f"task_{uuid4().hex}"
        _validate_initial_status(status, _TASK_TRANSITIONS, "task")
        dependencies = tuple(dependency_task_ids)
        if any(
            not isinstance(dependency, str) or not dependency
            for dependency in dependencies
        ) or len(set(dependencies)) != len(dependencies):
            raise ValueError("task dependencies must be unique non-empty ids")
        now = _now()
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    id, run_id, node_id, kind, status, attempt,
                    cache_key, created_at, updated_at,
                    dependency_task_ids_json
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    run_id,
                    node_id,
                    kind,
                    status,
                    cache_key,
                    now,
                    now,
                    _canonical_json(list(dependencies)),
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"task {task_id!r} does not exist")
        result = dict(row)
        result["dependency_task_ids"] = tuple(
            _decode_json(result.pop("dependency_task_ids_json"))
        )
        return result

    def list_tasks(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
        tasks = []
        for row in rows:
            task = dict(row)
            task["dependency_task_ids"] = tuple(
                _decode_json(task.pop("dependency_task_ids_json"))
            )
            tasks.append(task)
        return tasks

    def set_task_status(
        self,
        task_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        increment_attempt: bool = False,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT run_id, status, attempt FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task {task_id!r} does not exist")
            _require_execution_owner_if_requested(
                conn,
                run_id=str(row["run_id"]),
                owner_token=owner_token,
            )
            if expected_status is not None and row["status"] != expected_status:
                raise RevisionConflict(
                    f"task {task_id!r} is {row['status']!r}, not {expected_status!r}"
                )
            _validate_transition(
                row["status"], status, _TASK_TRANSITIONS, "task"
            )
            attempt = int(row["attempt"]) + (1 if increment_attempt else 0)
            conn.execute(
                """
                UPDATE tasks SET status = ?, attempt = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, attempt, _now(), task_id),
            )
        return self.get_task(task_id)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        created_at = _now()
        with self._transaction(immediate=True) as conn:
            _require_execution_owner_if_requested(
                conn,
                run_id=run_id,
                owner_token=owner_token,
            )
            cursor = conn.execute(
                """
                INSERT INTO events(run_id, task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    event_type,
                    _canonical_json(dict(payload or {})),
                    created_at,
                ),
            )
            sequence = int(cursor.lastrowid)
        return {
            "sequence": sequence,
            "run_id": run_id,
            "task_id": task_id,
            "event_type": event_type,
            "payload": dict(payload or {}),
            "created_at": created_at,
        }

    def list_events(
        self, run_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, after_sequence, limit),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "payload": _decode_json(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def put_artifact(
        self,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        digest = hashlib.sha256(content).hexdigest()
        target = self.artifact_root / digest[:2] / digest[2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _verify_artifact_file(target, digest, len(content))
        else:
            fd, tmp_name = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as tmp_file:
                    tmp_file.write(content)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                os.replace(tmp_name, target)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        now = _now()
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO artifacts(
                    digest, size, media_type, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(digest) DO NOTHING
                """,
                (
                    digest,
                    len(content),
                    media_type,
                    _canonical_json(dict(metadata or {})),
                    now,
                ),
            )
        return self.get_artifact(digest)

    def get_artifact(self, digest: str) -> Artifact:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE digest = ?", (digest,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"artifact {digest!r} does not exist")
        path = self.artifact_root / digest[:2] / digest[2:]
        if not path.is_file():
            raise StoreError(f"artifact {digest!r} metadata exists but content is missing")
        _verify_artifact_file(path, digest, int(row["size"]))
        return Artifact(
            digest=row["digest"],
            size=row["size"],
            media_type=row["media_type"],
            path=path,
            metadata=_decode_json(row["metadata_json"]),
            created_at=row["created_at"],
        )

    def attach_artifact(
        self,
        run_id: str,
        digest: str,
        *,
        role: str,
        task_id: str | None = None,
        owner_token: str | None = None,
    ) -> None:
        with self._transaction(immediate=True) as conn:
            _require_execution_owner_if_requested(
                conn,
                run_id=run_id,
                owner_token=owner_token,
            )
            existing = conn.execute(
                """
                SELECT 1 FROM run_artifacts
                WHERE run_id = ? AND digest = ? AND role = ?
                  AND (
                    task_id = ?
                    OR (task_id IS NULL AND ? IS NULL)
                  )
                """,
                (run_id, digest, role, task_id, task_id),
            ).fetchone()
            if existing is not None:
                return
            conn.execute(
                """
                INSERT INTO run_artifacts(
                    run_id, task_id, digest, role, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, task_id, digest, role, _now()),
            )

    def list_run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ra.run_id, ra.task_id, ra.digest, ra.role, ra.created_at,
                       a.size, a.media_type, a.metadata_json
                FROM run_artifacts AS ra
                JOIN artifacts AS a ON a.digest = ra.digest
                WHERE ra.run_id = ?
                ORDER BY ra.created_at, ra.digest, ra.role
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "digest": row["digest"],
                "role": row["role"],
                "created_at": row["created_at"],
                "size": row["size"],
                "media_type": row["media_type"],
                "metadata": _decode_json(row["metadata_json"]),
            }
            for row in rows
        ]

    def get_generation_memo(self, cache_key: str) -> dict[str, Any] | None:
        """Return a previously generated bundle for byte-identical inputs.

        The memo is keyed by the exact request that would be sent, so a hit
        means the model has already been asked this and nothing has changed.
        The payload is returned for revalidation, never for trust: the caller
        puts it back through the same parser a live response goes through.
        """

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generation_memos WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            artifact = self.get_artifact(row["payload_digest"])
        except (NotFoundError, StoreError):
            # A pruned or corrupt payload means no memo, never a broken run.
            return None
        return {
            "cache_key": row["cache_key"],
            "payload_digest": row["payload_digest"],
            "project_id": row["project_id"],
            "node_id": row["node_id"],
            "provider": row["provider"],
            "model": row["model"],
            "origin_run_id": row["origin_run_id"],
            "origin_task_id": row["origin_task_id"],
            "created_at": row["created_at"],
            "payload": artifact.path.read_bytes(),
        }

    def put_generation_memo(
        self,
        cache_key: str,
        *,
        payload: bytes,
        project_id: str,
        node_id: str,
        provider: str,
        model: str,
        run_id: str,
        task_id: str,
    ) -> str:
        """Record one generation against the exact inputs that produced it."""

        if (
            not isinstance(cache_key, str)
            or len(cache_key) != 64
            or any(character not in "0123456789abcdef" for character in cache_key)
        ):
            raise ValueError("cache_key must be a lowercase sha256 digest")
        artifact = self.put_artifact(
            payload,
            media_type="application/vnd.rich.generation-memo+json",
            metadata={
                "cache_key": cache_key,
                "project_id": project_id,
                "node_id": node_id,
            },
        )
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO generation_memos(
                    cache_key, payload_digest, project_id, node_id, provider,
                    model, origin_run_id, origin_task_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO NOTHING
                """,
                (
                    cache_key,
                    artifact.digest,
                    project_id,
                    node_id,
                    provider,
                    model,
                    run_id,
                    task_id,
                    _now(),
                ),
            )
        return artifact.digest

    def forget_generation_memos(self, *, project_id: str, node_id: str) -> int:
        """Drop every memo for one architecture node of one project.

        This is the whole of "rebuild this node and mean it": the next run
        recomputes it and replays its siblings. Scoped by project because
        every project's architecture uses the same layer node ids -- an
        unscoped forget would silently re-buy every other project's work.
        """

        with self._transaction(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM generation_memos WHERE project_id = ? AND node_id = ?",
                (project_id, node_id),
            )
        return cursor.rowcount

    def request_run_cancellation(
        self, run_id: str, *, reason: str = "canceled by operator"
    ) -> dict[str, Any]:
        """Ask a run to stop, durably.

        Durable rather than in-process because the process that started a run
        is often not the process being asked to stop it: the Canvas, the API
        server and the CLI are three writers over one state directory. An
        in-memory flag would cancel whichever one happened to be asked.

        This records the request; the engine observes it at its next
        cancellation checkpoint and unwinds through the paths that already
        exist. Nothing is killed here.
        """

        normalized = str(reason).strip() or "canceled by operator"
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status, cancellation_requested_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"run {run_id!r} does not exist")
            if row["cancellation_requested_at"] is None:
                conn.execute(
                    """
                    UPDATE runs
                    SET cancellation_requested_at = ?, cancellation_reason = ?
                    WHERE id = ?
                    """,
                    (_now(), normalized, run_id),
                )
        return self.get_run(run_id)

    def run_cancellation(self, run_id: str) -> dict[str, Any] | None:
        """Return the standing cancellation request for a run, if any."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cancellation_requested_at, cancellation_reason
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None or row["cancellation_requested_at"] is None:
            return None
        return {
            "requested_at": row["cancellation_requested_at"],
            "reason": row["cancellation_reason"],
        }

    def request_approval(
        self,
        project_id: str,
        *,
        gate: str,
        request: Mapping[str, Any],
        run_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        # An approval is only authority if something later asks for it by the
        # same name. A gate string that matches no known gate would record a
        # decision at a gate nothing ever checks -- an approval that looks
        # granted and authorizes nothing, in the one mechanism that must never
        # fail open.
        try:
            gate = ApprovalGate(gate).value
        except ValueError as exc:
            raise ValueError(
                f"unknown approval gate {gate!r}; expected one of "
                f"{sorted(item.value for item in ApprovalGate)}"
            ) from exc
        approval_id = approval_id or f"approval_{uuid4().hex}"
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO approvals(
                    id, project_id, run_id, gate, status, request_json,
                    decision_json, created_at, decided_at
                ) VALUES (?, ?, ?, ?, 'requested', ?, NULL, ?, NULL)
                """,
                (
                    approval_id,
                    project_id,
                    run_id,
                    gate,
                    _canonical_json(dict(request)),
                    _now(),
                ),
            )
        return self.get_approval(approval_id)

    def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decision: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"approval {approval_id!r} does not exist")
            if row["status"] != "requested":
                raise RevisionConflict(
                    f"approval {approval_id!r} has already been decided"
                )
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, decision_json = ?, decided_at = ?
                WHERE id = ?
                """,
                (
                    "approved" if approved else "rejected",
                    _canonical_json(dict(decision or {})),
                    _now(),
                    approval_id,
                ),
            )
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"approval {approval_id!r} does not exist")
        result = dict(row)
        result["request"] = _decode_json(result.pop("request_json"))
        result["decision"] = _decode_json(result.pop("decision_json"))
        return result

    def list_approvals(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id FROM approvals WHERE project_id = ?"
        params: list[Any] = [project_id]
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at, id"
        with self._connect() as conn:
            ids = [row["id"] for row in conn.execute(sql, params)]
        return [self.get_approval(approval_id) for approval_id in ids]

    def request_preview(
        self,
        run_id: str,
        *,
        request: Mapping[str, Any],
        approval_request: Mapping[str, Any],
        preview_id: str | None = None,
        approval_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically persist a preview request and its human approval gate."""

        preview_id = preview_id or f"preview_{uuid4().hex}"
        approval_id = approval_id or f"approval_{uuid4().hex}"
        now = _now()
        with self._transaction(immediate=True) as conn:
            run = conn.execute(
                "SELECT project_id FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise NotFoundError(f"run {run_id!r} does not exist")
            conn.execute(
                """
                INSERT INTO approvals(
                    id, project_id, run_id, gate, status, request_json,
                    decision_json, created_at, decided_at
                ) VALUES (?, ?, ?, 'preview_deployment', 'requested', ?, NULL, ?, NULL)
                """,
                (
                    approval_id,
                    run["project_id"],
                    run_id,
                    _canonical_json(dict(approval_request)),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO previews(
                    id, run_id, approval_id, status, request_json,
                    result_json, error_json, progress_json, created_at,
                    updated_at, destroyed_at
                ) VALUES (
                    ?, ?, ?, 'awaiting_approval', ?, NULL, NULL, '{}', ?, ?, NULL
                )
                """,
                (
                    preview_id,
                    run_id,
                    approval_id,
                    _canonical_json(dict(request)),
                    now,
                    now,
                ),
            )
        return self.get_preview(preview_id), self.get_approval(approval_id)

    def get_preview(self, preview_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM previews WHERE id = ?", (preview_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"preview {preview_id!r} does not exist")
        result = dict(row)
        result["request"] = _decode_json(result.pop("request_json"))
        result["result"] = _decode_json(result.pop("result_json"))
        result["error"] = _decode_json(result.pop("error_json"))
        result["progress"] = _decode_json(result.pop("progress_json"))
        # Operation tokens are fencing capabilities, not public preview data.
        result.pop("operation_owner_token", None)
        result.pop("operation_lease_expires_at", None)
        return result

    def list_previews(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM previews WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                )
            ]
        return [self.get_preview(preview_id) for preview_id in ids]

    def set_preview_status(
        self,
        preview_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        result: Mapping[str, Any] | None | object = _UNSET,
        error: Mapping[str, Any] | None | object = _UNSET,
        progress: Mapping[str, Any] | object = _UNSET,
        destroyed: bool = False,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, _now()]
        if result is not _UNSET:
            assignments.append("result_json = ?")
            params.append(
                None if result is None else _canonical_json(dict(result))
            )
        if error is not _UNSET:
            assignments.append("error_json = ?")
            params.append(None if error is None else _canonical_json(dict(error)))
        if progress is not _UNSET:
            progress_document = _validated_preview_progress(progress)
            assignments.append("progress_json = ?")
            params.append(_canonical_json(progress_document))
        if destroyed:
            assignments.append("destroyed_at = ?")
            params.append(_now())
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT status, operation_owner_token,
                       operation_lease_expires_at
                FROM previews WHERE id = ?
                """,
                (preview_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"preview {preview_id!r} does not exist")
            if expected_status is not None and row["status"] != expected_status:
                raise RevisionConflict(
                    f"preview {preview_id!r} is {row['status']!r}, "
                    f"not {expected_status!r}"
                )
            _validate_transition(
                row["status"], status, _PREVIEW_TRANSITIONS, "preview"
            )
            _require_preview_owner_if_claimed(
                row,
                preview_id=preview_id,
                owner_token=owner_token,
            )
            if status not in {"deploying", "destroying"}:
                assignments.extend(
                    [
                        "operation_owner_token = NULL",
                        "operation_lease_expires_at = NULL",
                    ]
                )
            params.append(preview_id)
            conn.execute(
                f"UPDATE previews SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
        return self.get_preview(preview_id)

    def claim_preview_operation(
        self,
        preview_id: str,
        *,
        operation: str,
        initial_progress: Mapping[str, Any],
        lease_seconds: float = 1800,
    ) -> PreviewLease:
        """Claim one deploy/destroy operation and fence all of its writes.

        A stale lease may be taken over, but the durable provider intent remains
        untouched.  The successor therefore resumes a known coordinate or
        fails closed instead of blindly repeating an ambiguous create request.
        """

        _validate_lease_seconds(lease_seconds)
        if operation == "deploy":
            resumable_status = "deploying"
            starting_statuses = {"awaiting_approval"}
        elif operation == "destroy":
            resumable_status = "destroying"
            starting_statuses = {
                "ready",
                "destroy_failed",
                "deploying",
                "failed",
            }
        else:
            raise ValueError("preview operation must be 'deploy' or 'destroy'")
        initial_document = _validated_preview_progress(initial_progress)
        owner_token = uuid4().hex
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT status, operation_owner_token,
                       operation_lease_expires_at
                FROM previews WHERE id = ?
                """,
                (preview_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"preview {preview_id!r} does not exist")
            status = str(row["status"])
            if status != resumable_status and status not in starting_statuses:
                raise RevisionConflict(
                    f"preview {preview_id!r} cannot {operation} from "
                    f"status {status!r}"
                )
            current_owner = row["operation_owner_token"]
            current_expiry = _parsed_timestamp(
                row["operation_lease_expires_at"]
            )
            if current_owner and current_expiry > now:
                raise RevisionConflict(
                    f"preview {preview_id!r} already has an active "
                    f"{status!r} owner"
                )
            if status != resumable_status:
                _validate_transition(
                    status,
                    resumable_status,
                    _PREVIEW_TRANSITIONS,
                    "preview",
                )
                progress_assignment = ", progress_json = ?"
                progress_params: list[Any] = [
                    _canonical_json(initial_document)
                ]
            else:
                progress_assignment = ""
                progress_params = []
            conn.execute(
                f"""
                UPDATE previews
                SET status = ?, operation_owner_token = ?,
                    operation_lease_expires_at = ?, error_json = NULL,
                    updated_at = ?{progress_assignment}
                WHERE id = ?
                """,
                [
                    resumable_status,
                    owner_token,
                    expires_at.isoformat(),
                    now.isoformat(),
                    *progress_params,
                    preview_id,
                ],
            )
        return PreviewLease(owner_token, expires_at.isoformat())

    def checkpoint_preview(
        self,
        preview_id: str,
        *,
        expected_status: str,
        progress: Mapping[str, Any],
        owner_token: str,
        lease_seconds: float = 1800,
    ) -> dict[str, Any]:
        """Persist non-secret provider progress under the active operation fence."""

        if expected_status not in {"deploying", "destroying"}:
            raise ValueError("preview checkpoints require an active operation")
        _validate_lease_seconds(lease_seconds)
        document = _validated_preview_progress(progress)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT status, operation_owner_token,
                       operation_lease_expires_at
                FROM previews WHERE id = ?
                """,
                (preview_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"preview {preview_id!r} does not exist")
            if row["status"] != expected_status:
                raise RevisionConflict(
                    f"preview {preview_id!r} is {row['status']!r}, "
                    f"not {expected_status!r}"
                )
            _require_active_preview_owner(
                row,
                preview_id=preview_id,
                owner_token=owner_token,
                now=now,
            )
            conn.execute(
                """
                UPDATE previews
                SET progress_json = ?, updated_at = ?,
                    operation_lease_expires_at = ?
                WHERE id = ? AND operation_owner_token = ?
                """,
                (
                    _canonical_json(document),
                    now.isoformat(),
                    expires_at.isoformat(),
                    preview_id,
                    owner_token,
                ),
            )
        return self.get_preview(preview_id)

    def release_preview_operation(
        self,
        preview_id: str,
        *,
        owner_token: str,
    ) -> bool:
        """Release a failed operation claim without changing durable progress."""

        if not isinstance(owner_token, str) or not owner_token:
            raise ValueError("owner_token cannot be empty")
        with self._transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE previews
                SET operation_owner_token = NULL,
                    operation_lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND operation_owner_token = ?
                """,
                (_now(), preview_id, owner_token),
            )
        return cursor.rowcount == 1

    def list_expired_previews(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return a bounded set of expired previews that may own resources."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("preview reap limit must be between 1 and 1000")
        cutoff = now or datetime.now(timezone.utc)
        if cutoff.tzinfo is None:
            raise ValueError("preview reap time must be timezone-aware")
        cutoff = cutoff.astimezone(timezone.utc)
        with self._connect() as conn:
            ids = [
                str(row["id"])
                for row in conn.execute(
                    """
                    SELECT id
                    FROM previews
                    WHERE status IN (
                        'deploying', 'ready', 'failed',
                        'destroying', 'destroy_failed'
                    )
                    ORDER BY updated_at, id
                    """
                )
            ]
        expired: list[dict[str, Any]] = []
        for preview_id in ids:
            preview = self.get_preview(preview_id)
            expires_at = _parsed_timestamp(
                preview["request"].get("expires_at")
            )
            if expires_at <= cutoff:
                expired.append(preview)
                if len(expired) == limit:
                    break
        return expired

    def claim_idempotency(
        self,
        key: str,
        *,
        operation: str,
        request: Mapping[str, Any],
        lease_seconds: float = 1800,
    ) -> IdempotencyReplay | IdempotencyLease:
        """Claim a mutating request or return its previously completed response."""
        if not key or len(key) > 255:
            raise ValueError("idempotency key must contain 1-255 characters")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or lease_seconds <= 0
            or lease_seconds > 86_400
        ):
            raise ValueError("idempotency lease must be within one day")
        request_digest = hashlib.sha256(
            _canonical_json(dict(request)).encode("utf-8")
        ).hexdigest()
        owner_token = uuid4().hex
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO idempotency_keys(
                        key, operation, request_digest, state, status_code,
                        response_json, created_at, completed_at,
                        owner_token, lease_expires_at
                    ) VALUES (
                        ?, ?, ?, 'in_progress', NULL, NULL, ?, NULL, ?, ?
                    )
                    """,
                    (
                        key,
                        operation,
                        request_digest,
                        now_text,
                        owner_token,
                        expires_at,
                    ),
                )
                return IdempotencyLease(owner_token, expires_at)
            if (
                existing["operation"] != operation
                or existing["request_digest"] != request_digest
            ):
                raise RevisionConflict(
                    f"idempotency key {key!r} was already used for a different request"
                )
            if existing["state"] == "completed":
                return IdempotencyReplay(
                    status_code=int(existing["status_code"]),
                    response=_decode_json(existing["response_json"]),
                )
            try:
                lease_expiry = datetime.fromisoformat(
                    str(existing["lease_expires_at"])
                )
                if lease_expiry.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                lease_expiry = datetime.min.replace(tzinfo=timezone.utc)
            if lease_expiry <= now:
                conn.execute(
                    """
                    UPDATE idempotency_keys
                    SET owner_token = ?, lease_expires_at = ?, created_at = ?
                    WHERE key = ? AND state = 'in_progress'
                    """,
                    (owner_token, expires_at, now_text, key),
                )
                return IdempotencyLease(owner_token, expires_at)
            raise StoreError(f"idempotency key {key!r} is already in progress")

    def claim_run_execution(
        self,
        run_id: str,
        *,
        lease_seconds: float = 60,
    ) -> ExecutionLease:
        """Acquire a fenced, expiring owner lease for one durable run."""

        _validate_lease_seconds(lease_seconds)
        owner_token = uuid4().hex
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction(immediate=True) as conn:
            if conn.execute(
                "SELECT 1 FROM runs WHERE id = ?", (run_id,)
            ).fetchone() is None:
                raise NotFoundError(f"run {run_id!r} does not exist")
            existing = conn.execute(
                """
                SELECT owner_token, lease_expires_at
                FROM run_execution_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO run_execution_leases(
                        run_id, owner_token, acquired_at, lease_expires_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        owner_token,
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
            else:
                current_expiry = _parsed_timestamp(
                    existing["lease_expires_at"]
                )
                if current_expiry > now:
                    raise StoreError(
                        f"run {run_id!r} already has an active execution owner"
                    )
                conn.execute(
                    """
                    UPDATE run_execution_leases
                    SET owner_token = ?, acquired_at = ?, lease_expires_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        owner_token,
                        now.isoformat(),
                        expires_at.isoformat(),
                        run_id,
                    ),
                )
        return ExecutionLease(owner_token, expires_at.isoformat())

    def renew_run_execution(
        self,
        run_id: str,
        *,
        owner_token: str,
        lease_seconds: float = 60,
    ) -> ExecutionLease:
        """Renew only the current fenced execution owner."""

        _validate_lease_seconds(lease_seconds)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE run_execution_leases
                SET lease_expires_at = ?
                WHERE run_id = ? AND owner_token = ? AND lease_expires_at > ?
                """,
                (
                    expires_at.isoformat(),
                    run_id,
                    owner_token,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    f"run {run_id!r} execution ownership was lost"
                )
        return ExecutionLease(owner_token, expires_at.isoformat())

    def is_run_execution_owner(self, run_id: str, owner_token: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT owner_token, lease_expires_at
                FROM run_execution_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return bool(
            row is not None
            and row["owner_token"] == owner_token
            and _parsed_timestamp(row["lease_expires_at"]) > now
        )

    def is_run_execution_leased(self, run_id: str) -> bool:
        """Report whether any live owner holds this run, without naming it.

        The owner token is the capability to write as that owner, so a
        token-free question needs a token-free answer: callers that only want
        to know whether a run is executing -- a status endpoint, say -- must
        not have to hold, or be handed, the right to mutate it."""

        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT lease_expires_at
                FROM run_execution_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return bool(
            row is not None
            and _parsed_timestamp(row["lease_expires_at"]) > now
        )

    def release_run_execution(self, run_id: str, *, owner_token: str) -> bool:
        """Release only this owner; a stale owner cannot release its successor."""

        with self._transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                DELETE FROM run_execution_leases
                WHERE run_id = ? AND owner_token = ?
                """,
                (run_id, owner_token),
            )
        return cursor.rowcount == 1

    def prepare_source_transaction(
        self,
        run_id: str,
        *,
        task_id: str,
        attempt: int,
        owner_token: str,
        journal_digest: str,
        generated_digest: str,
    ) -> dict[str, Any]:
        """Durably bind rollback and intended bytes before source mutation.

        Preparation is idempotent only for the exact same attempt and CAS
        objects.  It is fenced by both the active run lease and the task's
        current running attempt.
        """

        _validate_source_transaction_arguments(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            owner_token=owner_token,
            journal_digest=journal_digest,
            generated_digest=generated_digest,
        )
        # Re-hash both immutable inputs before a database row can authorize
        # their later use during recovery or commit.
        self.get_artifact(journal_digest)
        self.get_artifact(generated_digest)
        transaction_id = _source_transaction_id(run_id, task_id, attempt)
        now = _now()
        with self._transaction(immediate=True) as conn:
            _require_active_execution_owner(
                conn, run_id=run_id, owner_token=owner_token
            )
            task = conn.execute(
                """
                SELECT run_id, status, attempt
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task {task_id!r} does not exist")
            if (
                task["run_id"] != run_id
                or task["status"] != "running"
                or int(task["attempt"]) != attempt
            ):
                raise RevisionConflict(
                    "source transaction does not match the active task attempt"
                )
            existing = conn.execute(
                """
                SELECT * FROM source_transactions
                WHERE run_id = ? AND task_id = ? AND attempt = ?
                """,
                (run_id, task_id, attempt),
            ).fetchone()
            if existing is not None:
                if (
                    existing["id"] == transaction_id
                    and existing["status"] == "prepared"
                    and existing["prepared_owner_token"] == owner_token
                    and existing["journal_digest"] == journal_digest
                    and existing["generated_digest"] == generated_digest
                ):
                    return _source_transaction_record(existing)
                raise RevisionConflict(
                    "source transaction attempt is already bound to different "
                    "bytes, ownership, or resolution"
                )
            conn.execute(
                """
                INSERT INTO source_transactions(
                    id, run_id, task_id, attempt, prepared_owner_token,
                    resolved_owner_token, journal_digest, generated_digest,
                    status, resolution, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'prepared', NULL, ?, ?)
                """,
                (
                    transaction_id,
                    run_id,
                    task_id,
                    attempt,
                    owner_token,
                    journal_digest,
                    generated_digest,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO events(
                    run_id, task_id, event_type, payload_json, created_at
                ) VALUES (?, ?, 'source.transaction.prepared', ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    _canonical_json(
                        {
                            "attempt": attempt,
                            "transaction_id": transaction_id,
                            "journal_digest": journal_digest,
                            "generated_digest": generated_digest,
                        }
                    ),
                    now,
                ),
            )
        return self.get_source_transaction(transaction_id)

    def commit_source_transaction(
        self,
        run_id: str,
        *,
        task_id: str,
        attempt: int,
        owner_token: str,
    ) -> dict[str, Any]:
        """Atomically authorize generated bytes and resolve their journal."""

        _validate_source_transaction_arguments(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            owner_token=owner_token,
        )
        transaction_id = _source_transaction_id(run_id, task_id, attempt)
        now = _now()
        with self._transaction(immediate=True) as conn:
            _require_active_execution_owner(
                conn, run_id=run_id, owner_token=owner_token
            )
            row = conn.execute(
                "SELECT * FROM source_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"source transaction {transaction_id!r} does not exist"
                )
            if (
                row["run_id"] != run_id
                or row["task_id"] != task_id
                or int(row["attempt"]) != attempt
            ):
                raise RevisionConflict(
                    "source transaction identity does not match its attempt"
                )
            if row["status"] == "committed":
                if row["resolved_owner_token"] != owner_token:
                    raise RevisionConflict(
                        "source transaction was committed by another owner"
                    )
                return _source_transaction_record(row)
            if (
                row["status"] != "prepared"
                or row["prepared_owner_token"] != owner_token
            ):
                raise RevisionConflict(
                    "source transaction is not prepared by the active owner"
                )
            task = conn.execute(
                """
                SELECT run_id, status, attempt
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if (
                task is None
                or task["run_id"] != run_id
                or task["status"] != "running"
                or int(task["attempt"]) != attempt
            ):
                raise RevisionConflict(
                    "source transaction commit does not match the active task "
                    "attempt"
                )
            existing_attachment = conn.execute(
                """
                SELECT 1 FROM run_artifacts
                WHERE run_id = ? AND task_id = ? AND digest = ?
                  AND role = 'generated-source'
                """,
                (run_id, task_id, row["generated_digest"]),
            ).fetchone()
            if existing_attachment is None:
                conn.execute(
                    """
                    INSERT INTO run_artifacts(
                        run_id, task_id, digest, role, created_at
                    ) VALUES (?, ?, ?, 'generated-source', ?)
                    """,
                    (run_id, task_id, row["generated_digest"], now),
                )
            conn.execute(
                """
                UPDATE source_transactions
                SET status = 'committed', resolution = 'applied',
                    resolved_owner_token = ?, updated_at = ?
                WHERE id = ? AND status = 'prepared'
                """,
                (owner_token, now, transaction_id),
            )
            conn.execute(
                """
                INSERT INTO events(
                    run_id, task_id, event_type, payload_json, created_at
                ) VALUES (?, ?, 'source.transaction.committed', ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    _canonical_json(
                        {
                            "attempt": attempt,
                            "transaction_id": transaction_id,
                            "generated_digest": row["generated_digest"],
                        }
                    ),
                    now,
                ),
            )
        return self.get_source_transaction(transaction_id)

    def rollback_source_transaction(
        self,
        run_id: str,
        *,
        task_id: str,
        attempt: int,
        owner_token: str,
        reason: str,
    ) -> dict[str, Any]:
        """Resolve a prepared transaction only after its bytes were restored."""

        _validate_source_transaction_arguments(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            owner_token=owner_token,
        )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValueError("source transaction rollback reason is invalid")
        reason = reason.strip()
        transaction_id = _source_transaction_id(run_id, task_id, attempt)
        now = _now()
        with self._transaction(immediate=True) as conn:
            _require_active_execution_owner(
                conn, run_id=run_id, owner_token=owner_token
            )
            row = conn.execute(
                "SELECT * FROM source_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"source transaction {transaction_id!r} does not exist"
                )
            if row["status"] == "rolled_back":
                return _source_transaction_record(row)
            if row["status"] != "prepared":
                raise RevisionConflict(
                    "committed source transaction cannot be rolled back as "
                    "unrecorded source"
                )
            conn.execute(
                """
                UPDATE source_transactions
                SET status = 'rolled_back', resolution = ?,
                    resolved_owner_token = ?, updated_at = ?
                WHERE id = ? AND status = 'prepared'
                """,
                (reason, owner_token, now, transaction_id),
            )
            conn.execute(
                """
                INSERT INTO events(
                    run_id, task_id, event_type, payload_json, created_at
                ) VALUES (?, ?, 'source.transaction.rolled_back', ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    _canonical_json(
                        {
                            "attempt": attempt,
                            "transaction_id": transaction_id,
                            "reason": reason,
                        }
                    ),
                    now,
                ),
            )
        return self.get_source_transaction(transaction_id)

    def get_source_transaction(self, transaction_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"source transaction {transaction_id!r} does not exist"
            )
        return _source_transaction_record(row)

    def list_source_transactions(
        self,
        run_id: str,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in {
            "prepared",
            "committed",
            "rolled_back",
        }:
            raise ValueError("unknown source transaction status")
        query = "SELECT * FROM source_transactions WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if status is not None:
            query += " AND status = ?"
            parameters.append(status)
        query += " ORDER BY created_at, id"
        with self._connect() as conn:
            rows = conn.execute(query, parameters).fetchall()
        return [_source_transaction_record(row) for row in rows]

    def complete_idempotency(
        self,
        key: str,
        *,
        owner_token: str,
        status_code: int,
        response: Mapping[str, Any],
    ) -> None:
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT state, owner_token FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "in_progress"
                or row["owner_token"] != owner_token
            ):
                raise StoreError(f"idempotency key {key!r} is not an active claim")
            conn.execute(
                """
                UPDATE idempotency_keys
                SET state = 'completed', status_code = ?, response_json = ?,
                    completed_at = ?, lease_expires_at = NULL
                WHERE key = ? AND owner_token = ?
                """,
                (
                    status_code,
                    _canonical_json(dict(response)),
                    _now(),
                    key,
                    owner_token,
                ),
            )

    def abandon_idempotency(self, key: str, *, owner_token: str) -> None:
        """Release a failed in-progress claim so a corrected retry can run."""
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                DELETE FROM idempotency_keys
                WHERE key = ? AND state = 'in_progress' AND owner_token = ?
                """,
                (key, owner_token),
            )


def _validate_source_transaction_arguments(
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    owner_token: str,
    journal_digest: str | None = None,
    generated_digest: str | None = None,
) -> None:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id cannot be empty")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id cannot be empty")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if not isinstance(owner_token, str) or not owner_token:
        raise ValueError("owner_token cannot be empty")
    for label, value in (
        ("journal", journal_digest),
        ("generated", generated_digest),
    ):
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{label} digest must be lowercase sha256")


def _source_transaction_id(run_id: str, task_id: str, attempt: int) -> str:
    identity = _canonical_json(
        {"run_id": run_id, "task_id": task_id, "attempt": attempt}
    ).encode("utf-8")
    return f"source_tx_{hashlib.sha256(identity).hexdigest()}"


def _require_active_execution_owner(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    owner_token: str,
) -> None:
    row = conn.execute(
        """
        SELECT owner_token, lease_expires_at
        FROM run_execution_leases
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if (
        row is None
        or row["owner_token"] != owner_token
        or _parsed_timestamp(row["lease_expires_at"])
        <= datetime.now(timezone.utc)
    ):
        raise RevisionConflict(
            f"run {run_id!r} execution ownership was lost"
        )


def _require_execution_owner_if_requested(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    owner_token: str | None,
) -> None:
    """Fence one authoritative mutation when execution ownership applies.

    Control-plane and fixture writes performed outside an execution lease keep
    their existing owner-neutral path.  Once a scheduler opts into fencing,
    every mutation supplies its exact token and validates it in the same
    ``BEGIN IMMEDIATE`` transaction as the write.
    """

    if owner_token is None:
        return
    if not isinstance(owner_token, str) or not owner_token:
        raise ValueError("owner_token cannot be empty")
    _require_active_execution_owner(
        conn,
        run_id=run_id,
        owner_token=owner_token,
    )


def _require_active_preview_owner(
    row: Mapping[str, Any],
    *,
    preview_id: str,
    owner_token: str,
    now: datetime | None = None,
) -> None:
    if not isinstance(owner_token, str) or not owner_token:
        raise ValueError("owner_token cannot be empty")
    current_time = now or datetime.now(timezone.utc)
    if (
        row["operation_owner_token"] != owner_token
        or _parsed_timestamp(row["operation_lease_expires_at"])
        <= current_time
    ):
        raise RevisionConflict(
            f"preview {preview_id!r} operation ownership was lost"
        )


def _require_preview_owner_if_claimed(
    row: Mapping[str, Any],
    *,
    preview_id: str,
    owner_token: str | None,
) -> None:
    claimed = bool(row["operation_owner_token"])
    if owner_token is None:
        if claimed:
            raise RevisionConflict(
                f"preview {preview_id!r} mutation requires its operation owner"
            )
        return
    _require_active_preview_owner(
        row,
        preview_id=preview_id,
        owner_token=owner_token,
    )


def _validated_preview_progress(
    progress: Mapping[str, Any] | object,
) -> dict[str, Any]:
    """Reject credentials and bound the durable recovery checkpoint."""

    if not isinstance(progress, Mapping):
        raise ValueError("preview progress must be an object")
    document = dict(progress)
    forbidden_keys = {
        "authorization",
        "connection_uri",
        "database_url",
        "password",
        "secret",
        "token",
    }

    def inspect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValueError("preview progress keys must be strings")
                normalized = key.lower()
                if normalized in forbidden_keys or normalized.endswith(
                    ("_password", "_secret", "_token")
                ):
                    raise ValueError(
                        "preview progress cannot contain credentials"
                    )
                inspect(nested)
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                inspect(nested)
            return
        if isinstance(value, str) and (
            "postgres://" in value.lower()
            or "postgresql://" in value.lower()
        ):
            raise ValueError(
                "preview progress cannot contain a database connection URI"
            )
        if value is not None and not isinstance(
            value, (str, int, float, bool)
        ):
            raise ValueError("preview progress contains an unsupported value")

    inspect(document)
    serialized = _canonical_json(document)
    if len(serialized.encode("utf-8")) > 65_536:
        raise ValueError("preview progress exceeds its size limit")
    return document


def _source_transaction_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "attempt": int(row["attempt"]),
        "prepared_owner_token": row["prepared_owner_token"],
        "resolved_owner_token": row["resolved_owner_token"],
        "journal_digest": row["journal_digest"],
        "generated_digest": row["generated_digest"],
        "status": row["status"],
        "resolution": row["resolution"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _verify_artifact_file(path: Path, expected_digest: str, expected_size: int) -> None:
    """Fail closed if immutable CAS content was lost or modified."""

    try:
        actual_size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StoreError(
            f"artifact {expected_digest!r} could not be verified"
        ) from exc
    actual_digest = digest.hexdigest()
    if actual_size != expected_size or actual_digest != expected_digest:
        raise StoreError(
            f"artifact {expected_digest!r} failed immutable content verification"
        )


def _validate_initial_status(
    status: str,
    transitions: Mapping[str, set[str]],
    label: str,
) -> None:
    if status not in transitions:
        raise ValueError(f"unknown {label} status {status!r}")


def _validate_transition(
    current: str,
    target: str,
    transitions: Mapping[str, set[str]],
    label: str,
) -> None:
    _validate_initial_status(target, transitions, label)
    if target == current:
        return
    allowed = transitions.get(current)
    if allowed is None:
        raise StoreError(f"stored {label} status {current!r} is invalid")
    if target not in allowed:
        raise RevisionConflict(
            f"invalid {label} status transition: {current!r} -> {target!r}"
        )
