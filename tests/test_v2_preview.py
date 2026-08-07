from datetime import datetime, timedelta, timezone
import io
import json
import zipfile

import pytest

from rich_v2.preview import (
    DeploymentFile,
    HttpResponse,
    NeonPreviewDatabaseAdapter,
    PreviewError,
    PreviewOrchestrator,
    PreviewRequest,
    PreviewResult,
    SourceChangedSinceApproval,
    SqlMigrationRunner,
    UnsafeSourceTree,
    VercelPreviewDeploymentAdapter,
    collect_deployment_files,
    create_deployment_snapshot,
    extract_deployment_snapshot,
)


class FakeTransport:
    def __init__(self):
        self.requests = []

    def request(self, method, url, *, headers, body=None, timeout=60):
        self.requests.append((method, url, dict(headers), body))
        if "console.neon.tech" in url and method == "POST":
            return HttpResponse(
                201,
                json.dumps(
                    {
                        "branch": {"id": "br-preview", "name": "preview/run-1"},
                        "endpoints": [{"id": "ep-preview"}],
                        "connection_uris": [
                            {
                                "connection_uri": (
                                    "postgresql://owner:db-secret@"
                                    "ep-preview.neon.tech/neondb"
                                )
                            }
                        ],
                    }
                ).encode(),
            )
        if "console.neon.tech" in url and method == "DELETE":
            return HttpResponse(204, b"")
        if "/v2/files" in url:
            return HttpResponse(200, b"{}")
        if "/v13/deployments" in url and method == "POST":
            return HttpResponse(
                201,
                b'{"id":"dpl_preview","url":"preview.example.vercel.app",'
                b'"readyState":"QUEUED"}',
            )
        if "/v13/deployments/dpl_preview" in url and method == "GET":
            return HttpResponse(
                200,
                b'{"id":"dpl_preview","url":"preview.example.vercel.app",'
                b'"readyState":"READY"}',
            )
        if "/v13/deployments/dpl_preview" in url and method == "DELETE":
            return HttpResponse(204, b"")
        raise AssertionError((method, url))


class FakeMigrations:
    def __init__(self, *, failure=None):
        self.database_urls = []
        self.failure = failure

    def migrate(self, source_dir, *, database_url):
        self.database_urls.append(database_url)
        if self.failure:
            raise self.failure


def _source(tmp_path):
    source = tmp_path / "source"
    (source / "apps/web").mkdir(parents=True)
    (source / "apps/web/page.tsx").write_text("export default function Page() {}")
    (source / "package.json").write_text('{"name":"preview"}')
    (source / ".env").write_text("DATABASE_URL=must-not-upload")
    return source


def _request(tmp_path):
    return PreviewRequest(
        run_id="run-1",
        source_dir=_source(tmp_path),
        project_name="preview-app",
        neon_project_id="neon-project-1",
        neon_branch_name="preview/run-1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        vercel_team_id="team-1",
    )


def test_preview_orchestrator_creates_migrates_deploys_without_leaking_secrets(tmp_path):
    transport = FakeTransport()
    migrations = FakeMigrations()
    events = []
    orchestrator = PreviewOrchestrator(
        NeonPreviewDatabaseAdapter(transport),
        VercelPreviewDeploymentAdapter(transport),
        migrations,
        secret_resolver={
            "neon.api_token": "neon-secret",
            "vercel.api_token": "vercel-secret",
        }.__getitem__,
        event_sink=lambda event, payload: events.append((event, dict(payload))),
    )

    result = orchestrator.create(_request(tmp_path))

    assert result.preview_url == "https://preview.example.vercel.app"
    assert result.database_branch_id == "br-preview"
    assert "db-secret" not in repr(result)
    assert migrations.database_urls[0].startswith("postgresql://")
    assert all("db-secret" not in repr(payload) for _, payload in events)
    uploaded_bodies = [
        body
        for method, url, _, body in transport.requests
        if "/v2/files" in url and method == "POST"
    ]
    assert b"DATABASE_URL=must-not-upload" not in uploaded_bodies


