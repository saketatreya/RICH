from datetime import datetime, timedelta, timezone
import io
import json
import zipfile

import pytest

from richbuild.preview import (
    DeploymentFile,
    HttpResponse,
    MigrationDigest,
    MigrationReport,
    NeonPreviewDatabaseAdapter,
    PreviewError,
    PreviewOrchestrator,
    PreviewRequest,
    PreviewResult,
    ProviderApiError,
    SourceChangedSinceApproval,
    SqlMigrationRunner,
    UnsafeSourceTree,
    VercelPreviewDeploymentAdapter,
    _database_coordinates,
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
        if "/v1/files" in url:
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
    def __init__(self, *, failure=None, journal=None):
        self.database_urls = []
        self.expected = []
        self.failure = failure
        self.journal = journal

    def migrate(self, source_dir, *, database_url, expected_migration_digests=()):
        self.database_urls.append(database_url)
        self.expected.append(tuple(expected_migration_digests))
        if self.failure:
            raise self.failure
        return MigrationReport(
            journal=(
                tuple(expected_migration_digests)
                if self.journal is None
                else self.journal
            ),
            server_version="PostgreSQL 17.2 on x86_64-pc-linux-gnu",
        )


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
        if "/v1/files" in url and method == "POST"
    ]
    assert b"DATABASE_URL=must-not-upload" not in uploaded_bodies


def test_migration_mutations_are_disposable_and_never_enter_approved_upload(tmp_path):
    class MutatingMigrations:
        def __init__(self):
            self.source_dir = None

        def migrate(self, source_dir, *, database_url, expected_migration_digests=()):
            self.source_dir = source_dir
            (source_dir / "package.json").write_text(
                '{"name":"migration-mutated"}'
            )
            (source_dir / "generated-by-migration.txt").write_text(
                "unapproved"
            )
            return MigrationReport(journal=())

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
        if "/v1/files" in url and method == "POST"
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
    """Enough of a cursor to hold a journal: inserts are remembered, the
    journal query answers with them, and version() answers like Neon."""

    def __init__(self, *, preexisting=()):
        self.calls = []
        self.next_row = None
        self.journal = list(preexisting)
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        self._last = statement
        if "INSERT INTO public.__rich_migrations" in statement:
            self.journal.append(tuple(parameters))

    def fetchone(self):
        if "SELECT version()" in self._last:
            return ("PostgreSQL 17.2 on x86_64-pc-linux-gnu, compiled by gcc",)
        if "WHERE filename" in self._last:
            return None
        return self.next_row

    def fetchall(self):
        return sorted(self.journal)


class RecordingConnection:
    def __init__(self, *, preexisting=()):
        self.cursor_instance = RecordingCursor(preexisting=preexisting)
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
    def __init__(self, *, preexisting=()):
        self.calls = []
        self.connection = RecordingConnection(preexisting=preexisting)

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

    report = SqlMigrationRunner(connect=connector).migrate(
        source,
        database_url="postgresql://owner:secret@ep-test.neon.tech/app",
        expected_migration_digests=_digests(source),
    )

    assert report.journal == _digests(source)
    assert report.server_major == 17
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
    (source / "packages/db/migrations/0000_initial.sql").write_text("SELECT 1;\n")

    with pytest.raises(PreviewError) as captured:
        SqlMigrationRunner(connect=CredentialLeakingConnector()).migrate(
            source,
            database_url=(
                "postgresql://owner:highly-secret@ep-test.neon.tech/app"
            ),
            expected_migration_digests=_digests(source),
        )

    assert "highly-secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_database_coordinates_decode_percent_encoded_role_and_database():
    database, role = _database_coordinates(
        "postgresql://owner%40team:secret@ep-test.neon.tech/app%20db"
    )

    assert database == "app db"
    assert role == "owner@team"


def test_database_coordinates_ignore_query_parameters():
    database, role = _database_coordinates(
        "postgresql://owner:secret@ep-test.neon.tech/app"
        "?sslmode=require&options=project%3Dep-test"
    )

    assert database == "app"
    assert role == "owner"


@pytest.mark.parametrize(
    "connection_uri",
    [
        "postgresql://owner:secret@ep-test.neon.tech/",
        "postgresql://owner:secret@ep-test.neon.tech",
        "postgresql://ep-test.neon.tech/app",
        "postgresql://:secret@ep-test.neon.tech/app",
    ],
    ids=["empty-path", "no-path", "no-role", "blank-role"],
)
def test_database_coordinates_reject_incomplete_uris(connection_uri):
    with pytest.raises(ProviderApiError):
        _database_coordinates(connection_uri)


def test_database_coordinates_never_echo_the_connection_uri():
    with pytest.raises(ProviderApiError) as captured:
        _database_coordinates("postgresql://ep-test.neon.tech/app?token=highly-secret")

    assert "highly-secret" not in str(captured.value)
    assert "ep-test.neon.tech" not in str(captured.value)


# --------------------------------------------------------------------------
# Parity: the migration that passed the gate is the migration that is applied
# --------------------------------------------------------------------------


def _digests(source):
    from richbuild.preview import migration_digests

    return migration_digests(source)


def _migrated_source(tmp_path):
    source = _source(tmp_path)
    migrations = source / "packages/db/migrations"
    migrations.mkdir(parents=True)
    (migrations / "0000_initial.sql").write_text(
        'CREATE TABLE IF NOT EXISTS "todos" (id uuid PRIMARY KEY);\n'
    )
    (migrations / "0001_titles.sql").write_text(
        'ALTER TABLE "todos" ADD COLUMN title text;\n'
        "--> statement-breakpoint\n"
        'CREATE INDEX todos_title ON "todos"(title);\n'
    )
    return source


