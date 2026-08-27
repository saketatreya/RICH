"""Approval-ready Neon and Vercel preview provider adapters.

Secrets enter only the trusted provider boundary. They are never included in returned
records, durable events, command arguments, or exception messages.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit

from .paths import is_safe_relative_path
from urllib.request import Request, urlopen
import zipfile


NEON_API = "https://console.neon.tech/api/v2"
VERCEL_API = "https://api.vercel.com"
PREVIEW_PROGRESS_SCHEMA = "rich.preview-progress/v1"


class PreviewError(RuntimeError):
    """Base class for preview orchestration errors."""


class ProviderApiError(PreviewError):
    """A provider returned an unsuccessful or malformed response."""


class UnsafeSourceTree(PreviewError):
    """The source tree contains a path unsafe to upload."""


class SourceChangedSinceApproval(PreviewError):
    """The source tree no longer matches the digest a human approved."""


class PreviewRecoveryAmbiguous(PreviewError):
    """A provider may have created a resource before its coordinate was saved."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body or b"{}")
        except json.JSONDecodeError as exc:
            raise ProviderApiError("provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProviderApiError("provider returned a non-object JSON response")
        return value


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float = 60,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Small trusted HTTP seam whose errors deliberately omit request secrets."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float = 60,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read())
        except HTTPError as exc:
            excerpt = exc.read(2_000).decode("utf-8", errors="replace")
            raise ProviderApiError(
                f"provider request failed with HTTP {exc.code}: {_sanitize(excerpt)}"
            ) from exc
        except URLError as exc:
            raise ProviderApiError(
                f"provider request failed before a response: {type(exc.reason).__name__}"
            ) from exc


@dataclass(frozen=True, slots=True)
class NeonBranch:
    project_id: str
    branch_id: str
    branch_name: str
    endpoint_id: str
    connection_uri: str = field(repr=False)
    database_name: str = ""
    role_name: str = ""


@dataclass(frozen=True, slots=True)
class VercelDeployment:
    deployment_id: str
    url: str
    ready_state: str


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    run_id: str
    source_dir: Path
    project_name: str
    neon_project_id: str
    neon_branch_name: str
    expires_at: datetime
    preview_id: str | None = None
    neon_token_handle: str = "neon.api_token"
    vercel_token_handle: str = "vercel.api_token"
    neon_parent_branch_id: str | None = None
    vercel_project_id: str | None = None
    vercel_team_id: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.run_id):
            raise ValueError("run_id is not a stable identifier")
        if self.preview_id is not None and not re.fullmatch(
            r"preview_[0-9a-f]{32}", self.preview_id
        ):
            raise ValueError("preview_id is not a stable preview identifier")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", self.project_name):
            raise ValueError("project_name must be DNS-safe")
        if not re.fullmatch(r"[a-z0-9-]{1,60}", self.neon_project_id):
            raise ValueError("invalid Neon project id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,62}", self.neon_branch_name):
            raise ValueError("invalid Neon branch name")
        source = Path(self.source_dir).resolve()
        if not source.is_dir():
            raise ValueError("preview source_dir must be an existing directory")
        object.__setattr__(self, "source_dir", source)
        if self.expires_at.tzinfo is None:
            raise ValueError("preview expiry must be timezone-aware")
        now = datetime.now(timezone.utc)
        expiry = self.expires_at.astimezone(timezone.utc)
        if expiry <= now or expiry > now + timedelta(days=30):
            raise ValueError("preview expiry must be within the next 30 days")
        object.__setattr__(self, "expires_at", expiry)
        if self.neon_token_handle != "neon.api_token":
            raise ValueError("unsupported Neon secret handle")
        if self.vercel_token_handle != "vercel.api_token":
            raise ValueError("unsupported Vercel secret handle")


@dataclass(frozen=True, slots=True)
class PreviewResult:
    run_id: str
    provider: str
    deployment_id: str
    preview_url: str
    database_provider: str
    database_project_id: str
    database_branch_id: str
    database_branch_name: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class PreviewTeardown:
    """Non-secret provider coordinates needed to destroy a durable preview."""

    run_id: str
    neon_token_handle: str = "neon.api_token"
    vercel_token_handle: str = "vercel.api_token"
    vercel_team_id: str | None = None