def test_migration_mutations_are_disposable_and_never_enter_approved_upload(tmp_path):
    class MutatingMigrations:
        def __init__(self):
            self.source_dir = None

        def migrate(self, source_dir, *, database_url):
            self.source_dir = source_dir
            (source_dir / "package.json").write_text(
                '{"name":"migration-mutated"}'
            )
            (source_dir / "generated-by-migration.txt").write_text(
                "unapproved"
            )

    transport = FakeTransport()
    migrations = MutatingMigrations()
    request = _request(tmp_path)
    original_source = request.source_dir
    orchestrator = PreviewOrchestrator(
        NeonPreviewDatabaseAdapter(transport),
        VercelPreviewDeploymentAdapter(transport),
        migrations,
        secret_resolver=lambda handle: f"secret-for-{handle}",
    )

    orchestrator.create(request)

    assert migrations.source_dir != original_source
    assert not migrations.source_dir.exists()
    assert (original_source / "package.json").read_text() == '{"name":"preview"}'
    assert not (original_source / "generated-by-migration.txt").exists()

    uploads = [
        body
        for method, url, _, body in transport.requests
        if "/v2/files" in url and method == "POST"
    ]
    assert b'{"name":"preview"}' in uploads
    assert b'{"name":"migration-mutated"}' not in uploads
    assert b"unapproved" not in uploads
    deployment_request = next(
        json.loads(body)
        for method, url, _, body in transport.requests
        if "/v13/deployments" in url and method == "POST"
    )
    assert "generated-by-migration.txt" not in {
        file["file"] for file in deployment_request["files"]
    }


def test_vercel_revalidates_frozen_approved_bytes_before_upload(tmp_path):
    transport = FakeTransport()
    request = _request(tmp_path)
    changed = DeploymentFile(
        file="package.json",
        sha="0" * 40,
        size=len(b'{"name":"preview"}'),
        content=b'{"name":"preview"}',
    )

    with pytest.raises(SourceChangedSinceApproval, match="digest changed"):
        VercelPreviewDeploymentAdapter(transport).deploy(
            request,
            token="vercel-secret",
            environment={},
            approved_files=(changed,),
        )

    assert transport.requests == []


def test_failed_migration_cleans_up_database_branch(tmp_path):
    transport = FakeTransport()
    orchestrator = PreviewOrchestrator(
        NeonPreviewDatabaseAdapter(transport),
        VercelPreviewDeploymentAdapter(transport),
        FakeMigrations(failure=PreviewError("migration failed")),
        secret_resolver=lambda handle: f"secret-for-{handle}",
    )

    with pytest.raises(PreviewError, match="migration failed"):
        orchestrator.create(_request(tmp_path))

    assert any(
        method == "DELETE" and "console.neon.tech" in url
        for method, url, _, _ in transport.requests
    )
    assert not any(
        "/v13/deployments" in url and method == "POST"
        for method, url, _, _ in transport.requests
    )


def test_destroy_attempts_database_cleanup_even_if_deployment_delete_fails(tmp_path):
    class DeleteFailureTransport(FakeTransport):
        def request(self, method, url, *, headers, body=None, timeout=60):
            if method == "DELETE" and "api.vercel.com" in url:
                self.requests.append((method, url, dict(headers), body))
                raise PreviewError("Vercel delete unavailable")
            return super().request(
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
            )

    transport = DeleteFailureTransport()
    request = _request(tmp_path)
    orchestrator = PreviewOrchestrator(
        NeonPreviewDatabaseAdapter(transport),
        VercelPreviewDeploymentAdapter(transport),
        FakeMigrations(),
        secret_resolver=lambda handle: f"secret-for-{handle}",
    )
    result = PreviewResult(
        run_id=request.run_id,
        provider="vercel",
        deployment_id="dpl_preview",
        preview_url="https://preview.example.vercel.app",
        database_provider="neon",
        database_project_id=request.neon_project_id,
        database_branch_id="br-preview",
        database_branch_name=request.neon_branch_name,
        expires_at=request.expires_at.isoformat(),
    )

    with pytest.raises(PreviewError, match="vercel"):
        orchestrator.destroy(request, result)

    assert any(
        method == "DELETE" and "console.neon.tech" in url
        for method, url, _, _ in transport.requests
    )


def test_source_collection_rejects_symlinks_and_skips_secret_files(tmp_path):
    source = _source(tmp_path)
    (source / "linked").symlink_to(source / "package.json")

    with pytest.raises(UnsafeSourceTree, match="symbolic link"):
        collect_deployment_files(source)

    (source / "linked").unlink()
    files = collect_deployment_files(source)
    assert ".env" not in {file.file for file in files}