def test_preview_migrations_refuse_a_source_that_is_not_the_verified_set(tmp_path):
    source = _migrated_source(tmp_path)
    connector = RecordingConnector()
    verified = _digests(source)
    drifted = (verified[0], MigrationDigest(verified[1].file, "0" * 64))

    # Before any connection: nothing is applied to a database that would hold
    # a schema the run never verified.
    with pytest.raises(PreviewError, match="not the set the run verified"):
        SqlMigrationRunner(connect=connector).migrate(
            source,
            database_url="postgresql://owner:secret@ep-test.neon.tech/app",
            expected_migration_digests=drifted,
        )
    with pytest.raises(PreviewError, match="not the set the run verified"):
        SqlMigrationRunner(connect=connector).migrate(
            source,
            database_url="postgresql://owner:secret@ep-test.neon.tech/app",
            expected_migration_digests=verified[:1],
        )
    # A run that recorded no set cannot have its source's migrations applied.
    with pytest.raises(PreviewError, match="not the set the run verified"):
        SqlMigrationRunner(connect=connector).migrate(
            source,
            database_url="postgresql://owner:secret@ep-test.neon.tech/app",
        )
    assert connector.calls == []

    # And the other way round: a run that verified migrations the source lost.
    bare = _source(tmp_path / "bare")
    with pytest.raises(PreviewError, match="does not carry"):
        SqlMigrationRunner(connect=connector).migrate(
            bare,
            database_url="postgresql://owner:secret@ep-test.neon.tech/app",
            expected_migration_digests=verified,
        )
    assert SqlMigrationRunner(connect=connector).migrate(
        bare, database_url="postgresql://owner:secret@ep-test.neon.tech/app"
    ) == MigrationReport(journal=())
    assert connector.calls == []


def test_preview_migrations_refuse_a_journal_that_differs_after_apply(tmp_path):
    source = _migrated_source(tmp_path)
    # A parent branch that already carried a migration this run never saw.
    connector = RecordingConnector(preexisting=[("0000_other.sql", "f" * 64)])

    with pytest.raises(PreviewError, match="journal after apply"):
        SqlMigrationRunner(connect=connector).migrate(
            source,
            database_url="postgresql://owner:secret@ep-test.neon.tech/app",
            expected_migration_digests=_digests(source),
        )

    assert connector.connection.commits == 0, "refused before commit"


def test_preview_migrations_record_the_journal_and_the_server_on_success(tmp_path):
    source = _migrated_source(tmp_path)
    connector = RecordingConnector()

    report = SqlMigrationRunner(connect=connector).migrate(
        source,
        database_url="postgresql://owner:secret@ep-test.neon.tech/app",
        expected_migration_digests=_digests(source),
    )

    assert report.journal == _digests(source)
    assert [entry.file for entry in report.journal] == ["0000_initial.sql", "0001_titles.sql"]
    assert report.server_version.startswith("PostgreSQL 17.2")
    assert report.server_major == 17
    statements = [s for s, _ in connector.connection.cursor_instance.calls]
    assert any("CREATE INDEX todos_title" in s for s in statements)
    assert statements[-1] == "SELECT version()"
    assert connector.connection.commits == 1


def test_the_orchestrator_holds_the_preview_to_the_run_s_recorded_set(tmp_path):
    from richbuild.preview import server_major

    transport = FakeTransport()
    source = _migrated_source(tmp_path)
    recorded = _digests(source)
    migrations = FakeMigrations()
    events = []
    orchestrator = PreviewOrchestrator(
        NeonPreviewDatabaseAdapter(transport),
        VercelPreviewDeploymentAdapter(transport),
        migrations,
        secret_resolver=lambda handle: f"secret-for-{handle}",
        event_sink=lambda event, payload: events.append((event, dict(payload))),
    )
    request = PreviewRequest(
        run_id="run-1",
        source_dir=source,
        project_name="preview-app",
        neon_project_id="neon-project-1",
        neon_branch_name="preview/run-1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        migration_digests=recorded,
        gate_engine="PostgreSQL 18.3 (PGlite 0.5.8) on wasm32-unknown-emscripten",
    )

    orchestrator.create(request)

    assert migrations.expected == [recorded]
    migrated = next(payload for event, payload in events if event == "preview.database.migrated")
    assert migrated["migrations"] == [entry.as_dict() for entry in recorded]
    # Both sides, for the record: the engine that verified the text and the
    # engine now holding it. Recorded, not compared -- a major mismatch is a
    # fact about the deployment target, not a failure of the migration.
    assert migrated["gate_server_major"] == 18
    assert migrated["preview_server_major"] == 17
    assert migrated["preview_server_version"].startswith("PostgreSQL 17.2")
    assert server_major("nonsense") is None

    # A runner whose journal disagrees fails the preview, and the branch goes.
    disagreeing = FakeMigrations(journal=recorded[:1])
    orchestrator = PreviewOrchestrator(
        NeonPreviewDatabaseAdapter(FakeTransport()),
        VercelPreviewDeploymentAdapter(FakeTransport()),
        disagreeing,
        secret_resolver=lambda handle: f"secret-for-{handle}",
    )
    with pytest.raises(PreviewError, match="not the set the run verified"):
        orchestrator.create(request)


def test_a_preview_request_only_carries_digest_entries():
    with pytest.raises(ValueError, match="MigrationDigest"):
        PreviewRequest(
            run_id="run-1",
            source_dir=".",
            project_name="preview-app",
            neon_project_id="neon-project-1",
            neon_branch_name="preview/run-1",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            migration_digests=({"file": "0000_initial.sql", "sha256": "0" * 64},),
        )