class NeonPreviewDatabaseAdapter:
    def __init__(self, transport: HttpTransport):
        self.transport = transport

    def create_branch(self, request: PreviewRequest, *, token: str) -> NeonBranch:
        branch: dict[str, Any] = {
            "name": request.neon_branch_name,
            "expires_at": request.expires_at.isoformat().replace("+00:00", "Z"),
        }
        if request.neon_parent_branch_id:
            branch["parent_id"] = request.neon_parent_branch_id
        response = self.transport.request(
            "POST",
            f"{NEON_API}/projects/{request.neon_project_id}/branches",
            headers=_json_headers(token),
            body=_json_bytes(
                {
                    "branch": branch,
                    "endpoints": [{"type": "read_write"}],
                }
            ),
        )
        if response.status != 201:
            raise ProviderApiError(
                f"Neon branch creation returned HTTP {response.status}"
            )
        document = response.json()
        try:
            branch_record = document["branch"]
            endpoint = document["endpoints"][0]
            connection_uri = document["connection_uris"][0]["connection_uri"]
            branch_id = branch_record["id"]
            branch_name = branch_record["name"]
            endpoint_id = endpoint["id"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderApiError(
                "Neon branch response omitted branch, endpoint, or connection URI"
            ) from exc
        if not isinstance(connection_uri, str) or not connection_uri.startswith(
            ("postgres://", "postgresql://")
        ):
            raise ProviderApiError("Neon returned an invalid connection URI")
        database_name, role_name = _database_coordinates(connection_uri)
        return NeonBranch(
            project_id=request.neon_project_id,
            branch_id=str(branch_id),
            branch_name=str(branch_name),
            endpoint_id=str(endpoint_id),
            connection_uri=connection_uri,
            database_name=database_name,
            role_name=role_name,
        )

    def get_connection_uri(self, branch: NeonBranch, *, token: str) -> str:
        """Resolve a credential only in memory from durable non-secret coordinates."""

        if not branch.database_name or not branch.role_name or not branch.endpoint_id:
            raise PreviewRecoveryAmbiguous(
                "Neon recovery lacks the database, role, or endpoint coordinate"
            )
        query = urlencode(
            {
                "branch_id": branch.branch_id,
                "endpoint_id": branch.endpoint_id,
                "database_name": branch.database_name,
                "role_name": branch.role_name,
            }
        )
        response = self.transport.request(
            "GET",
            f"{NEON_API}/projects/{branch.project_id}/connection_uri?{query}",
            headers=_json_headers(token),
        )
        if response.status != 200:
            raise ProviderApiError(
                f"Neon connection URI lookup returned HTTP {response.status}"
            )
        uri = response.json().get("uri")
        if not isinstance(uri, str) or not uri.startswith(
            ("postgres://", "postgresql://")
        ):
            raise ProviderApiError("Neon returned an invalid connection URI")
        return uri

    def delete_branch(self, branch: NeonBranch, *, token: str) -> None:
        response = self.transport.request(
            "DELETE",
            f"{NEON_API}/projects/{branch.project_id}/branches/{branch.branch_id}",
            headers=_json_headers(token),
        )
        if response.status not in {200, 204, 404}:
            raise ProviderApiError(
                f"Neon branch deletion returned HTTP {response.status}"
            )


@dataclass(frozen=True, slots=True)
class DeploymentFile:
    file: str
    sha: str
    size: int
    content: bytes = field(repr=False, compare=False)

    def api_record(self) -> dict[str, Any]:
        return {"file": self.file, "sha": self.sha, "size": self.size}


class VercelPreviewDeploymentAdapter:
    def __init__(self, transport: HttpTransport):
        self.transport = transport

    def deploy(
        self,
        request: PreviewRequest,
        *,
        token: str,
        environment: Mapping[str, str],
        approved_files: tuple[DeploymentFile, ...] | None = None,
    ) -> VercelDeployment:
        # Preview orchestration supplies the upload set captured before any
        # migration/build preparation.  Keeping the bytes here (rather than
        # rescanning a mutable worktree) closes the approval-to-upload TOCTOU
        # window.  Direct adapter callers retain the convenient source-dir API.
        files = (
            _validate_approved_files(approved_files)
            if approved_files is not None
            else collect_deployment_files(request.source_dir)
        )
        query = _team_query(request.vercel_team_id)
        headers = _json_headers(token)
        for file in files:
            upload = self.transport.request(
                "POST",
                f"{VERCEL_API}/v2/files{query}",
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "x-vercel-digest": file.sha,
                },
                body=file.content,
            )
            if upload.status not in {200, 201}:
                raise ProviderApiError(
                    f"Vercel file upload returned HTTP {upload.status}"
                )

        deployment_body: dict[str, Any] = {
            "name": request.project_name,
            "files": [file.api_record() for file in files],
            "target": "preview",
            "projectSettings": {"framework": "nextjs"},
            "env": dict(environment),
            "build": {"env": dict(environment)},
            "meta": {
                "richRunId": request.run_id,
                **(
                    {"richPreviewId": request.preview_id}
                    if request.preview_id is not None
                    else {}
                ),
            },
        }
        if request.vercel_project_id:
            deployment_body["project"] = request.vercel_project_id
        response = self.transport.request(
            "POST",
            f"{VERCEL_API}/v13/deployments{query}",
            headers=headers,
            body=_json_bytes(deployment_body),
            timeout=120,
        )
        if response.status not in {200, 201}:
            raise ProviderApiError(
                f"Vercel deployment returned HTTP {response.status}"
            )
        return _deployment(response.json())

    def wait_until_ready(
        self,
        deployment: VercelDeployment,
        *,
        token: str,
        team_id: str | None,
        timeout_seconds: float = 600,
        poll_seconds: float = 2,
    ) -> VercelDeployment:
        deadline = time.monotonic() + timeout_seconds
        current = deployment
        while current.ready_state not in {"READY", "ERROR", "CANCELED"}:
            if time.monotonic() >= deadline:
                raise ProviderApiError("Vercel preview did not become ready before timeout")
            time.sleep(poll_seconds)
            response = self.transport.request(
                "GET",
                f"{VERCEL_API}/v13/deployments/{current.deployment_id}"
                f"{_team_query(team_id)}",
                headers=_json_headers(token),
            )
            if response.status != 200:
                raise ProviderApiError(
                    f"Vercel deployment inspection returned HTTP {response.status}"
                )
            current = _deployment(response.json())
        if current.ready_state != "READY":
            raise ProviderApiError(
                f"Vercel preview ended in state {current.ready_state}"
            )
        return current

    def delete(
        self,
        deployment: VercelDeployment,
        *,
        token: str,
        team_id: str | None,
    ) -> None:
        response = self.transport.request(
            "DELETE",
            f"{VERCEL_API}/v13/deployments/{deployment.deployment_id}"
            f"{_team_query(team_id)}",
            headers=_json_headers(token),
        )
        if response.status not in {200, 204, 404}:
            raise ProviderApiError(
                f"Vercel deployment deletion returned HTTP {response.status}"
            )