def test_preview_expiry_is_bounded(tmp_path):
    with pytest.raises(ValueError, match="30 days"):
        PreviewRequest(
            run_id="run-1",
            source_dir=_source(tmp_path),
            project_name="preview-app",
            neon_project_id="neon-project-1",
            neon_branch_name="preview/run-1",
            expires_at=datetime.now(timezone.utc) + timedelta(days=31),
        )


def test_preview_snapshot_is_deterministic_excludes_secrets_and_extracts(tmp_path):
    source = _source(tmp_path)

    first = create_deployment_snapshot(source)
    second = create_deployment_snapshot(source)
    extracted = extract_deployment_snapshot(first, tmp_path / "extracted")

    assert first == second
    assert (extracted / "package.json").read_text() == '{"name":"preview"}'
    assert not (extracted / ".env").exists()


def test_preview_snapshot_extraction_rejects_traversal(tmp_path):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("../escape", b"owned")

    with pytest.raises(UnsafeSourceTree, match="unsafe path"):
        extract_deployment_snapshot(content.getvalue(), tmp_path / "extracted")

    assert not (tmp_path / "escape").exists()


def test_preview_request_rejects_untrusted_secret_handle(tmp_path):
    with pytest.raises(ValueError, match="unsupported Neon"):
        PreviewRequest(
            run_id="run-1",
            source_dir=_source(tmp_path),
            project_name="preview-app",
            neon_project_id="neon-project-1",
            neon_branch_name="preview/run-1",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            neon_token_handle="env:HOME",
        )


class RecordingCursor:
    def __init__(self):
        self.calls = []
        self.next_row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))

    def fetchone(self):
        return self.next_row


class RecordingConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1


class RecordingConnector:
    def __init__(self):
        self.calls = []
        self.connection = RecordingConnection()

    def __call__(self, database_url, **options):
        self.calls.append((database_url, options))
        return self.connection


def test_trusted_sql_migrations_never_execute_generated_runtime(tmp_path):
    source = _source(tmp_path)
    migrations = source / "packages/db/migrations"
    migrations.mkdir(parents=True)
    (migrations / "0000_initial.sql").write_text(
        "CREATE TABLE example(id bigint);\n"
        "--> statement-breakpoint\n"
        "CREATE INDEX example_id ON example(id);\n"
    )
    generated_runtime = source / "packages/db/src/migrate.ts"
    generated_runtime.parent.mkdir(parents=True)
    generated_runtime.write_text("throw new Error('must never execute');\n")
    connector = RecordingConnector()

    SqlMigrationRunner(connect=connector).migrate(
        source,
        database_url="postgresql://owner:secret@ep-test.neon.tech/app",
    )

    assert len(connector.calls) == 1
    assert connector.calls[0][1]["sslmode"] == "require"
    statements = [
        statement
        for statement, _parameters
        in connector.connection.cursor_instance.calls
    ]
    assert any("CREATE TABLE example" in statement for statement in statements)
    assert any("CREATE INDEX example_id" in statement for statement in statements)
    assert all("must never execute" not in statement for statement in statements)
    assert connector.connection.commits == 1


def test_sql_migrations_reject_non_neon_credentials_before_connect(tmp_path):
    connector = RecordingConnector()

    with pytest.raises(PreviewError, match="Neon database URL"):
        SqlMigrationRunner(connect=connector).migrate(
            _source(tmp_path),
            database_url="postgresql://owner:secret@database.invalid/app",
        )

    assert connector.calls == []


def test_sql_migration_errors_drop_credential_bearing_exception_causes(tmp_path):
    class CredentialLeakingConnector:
        def __call__(self, database_url, **_options):
            raise RuntimeError(f"could not connect with {database_url}")

    source = _source(tmp_path)
    (source / "packages/db/migrations").mkdir(parents=True)

    with pytest.raises(PreviewError) as captured:
        SqlMigrationRunner(connect=CredentialLeakingConnector()).migrate(
            source,
            database_url=(
                "postgresql://owner:highly-secret@ep-test.neon.tech/app"
            ),
        )

    assert "highly-secret" not in str(captured.value)
    assert captured.value.__cause__ is None