class MigrationRunner(Protocol):
    def migrate(self, source_dir: Path, *, database_url: str) -> None: ...


class SqlMigrationRunner:
    """Apply bounded SQL without exposing a database credential to generated code."""

    def __init__(
        self,
        *,
        connect: Callable[..., Any] | None = None,
        connect_timeout_seconds: int = 15,
        statement_timeout_seconds: int = 30,
        max_files: int = 128,
        max_file_bytes: int = 1_048_576,
        max_total_bytes: int = 8_388_608,
    ):
        for name, value in (
            ("connect_timeout_seconds", connect_timeout_seconds),
            ("statement_timeout_seconds", statement_timeout_seconds),
            ("max_files", max_files),
            ("max_file_bytes", max_file_bytes),
            ("max_total_bytes", max_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if connect is not None and not callable(connect):
            raise TypeError("connect must be callable")
        self._connect = connect
        self.connect_timeout_seconds = connect_timeout_seconds
        self.statement_timeout_seconds = statement_timeout_seconds
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def migrate(self, source_dir: Path, *, database_url: str) -> None:
        parsed = urlsplit(database_url)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or not parsed.hostname.endswith(".neon.tech")
            or not parsed.username
            or parsed.password is None
            or not parsed.path
            or parsed.path == "/"
            or parsed.fragment
        ):
            raise PreviewError(
                "preview migration requires a credentialed Neon database URL"
            )
        source = Path(source_dir).resolve(strict=True)
        migrations = source / "packages" / "db" / "migrations"
        if not migrations.exists():
            return
        files = self._migration_files(migrations)
        connector = self._connect or _psycopg_connect()
        try:
            with connector(
                database_url,
                connect_timeout=self.connect_timeout_seconds,
                sslmode="require",
                application_name="rich-preview-migration",
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET LOCAL statement_timeout = %s",
                        (self.statement_timeout_seconds * 1_000,),
                    )
                    cursor.execute(
                        "SET LOCAL lock_timeout = %s",
                        (self.statement_timeout_seconds * 1_000,),
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS public.__rich_migrations (
                            filename text PRIMARY KEY,
                            sha256 text NOT NULL,
                            applied_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    for filename, payload in files:
                        digest = hashlib.sha256(payload).hexdigest()
                        cursor.execute(
                            """
                            SELECT sha256
                            FROM public.__rich_migrations
                            WHERE filename = %s
                            """,
                            (filename,),
                        )
                        existing = cursor.fetchone()
                        if existing is not None:
                            if existing[0] != digest:
                                raise PreviewError(
                                    "an applied migration changed content"
                                )
                            continue
                        sql = payload.decode("utf-8")
                        statements = [
                            statement.strip()
                            for statement in sql.split(
                                "--> statement-breakpoint"
                            )
                            if statement.strip()
                        ]
                        if not statements or len(statements) > 512:
                            raise PreviewError(
                                f"migration {filename!r} has invalid statement boundaries"
                            )
                        for statement in statements:
                            cursor.execute(statement)
                        cursor.execute(
                            """
                            INSERT INTO public.__rich_migrations(filename, sha256)
                            VALUES (%s, %s)
                            """,
                            (filename, digest),
                        )
                connection.commit()
        except PreviewError:
            raise
        except Exception as exc:
            raise PreviewError(
                f"trusted SQL migration failed: {type(exc).__name__}"
            ) from None

    def _migration_files(
        self, migrations: Path
    ) -> tuple[tuple[str, bytes], ...]:
        if migrations.is_symlink() or not migrations.is_dir():
            raise PreviewError("migration root must be a regular directory")
        candidates = sorted(
            path
            for path in migrations.iterdir()
            if path.name.endswith(".sql")
        )
        if len(candidates) > self.max_files:
            raise PreviewError("too many SQL migration files")
        result: list[tuple[str, bytes]] = []
        total = 0
        for path in candidates:
            if (
                path.is_symlink()
                or not path.is_file()
                or not re.fullmatch(
                    r"[0-9]{4,}_[a-z0-9][a-z0-9_-]*\.sql",
                    path.name,
                )
            ):
                raise PreviewError(
                    f"unsafe SQL migration filename {path.name!r}"
                )
            payload = path.read_bytes()
            if len(payload) > self.max_file_bytes:
                raise PreviewError(
                    f"SQL migration {path.name!r} exceeds its size limit"
                )
            total += len(payload)
            if total > self.max_total_bytes:
                raise PreviewError("SQL migrations exceed their total size limit")
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PreviewError(
                    f"SQL migration {path.name!r} is not UTF-8"
                ) from exc
            if b"\x00" in payload:
                raise PreviewError(
                    f"SQL migration {path.name!r} contains a NUL byte"
                )
            result.append((path.name, payload))
        return tuple(result)


SecretResolver = Callable[[str], str]
EventSink = Callable[[str, Mapping[str, Any]], None]


class PreviewOrchestrator:
    def __init__(
        self,
        neon: NeonPreviewDatabaseAdapter,
        vercel: VercelPreviewDeploymentAdapter,
        migrations: MigrationRunner,
        *,
        secret_resolver: SecretResolver,
        event_sink: EventSink | None = None,
    ):
        self.neon = neon
        self.vercel = vercel
        self.migrations = migrations
        self.secret_resolver = secret_resolver
        self.event_sink = event_sink or (lambda _event, _payload: None)

    def create(self, request: PreviewRequest) -> PreviewResult:
        neon_token = self._secret(request.neon_token_handle)
        vercel_token = self._secret(request.vercel_token_handle)
        # Capture the complete upload set before any executable preparation
        # runs.  Bytes are immutable and are passed directly to Vercel later.
        # Migrations receive only an extracted disposable copy.
        approved_files = collect_deployment_files(request.source_dir)
        approved_snapshot = _create_deployment_snapshot(approved_files)
        branch: NeonBranch | None = None
        deployment: VercelDeployment | None = None
        try:
            branch = self.neon.create_branch(request, token=neon_token)
            self.event_sink(
                "preview.database.created",
                {
                    "run_id": request.run_id,
                    "provider": "neon",
                    "project_id": branch.project_id,
                    "branch_id": branch.branch_id,
                    "branch_name": branch.branch_name,
                },
            )
            with TemporaryDirectory(prefix="rich-preview-migration-") as temporary:
                migration_source = extract_deployment_snapshot(
                    approved_snapshot,
                    Path(temporary) / "source",
                )
                self.migrations.migrate(
                    migration_source,
                    database_url=branch.connection_uri,
                )
            self.event_sink(
                "preview.database.migrated",
                {"run_id": request.run_id, "branch_id": branch.branch_id},
            )
            deployment = self.vercel.deploy(
                request,
                token=vercel_token,
                environment={"DATABASE_URL": branch.connection_uri},
                approved_files=approved_files,
            )
            deployment = self.vercel.wait_until_ready(
                deployment,
                token=vercel_token,
                team_id=request.vercel_team_id,
            )
            result = PreviewResult(
                run_id=request.run_id,
                provider="vercel",
                deployment_id=deployment.deployment_id,
                preview_url=deployment.url,
                database_provider="neon",
                database_project_id=branch.project_id,
                database_branch_id=branch.branch_id,
                database_branch_name=branch.branch_name,
                expires_at=request.expires_at.isoformat(),
            )
            self.event_sink(
                "preview.ready",
                {
                    "run_id": result.run_id,
                    "deployment_id": result.deployment_id,
                    "preview_url": result.preview_url,
                    "database_branch_id": result.database_branch_id,
                },
            )
            return result
        except Exception:
            if deployment is not None:
                try:
                    self.vercel.delete(
                        deployment,
                        token=vercel_token,
                        team_id=request.vercel_team_id,
                    )
                except Exception:
                    pass
            if branch is not None:
                try:
                    self.neon.delete_branch(branch, token=neon_token)
                except Exception:
                    pass
            raise

    def destroy(
        self,
        request: PreviewRequest | PreviewTeardown,
        result: PreviewResult,
    ) -> None:
        vercel_token = self._secret(request.vercel_token_handle)
        neon_token = self._secret(request.neon_token_handle)
        deployment = VercelDeployment(
            result.deployment_id, result.preview_url, "READY"
        )
        branch = NeonBranch(
            result.database_project_id,
            result.database_branch_id,
            result.database_branch_name,
            endpoint_id="unknown",
            connection_uri="redacted",
        )
        failures: list[str] = []
        try:
            self.vercel.delete(
                deployment, token=vercel_token, team_id=request.vercel_team_id
            )
        except Exception:
            failures.append("vercel")
        try:
            self.neon.delete_branch(branch, token=neon_token)
        except Exception:
            failures.append("neon")
        if failures:
            raise PreviewError(
                "preview cleanup failed for provider(s): "
                + ", ".join(failures)
            )
        self.event_sink(
            "preview.destroyed",
            {
                "run_id": result.run_id,
                "deployment_id": result.deployment_id,
                "database_branch_id": result.database_branch_id,
            },
        )

    def _secret(self, handle: str) -> str:
        value = self.secret_resolver(handle)
        if not value:
            raise PreviewError(f"secret handle {handle!r} resolved to an empty value")
        return value


_EXCLUDED_DIRECTORIES = {
    ".git",
    ".next",
    ".vercel",
    "node_modules",
    "dist",
    "coverage",
    "playwright-report",
    "test-results",
}
_EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    "pnpm-debug.log",
    "npm-debug.log",
}


def collect_deployment_files(
    source_dir: str | Path,
    *,
    max_files: int = 5_000,
    max_total_bytes: int = 100 * 1024 * 1024,
    max_file_bytes: int = 10 * 1024 * 1024,
) -> tuple[DeploymentFile, ...]:
    root = Path(source_dir).resolve(strict=True)
    files: list[DeploymentFile] = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[:2] == (".rich", "runtime"):
            continue
        if any(part in _EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_symlink():
            raise UnsafeSourceTree(f"symbolic link cannot be deployed: {relative}")
        if not path.is_file():
            continue
        if (
            path.name in _EXCLUDED_FILE_NAMES
            or path.name == ".env"
            or path.name.startswith(".env.")
            or path.suffix in {".pem", ".key", ".p12", ".pfx"}
        ):
            continue
        content = path.read_bytes()
        if len(content) > max_file_bytes:
            raise UnsafeSourceTree(f"deployment file is too large: {relative}")
        total += len(content)
        if total > max_total_bytes:
            raise UnsafeSourceTree("deployment source exceeds total size limit")
        if len(files) >= max_files:
            raise UnsafeSourceTree("deployment source exceeds file count limit")
        files.append(
            DeploymentFile(
                file=relative.as_posix(),
                sha=hashlib.sha1(content).hexdigest(),
                size=len(content),
                content=content,
            )
        )
    if not files:
        raise UnsafeSourceTree("deployment source contains no uploadable files")
    return tuple(files)


def deployment_source_digest(source_dir: str | Path) -> str:
    """Return a stable digest for exactly the files that would be uploaded."""

    return _deployment_files_digest(collect_deployment_files(source_dir))


def _deployment_files_digest(files: tuple[DeploymentFile, ...]) -> str:
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.file.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file.sha.encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def create_deployment_snapshot(source_dir: str | Path) -> bytes:
    """Freeze exactly the approved upload set into a deterministic ZIP archive."""

    return _create_deployment_snapshot(collect_deployment_files(source_dir))


def _create_deployment_snapshot(files: tuple[DeploymentFile, ...]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for file in files:
            info = zipfile.ZipInfo(file.file, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file.content, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def _validate_approved_files(
    files: tuple[DeploymentFile, ...],
) -> tuple[DeploymentFile, ...]:
    """Fail closed if an in-memory approved upload set is malformed."""

    if not files:
        raise UnsafeSourceTree("approved deployment source contains no files")
    if len(files) > 5_000:
        raise UnsafeSourceTree("approved deployment source exceeds file count limit")
    seen: set[str] = set()
    previous: str | None = None
    total = 0
    for file in files:
        if not is_safe_relative_path(file.file) or file.file in seen:
            raise UnsafeSourceTree(
                f"approved deployment source contains an unsafe path: {file.file!r}"
            )
        relative = PurePosixPath(file.file)
        if (
            relative.parts[:2] == (".rich", "runtime")
            or any(part in _EXCLUDED_DIRECTORIES for part in relative.parts)
            or relative.name in _EXCLUDED_FILE_NAMES
            or relative.name == ".env"
            or relative.name.startswith(".env.")
            or relative.suffix in {".pem", ".key", ".p12", ".pfx"}
        ):
            raise UnsafeSourceTree(
                f"approved deployment source contains an excluded path: {file.file!r}"
            )
        if previous is not None and file.file <= previous:
            raise UnsafeSourceTree("approved deployment files are not canonical")
        if file.size != len(file.content):
            raise SourceChangedSinceApproval(
                f"approved deployment file size changed: {file.file}"
            )
        if file.sha != hashlib.sha1(file.content).hexdigest():
            raise SourceChangedSinceApproval(
                f"approved deployment file digest changed: {file.file}"
            )
        if file.size > 10 * 1024 * 1024:
            raise UnsafeSourceTree(
                f"approved deployment file is too large: {file.file}"
            )
        total += file.size
        if total > 100 * 1024 * 1024:
            raise UnsafeSourceTree("approved deployment source exceeds total size limit")
        seen.add(file.file)
        previous = file.file
    return files


def extract_deployment_snapshot(
    content: bytes,
    destination: str | Path,
    *,
    max_files: int = 5_000,
    max_total_bytes: int = 100 * 1024 * 1024,
    max_file_bytes: int = 10 * 1024 * 1024,
) -> Path:
    """Extract a trusted CAS snapshot while rechecking every archive boundary."""

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    total = 0
    seen: set[str] = set()
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), mode="r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise UnsafeSourceTree("preview source snapshot is not a valid ZIP") from exc
    with archive:
        entries = archive.infolist()
        if not entries or len(entries) > max_files:
            raise UnsafeSourceTree("preview source snapshot has an invalid file count")
        for entry in entries:
            if (
                entry.is_dir()
                or not is_safe_relative_path(entry.filename)
                or entry.filename in seen
            ):
                raise UnsafeSourceTree(
                    f"preview source snapshot contains an unsafe path: {entry.filename!r}"
                )
            relative = PurePosixPath(entry.filename)
            if entry.flag_bits & 0x1:
                raise UnsafeSourceTree("encrypted preview snapshots are not supported")
            if entry.file_size > max_file_bytes:
                raise UnsafeSourceTree(
                    f"preview source snapshot file is too large: {entry.filename}"
                )
            total += entry.file_size
            if total > max_total_bytes:
                raise UnsafeSourceTree("preview source snapshot is too large")
            seen.add(entry.filename)
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                payload = archive.read(entry)
            except (RuntimeError, zipfile.BadZipFile) as exc:
                raise UnsafeSourceTree(
                    "preview source snapshot content is corrupt"
                ) from exc
            if len(payload) != entry.file_size:
                raise UnsafeSourceTree(
                    f"preview source snapshot size mismatch: {entry.filename}"
                )
            target.write_bytes(payload)
    return root


def environment_secret_resolver(handle: str) -> str:
    """Resolve an allow-listed secret handle from this process' environment."""

    aliases = {
        "neon.api_token": "NEON_API_TOKEN",
        "vercel.api_token": "VERCEL_TOKEN",
    }
    variable = aliases.get(handle)
    if variable is None:
        raise PreviewError(f"unsupported secret handle {handle!r}")
    value = os.environ.get(variable)
    if not value:
        raise PreviewError(
            f"secret handle {handle!r} requires environment variable {variable}"
        )
    return value


def _psycopg_connect() -> Callable[..., Any]:
    """Resolve the trusted SQL driver lazily at the provider boundary."""

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise PreviewError(
            "preview SQL migrations require the psycopg runtime dependency"
        ) from exc
    return psycopg.connect


def default_preview_orchestrator(
    *,
    event_sink: EventSink | None = None,
) -> PreviewOrchestrator:
    """Construct the trusted live provider boundary used by the API and CLI."""

    transport = UrllibTransport()
    return PreviewOrchestrator(
        NeonPreviewDatabaseAdapter(transport),
        VercelPreviewDeploymentAdapter(transport),
        SqlMigrationRunner(),
        secret_resolver=environment_secret_resolver,
        event_sink=event_sink,
    )


def _database_coordinates(connection_uri: str) -> tuple[str, str]:
    """Return the (database, role) a Postgres connection URI encodes.

    These are the durable non-secret coordinates ``get_connection_uri`` later
    replays to re-resolve a credential, so they must round-trip exactly. The
    URI itself never reaches the exception message.
    """

    try:
        split = urlsplit(connection_uri)
    except ValueError as exc:
        raise ProviderApiError("Neon returned an unparsable connection URI") from exc
    database = unquote(split.path).lstrip("/")
    role = unquote(split.username or "")
    if not database or not role:
        raise ProviderApiError(
            "Neon connection URI does not encode a database and role"
        )
    if "/" in database or "\x00" in database or "\x00" in role:
        raise ProviderApiError(
            "Neon connection URI encodes an unusable database or role"
        )
    return database, role


def _deployment(document: Mapping[str, Any]) -> VercelDeployment:
    try:
        deployment_id = str(document["id"])
        url = str(document["url"])
    except KeyError as exc:
        raise ProviderApiError("Vercel response omitted deployment id or URL") from exc
    ready_state = str(
        document.get("readyState") or document.get("state") or "QUEUED"
    ).upper()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return VercelDeployment(deployment_id, url, ready_state)


def _json_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _team_query(team_id: str | None) -> str:
    return f"?{urlencode({'teamId': team_id})}" if team_id else ""


_DATABASE_URL_RE = re.compile(
    r"postgres(?:ql)?://[^\s\"']+",
    re.IGNORECASE,
)


def _sanitize(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    sanitized = _DATABASE_URL_RE.sub("[REDACTED_DATABASE_URL]", value)
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED_SECRET]")
    return sanitized
