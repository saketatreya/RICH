import json
import os
from pathlib import PurePosixPath
from decimal import Decimal

import pytest

import richbuild.coding as coding
from richbuild.budget import BudgetLedger, RunBudget, Usage
from richbuild.coding import (
    ApprovalMismatchError,
    ApprovalWitness,
    AtomicSourceWriter,
    CodingLimits,
    CodingWorker,
    FileBundleValidationError,
    PriorAttemptFailure,
    PromptLimitError,
    SourceTransactionError,
    build_task_prompt,
    file_bundle_schema,
    generation_cache_key,
    parse_file_bundle,
    redact_diagnostics,
)
from richbuild.compiler import compile_architecture
from richbuild.models import (
    AcceptanceScenario,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpec,
    Contract,
    EdgeKind,
    NodeKind,
    OperationContract,
    ProjectSpec,
    Requirement,
)
from richbuild.providers import ModelGateway, ModelResponse


def _fixture():
    project = ProjectSpec(
        id="project.coding",
        name="Coding fixture",
        goal="Render a saved note",
        audiences=("technical founders",),
        requirements=(
            Requirement(
                id="requirement.domain",
                title="Store note",
                statement="A note retains its text.",
            ),
            Requirement(
                id="requirement.web",
                title="Render note",
                statement="The current note is rendered in the web application.",
            ),
        ),
        acceptance_scenarios=(
            AcceptanceScenario(
                id="scenario.note",
                title="Saved note appears",
                given=("A note contains hello.",),
                when=("The founder opens the application.",),
                then=("The application renders hello.",),
                requirement_ids=("requirement.domain", "requirement.web"),
                oracle=(
                    {"action": "navigate", "value": "/"},
                    {
                        "action": "assert_text",
                        "locator": {"kind": "text", "value": "hello"},
                        "value": "hello",
                    },
                ),
            ),
        ),
        constraints=("Use strict TypeScript.",),
    )
    domain = ArchitectureNode(
        id="domain",
        name="Note domain",
        kind=NodeKind.DOMAIN,
        contract_id="contract.domain",
        requirement_ids=("requirement.domain",),
        owned_paths=("packages/domain",),
    )
    web = ArchitectureNode(
        id="web",
        name="Web application",
        kind=NodeKind.APPLICATION,
        contract_id="contract.web",
        requirement_ids=("requirement.web",),
        owned_paths=("apps/web",),
    )
    contracts = (
        Contract(
            id="contract.domain",
            node_id="domain",
            operations=(
                OperationContract(
                    id="operation.domain",
                    name="getNote",
                    input_schema={"type": "object"},
                    output_schema={"type": "string"},
                    requirement_ids=("requirement.domain",),
                ),
            ),
        ),
        Contract(
            id="contract.web",
            node_id="web",
            operations=(
                OperationContract(
                    id="operation.web",
                    name="renderNote",
                    input_schema={"type": "string"},
                    output_schema={"type": "string"},
                    requirement_ids=("requirement.web",),
                ),
            ),
        ),
    )
    architecture = ArchitectureSpec(
        id="architecture.coding",
        project_id=project.id,
        root_node_id="web",
        target_pack="nextjs-app-router",
        nodes=(web, domain),
        edges=(
            ArchitectureEdge(
                id="edge.web.domain",
                kind=EdgeKind.CONTAINS,
                source_node_id="web",
                target_node_id="domain",
            ),
        ),
        contracts=contracts,
    )
    plan = compile_architecture(architecture, project)
    approval = ApprovalWitness(
        project_id=project.id,
        project_revision=project.revision,
        architecture_id=architecture.id,
        architecture_revision=architecture.revision,
    )
    return project, architecture, plan, approval


def _valid_bundle(**overrides):
    bundle = {
        "summary": "Implemented note rendering",
        "files": [
            {
                "operation": "create",
                "path": "apps/web/page.tsx",
                "content": "export default function Page() { return <p>hello</p>; }\n",
            }
        ],
    }
    bundle.update(overrides)
    return bundle


class RecordingProvider:
    name = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return ModelResponse(
            text=json.dumps(self.payload),
            parsed=self.payload,
            provider=self.name,
            model=request.model,
            usage=Usage(
                model_attempts=1,
                input_tokens=200,
                output_tokens=100,
                cost_usd=Decimal("0.01"),
                execution_seconds=0.1,
            ),
            provider_request_id="provider-request-1",
        )


def _gateway(provider):
    return ModelGateway(
        [provider],
        BudgetLedger(
            RunBudget(
                max_model_attempts=4,
                max_input_tokens=100_000,
                max_output_tokens=100_000,
                max_cost_usd=Decimal("20"),
                max_execution_seconds=1_000,
            )
        ),
    )


def test_schema_is_strict_and_only_authorizes_create_or_replace():
    schema = file_bundle_schema(CodingLimits(max_files=3))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["files"]["maxItems"] == 3
    item = schema["properties"]["files"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["operation"]["enum"] == ["create", "replace"]
    assert set(item["required"]) == {"operation", "path", "content"}


def test_default_prompt_and_cost_reservations_are_internally_coherent():
    limits = coding.DEFAULT_LIMITS

    # four bytes of prompt is about a token: the byte ceiling must fit the
    # token budget the provider reserves for the attempt
    assert limits.max_prompt_bytes <= limits.max_input_tokens * 4
    assert limits.max_prompt_bytes == 48_000
    assert limits.max_input_tokens == 32_000
    assert limits.max_output_tokens == 8_000
    assert limits.max_cost_usd == Decimal("0.208")
    with pytest.raises(ValueError, match="cannot exceed max_input_tokens"):
        CodingLimits(max_prompt_bytes=16_001, max_input_tokens=4_000)


def test_prompt_contains_only_approved_task_context_dependencies_and_current_files(
    tmp_path,
):
    project, architecture, plan, approval = _fixture()
    current = tmp_path / "apps/web/existing.ts"
    current.parent.mkdir(parents=True)
    current.write_text("export const existing = true;\n")

    prompt = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["web"],
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
    )

    assert "A note retains its text." not in prompt.user_prompt
    assert "The current note is rendered" in prompt.user_prompt
    assert "Exposes a stable getNote operation." in prompt.user_prompt
    assert "apps/web/existing.ts" in prompt.user_prompt
    assert "export const existing = true;" in prompt.user_prompt
    assert "verification was not run" not in prompt.user_prompt
    assert prompt.current_file_count == 1
    assert prompt.prompt_bytes <= coding.DEFAULT_LIMITS.max_prompt_bytes


def test_prompt_rejects_stale_approval_unrelated_summaries_and_size_overflow(
    tmp_path,
):
    project, architecture, plan, approval = _fixture()
    stale = ApprovalWitness(
        project.id, project.revision + 1, architecture.id, architecture.revision
    )
    with pytest.raises(ApprovalMismatchError):
        build_task_prompt(
            workspace=tmp_path,
            project=project,
            architecture=architecture,
            task=plan.task_index["web"],
            approval=stale,
        )

    with pytest.raises(coding.CodingWorkerError, match="unrelated"):
        build_task_prompt(
            workspace=tmp_path,
            project=project,
            architecture=architecture,
            task=plan.task_index["web"],
            approval=approval,
            dependency_summaries={"other": "out of scope"},
        )

    with pytest.raises(PromptLimitError):
        build_task_prompt(
            workspace=tmp_path,
            project=project,
            architecture=architecture,
            task=plan.task_index["web"],
            approval=approval,
            dependency_summaries={"domain": "x" * 100},
            limits=CodingLimits(
                max_dependency_summary_bytes=16,
                max_dependency_summary_total_bytes=16,
            ),
        )


@pytest.mark.parametrize(
    ("file_patch", "message"),
    [
        ({"operation": "delete"}, "deletion is not authorized"),
        ({"path": "../outside.ts"}, "strict relative"),
        ({"path": "packages/domain/outside.ts"}, "outside task ownership"),
        ({"path": "apps/web/.env.local"}, "secret-bearing"),
        ({"path": "apps/web/private.pem"}, "secret-bearing"),
        ({"content": "prefix\x00suffix"}, "NUL"),
        ({"content": "prefix\x07suffix"}, "binary"),
    ],
)
def test_bundle_rejects_unauthorized_paths_operations_secrets_and_binary(
    file_patch, message
):
    document = _valid_bundle()
    document["files"][0].update(file_patch)

    with pytest.raises(FileBundleValidationError, match=message):
        parse_file_bundle(document, owned_paths=("apps/web",))


@pytest.mark.parametrize(
    "path",
    [
        "apps/web/package.json",
        "apps/web/tsconfig.json",
        "apps/web/next.config.mjs",
        "apps/web/next-env.d.ts",
        "apps/web/src/page.test.tsx",
        "apps/web/tests/acceptance.spec.ts",
        "apps/web/src/global.d.ts",
    ],
)
def test_bundle_cannot_rewrite_its_verification_or_toolchain_inputs(path):
    with pytest.raises(
        FileBundleValidationError, match="protected verification or toolchain"
    ):
        parse_file_bundle(
            _valid_bundle(
                files=[
                    {
                        "operation": "replace",
                        "path": path,
                        "content": "export {};\n",
                    }
                ]
            ),
            owned_paths=("apps/web",),
        )


def test_bundle_rejects_unknown_fields_duplicates_counts_and_byte_limits():
    unknown = _valid_bundle()
    unknown["files"][0]["mode"] = "0644"
    with pytest.raises(FileBundleValidationError, match="exactly"):
        parse_file_bundle(unknown, owned_paths=("apps/web",))

    duplicate = _valid_bundle()
    duplicate["files"].append(dict(duplicate["files"][0]))
    with pytest.raises(FileBundleValidationError, match="duplicated"):
        parse_file_bundle(duplicate, owned_paths=("apps/web",))

    with pytest.raises(FileBundleValidationError, match="1-file"):
        parse_file_bundle(
            {
                "summary": "too many",
                "files": [
                    {
                        "operation": "create",
                        "path": f"apps/web/{index}.ts",
                        "content": "x",
                    }
                    for index in range(2)
                ],
            },
            owned_paths=("apps/web",),
            limits=CodingLimits(max_files=1),
        )

    with pytest.raises(FileBundleValidationError, match="exceeds 4 bytes"):
        parse_file_bundle(
            _valid_bundle(
                files=[
                    {
                        "operation": "create",
                        "path": "apps/web/a.ts",
                        "content": "ééé",
                    }
                ]
            ),
            owned_paths=("apps/web",),
            limits=CodingLimits(max_file_bytes=4, max_total_bytes=4),
        )

    malformed = _valid_bundle()
    malformed["files"][0]["operation"] = []
    with pytest.raises(FileBundleValidationError, match="operation"):
        parse_file_bundle(malformed, owned_paths=("apps/web",))

    tuple_files = _valid_bundle()
    tuple_files["files"] = tuple(tuple_files["files"])
    with pytest.raises(FileBundleValidationError, match="array"):
        parse_file_bundle(tuple_files, owned_paths=("apps/web",))


def test_atomic_writer_applies_create_and_replace_then_receipt_rolls_back(
    tmp_path,
):
    existing = tmp_path / "apps/web/existing.ts"
    existing.parent.mkdir(parents=True)
    existing.write_text("old\n")
    bundle = parse_file_bundle(
        {
            "summary": "two files",
            "files": [
                {
                    "operation": "replace",
                    "path": "apps/web/existing.ts",
                    "content": "new\n",
                },
                {
                    "operation": "create",
                    "path": "apps/web/nested/new.ts",
                    "content": "created\n",
                },
            ],
        },
        owned_paths=("apps/web",),
    )

    commit = AtomicSourceWriter(tmp_path).apply(bundle)

    assert existing.read_text() == "new\n"
    assert (tmp_path / "apps/web/nested/new.ts").read_text() == "created\n"
    assert len(commit.source_digest) == 64
    assert commit.total_bytes == len(b"new\ncreated\n")

    commit.rollback()
    commit.rollback()  # idempotent after a successful rollback

    assert existing.read_text() == "old\n"
    assert not (tmp_path / "apps/web/nested/new.ts").exists()
    assert not (tmp_path / "apps/web/nested").exists()
    assert commit.rolled_back


def test_atomic_writer_rolls_back_partial_failure(tmp_path, monkeypatch):
    first = tmp_path / "apps/web/a.ts"
    second = tmp_path / "apps/web/b.ts"
    first.parent.mkdir(parents=True)
    first.write_text("old-a")
    second.write_text("old-b")
    bundle = parse_file_bundle(
        {
            "summary": "replace both",
            "files": [
                {
                    "operation": "replace",
                    "path": "apps/web/a.ts",
                    "content": "new-a",
                },
                {
                    "operation": "replace",
                    "path": "apps/web/b.ts",
                    "content": "new-b",
                },
            ],
        },
        owned_paths=("apps/web",),
    )
    real_replace = os.replace
    generated_replaces = 0

    def fail_second_generated(source, destination):
        nonlocal generated_replaces
        if ".rich-coding-" in str(source) and ".recovery" not in str(source):
            generated_replaces += 1
            if generated_replaces == 2:
                raise OSError("simulated second rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(coding.os, "replace", fail_second_generated)

    with pytest.raises(SourceTransactionError):
        AtomicSourceWriter(tmp_path).apply(bundle)

    assert first.read_text() == "old-a"
    assert second.read_text() == "old-b"


def test_writer_rejects_create_replace_mismatch_and_symlink_traversal(tmp_path):
    existing = tmp_path / "apps/web/existing.ts"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")

    with pytest.raises(SourceTransactionError, match="already exists"):
        AtomicSourceWriter(tmp_path).apply(
            parse_file_bundle(
                _valid_bundle(
                    files=[
                        {
                            "operation": "create",
                            "path": "apps/web/existing.ts",
                            "content": "new",
                        }
                    ]
                ),
                owned_paths=("apps/web",),
            )
        )
    with pytest.raises(SourceTransactionError, match="does not exist"):
        AtomicSourceWriter(tmp_path).apply(
            parse_file_bundle(
                _valid_bundle(
                    files=[
                        {
                            "operation": "replace",
                            "path": "apps/web/missing.ts",
                            "content": "new",
                        }
                    ]
                ),
                owned_paths=("apps/web",),
            )
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "apps/web/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SourceTransactionError, match="symlink"):
        AtomicSourceWriter(tmp_path).apply(
            parse_file_bundle(
                _valid_bundle(
                    files=[
                        {
                            "operation": "create",
                            "path": "apps/web/link/escape.ts",
                            "content": "no",
                        }
                    ]
                ),
                owned_paths=("apps/web",),
            )
        )
    assert not (outside / "escape.ts").exists()


def test_stale_commit_cannot_overwrite_later_change(tmp_path):
    target = tmp_path / "apps/web/page.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    commit = AtomicSourceWriter(tmp_path).apply(
        parse_file_bundle(
            _valid_bundle(
                files=[
                    {
                        "operation": "replace",
                        "path": "apps/web/page.tsx",
                        "content": "generated",
                    }
                ]
            ),
            owned_paths=("apps/web",),
        )
    )
    target.write_text("later")

    with pytest.raises(SourceTransactionError, match="stale rollback"):
        commit.rollback()

    assert target.read_text() == "later"


def test_coding_worker_uses_gateway_and_emits_generation_not_verification(
    tmp_path,
):
    project, architecture, plan, approval = _fixture()
    provider = RecordingProvider(_valid_bundle())
    commits = []
    worker = CodingWorker(
        _gateway(provider),
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        approval=approval,
        provider="fake",
        model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        commit_sink=commits.append,
    )

    result = worker.run_task(
        run_id="run.coding",
        durable_task_id="run.coding:implement:web",
        attempt=1,
        task=plan.task_index["web"],
    )

    assert result.succeeded
    assert (tmp_path / "apps/web/page.tsx").is_file()
    assert provider.requests[0].response_schema["additionalProperties"] is False
    assert provider.requests[0].role.value == "implementer"
    assert provider.requests[0].correlation_id.endswith(":attempt:1:implementation")
    assert len(commits) == 1
    assert result.evidence[0].kind == "generation"
    assert result.evidence[0].status == "passed"
    assert result.evidence[0].blocking is False
    assert result.evidence[0].details["verification_status"] == "not_run"
    assert result.evidence[0].details["acceptance_status"] == "not_evaluated"
    assert "verification not run" in result.summary
    artifact = json.loads(result.artifacts[0].content)
    assert artifact["source_digest"] == commits[0].source_digest
    assert result.artifacts[0].metadata["verification_status"] == "not_run"
    assert result.artifacts[0].metadata["acceptance_status"] == "not_evaluated"


def test_invalid_provider_bundle_never_mutates_workspace(tmp_path):
    project, architecture, plan, approval = _fixture()
    provider = RecordingProvider(
        _valid_bundle(
            files=[
                {
                    "operation": "create",
                    "path": "../escape.ts",
                    "content": "bad",
                }
            ]
        )
    )
    worker = CodingWorker(
        _gateway(provider),
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        approval=approval,
        provider="fake",
        model="test-model",
        dependency_summaries={"domain": "complete"},
    )

    with pytest.raises(FileBundleValidationError):
        worker.run_task(
            run_id="run.coding",
            durable_task_id="run.coding:implement:web",
            attempt=1,
            task=plan.task_index["web"],
        )

    assert list(tmp_path.iterdir()) == []


_OWNED = ("apps/web/note.tsx",)


def test_redaction_keeps_your_diagnostics_and_withholds_a_sibling_source():
    """A compiler diagnostic naming another node's file would otherwise be a
    side channel straight through the information firewall."""

    observed = (
        "apps/web/note.tsx(12,5): error TS2322: Type 'number' is not assignable.\n"
        "packages/domain/secret.ts(3,1): error TS2554: Expected 2 arguments.\n"
        "   3 | const APP_SECRET = 'hunter2'\n"
        "     |       ^^^^^^^^^^\n"
        "tests/unit/requirements/r_1.test.ts(8,3): AssertionError: expected 1\n"
    )

    kept, withheld = redact_diagnostics(observed, owned_paths=_OWNED)
    joined = "\n".join(kept)

    assert "apps/web/note.tsx(12,5)" in joined, "your own error is the point"
    assert "tests/unit/requirements" in joined, (
        "generated tests are the approved spec made executable, not a sibling's scope"
    )
    assert "secret.ts" not in joined
    assert "APP_SECRET" not in joined
    assert "hunter2" not in joined, "the code frame inherits its file's disclosability"
    assert withheld == 3


def test_redaction_is_bounded_by_lines_and_bytes():
    limits = CodingLimits(max_prior_failure_lines=2, max_prior_failure_bytes=4096)
    observed = "\n".join(
        f"apps/web/note.tsx({index},1): error TS1005: ';' expected."
        for index in range(1, 9)
    )

    kept, withheld = redact_diagnostics(observed, owned_paths=_OWNED, limits=limits)

    assert len(kept) == 2
    assert withheld == 6


def test_a_retry_prompt_carries_the_gate_output_a_first_attempt_cannot(tmp_path):
    project, architecture, plan, approval = _fixture()
    task = plan.task_index["web"]
    failure = PriorAttemptFailure(
        attempt=1,
        gate="types",
        summary="types exited with 2",
        diagnostics=("apps/web/note.tsx(4,9): error TS2304: Cannot find 'noteText'.",),
        withheld_line_count=2,
    )

    first = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=task,
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
    )
    retry = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=task,
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
        prior_failures=[failure],
    )

    assert "TS2304" not in first.user_prompt
    assert "prior_attempt_failures" in first.user_prompt
    assert "TS2304" in retry.user_prompt
    assert "withheld_diagnostic_lines" in retry.user_prompt, (
        "a broken consumer must be reported as a count, since its source cannot be"
    )
    assert "observed by the harness" in retry.user_prompt
    assert retry.prompt_bytes <= coding.DEFAULT_LIMITS.max_prompt_bytes


def test_only_the_most_recent_failures_are_carried(tmp_path):
    project, architecture, plan, approval = _fixture()
    failures = [
        PriorAttemptFailure(
            attempt=index,
            gate="unit",
            summary=f"unit exited with {index}",
            diagnostics=(f"apps/web/note.tsx(1,1): failure number {index}",),
        )
        for index in range(1, 7)
    ]

    prompt = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["web"],
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
        prior_failures=failures,
    )

    assert "failure number 6" in prompt.user_prompt
    assert "failure number 4" in prompt.user_prompt
    assert "failure number 3" not in prompt.user_prompt, "bounded by max_prior_failures"


def test_the_worker_asks_for_prior_failures_only_when_retrying(tmp_path):
    project, architecture, plan, approval = _fixture()
    asked = []

    def prior_failures(task, attempt):
        asked.append((task.node_id, attempt))
        return [
            PriorAttemptFailure(
                attempt=attempt - 1,
                gate="lint",
                summary="lint exited with 1",
                diagnostics=("apps/web/note.tsx(2,1): error: no-unused-vars",),
            )
        ]

    def _worker(provider, workspace):
        workspace.mkdir(parents=True, exist_ok=True)
        return CodingWorker(
            _gateway(provider),
            workspace=workspace,
            project=project,
            architecture=architecture,
            approval=approval,
            provider="fake",
            model="test-model",
            dependency_summaries={"domain": "Domain generation completed."},
            prior_failures=prior_failures,
        )

    first_provider = RecordingProvider(_valid_bundle())
    _worker(first_provider, tmp_path / "first").run_task(
        run_id="run.retry",
        durable_task_id="run.retry:implement:web",
        attempt=1,
        task=plan.task_index["web"],
    )
    retry_provider = RecordingProvider(_valid_bundle())
    _worker(retry_provider, tmp_path / "second").run_task(
        run_id="run.retry",
        durable_task_id="run.retry:implement:web",
        attempt=2,
        task=plan.task_index["web"],
    )

    assert asked == [("web", 2)], "a first attempt has nothing to learn from"
    assert "no-unused-vars" not in first_provider.requests[0].user_prompt
    assert "no-unused-vars" in retry_provider.requests[0].user_prompt


class _Memo:
    """The smallest thing satisfying GenerationMemoStore."""

    def __init__(self):
        self.entries = {}
        self.reads = 0

    def get(self, cache_key):
        self.reads += 1
        return self.entries.get(cache_key)

    def put(
        self, cache_key, *, document, project_id, node_id, provider, model,
        run_id, task_id,
    ):
        self.entries[cache_key] = {
            "bundle": document["bundle"],
            "provider": provider,
            "model": model,
            "origin_run_id": run_id,
            "origin_task_id": task_id,
        }


def _workspace(tmp_path, name):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run(worker, task, *, run_id="run.memo", attempt=1, verified=True):
    """Run one task, then stand in for the gates accepting it.

    A worker stages its memo and cannot commit it: only whatever ran the
    independent gates may decide an answer is worth replaying.
    """

    result = worker.run_task(
        run_id=run_id,
        durable_task_id=f"{run_id}:implement:web",
        attempt=attempt,
        task=task,
    )
    if verified:
        worker.commit_memo()
    return result


def test_an_identical_request_reuses_the_bundle_without_calling_the_model(tmp_path):
    project, architecture, plan, approval = _fixture()
    memo = _Memo()
    task = plan.task_index["web"]

    first_provider = RecordingProvider(_valid_bundle())
    first = _run(
        CodingWorker(
            _gateway(first_provider),
            workspace=_workspace(tmp_path, "a"),
            project=project,
            architecture=architecture,
            approval=approval,
            provider="fake",
            model="test-model",
            dependency_summaries={"domain": "Domain generation completed."},
            memo=memo,
        ),
        task,
    )
    second_provider = RecordingProvider(_valid_bundle())
    second = _run(
        CodingWorker(
            _gateway(second_provider),
            workspace=_workspace(tmp_path, "b"),
            project=project,
            architecture=architecture,
            approval=approval,
            provider="fake",
            model="test-model",
            dependency_summaries={"domain": "Domain generation completed."},
            memo=memo,
        ),
        task,
        run_id="run.memo2",
    )

    assert len(first_provider.requests) == 1
    assert len(second_provider.requests) == 0, "the model must not be asked twice"
    assert first.succeeded and second.succeeded
    assert (tmp_path / "b/apps/web/page.tsx").is_file(), "the source is still written"
    assert first.evidence[0].details["generation_reused"] is False
    assert second.evidence[0].details["generation_reused"] is True
    assert second.evidence[0].details["reused_from_run_id"] == "run.memo"
    assert "Reused" in second.evidence[0].summary
    assert "Generated" in first.evidence[0].summary


def test_a_reused_bundle_is_revalidated_not_trusted(tmp_path):
    """A memo is a way to skip asking, never a way to skip checking."""

    project, architecture, plan, approval = _fixture()
    task = plan.task_index["web"]
    memo = _Memo()
    provider = RecordingProvider(_valid_bundle())
    worker = CodingWorker(
        _gateway(provider),
        workspace=_workspace(tmp_path, "a"),
        project=project,
        architecture=architecture,
        approval=approval,
        provider="fake",
        model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        memo=memo,
    )
    _run(worker, task)

    # Poison the store the way a corrupted or tampered payload would.
    (key,) = memo.entries
    memo.entries[key]["bundle"]["files"][0]["path"] = "packages/db/schema.ts"

    poisoned = CodingWorker(
        _gateway(RecordingProvider(_valid_bundle())),
        workspace=_workspace(tmp_path, "c"),
        project=project,
        architecture=architecture,
        approval=approval,
        provider="fake",
        model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        memo=memo,
    )
    with pytest.raises(FileBundleValidationError, match="outside task ownership"):
        _run(poisoned, task, run_id="run.memo3")
    assert not (tmp_path / "c/packages").exists(), "nothing was written"


def test_a_retry_looks_up_the_same_memo_as_a_first_attempt(tmp_path):
    """The memo answers "what source satisfies this task?". How many attempts
    it took to find that answer is coaching, not part of the question -- and
    keying on it would make a memo recorded on a retry unreachable forever."""

    project, architecture, plan, approval = _fixture()
    task = plan.task_index["web"]
    asked = []

    def prior_failures(_task, attempt):
        asked.append(attempt)
        return [
            PriorAttemptFailure(
                attempt=attempt - 1,
                gate="unit",
                summary="unit exited with 1",
                diagnostics=("apps/web/note.tsx(1,1): boom",),
            )
        ]

    memo = _Memo()
    first = CodingWorker(
        _gateway(RecordingProvider(_valid_bundle())),
        workspace=_workspace(tmp_path, "one"),
        project=project, architecture=architecture, approval=approval,
        provider="fake", model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        prior_failures=prior_failures, memo=memo,
    )
    # A second attempt succeeds and records its answer.
    _run(first, task, run_id="run.k", attempt=2)
    (recorded_key,) = memo.entries

    second_provider = RecordingProvider(_valid_bundle())
    second = CodingWorker(
        _gateway(second_provider),
        workspace=_workspace(tmp_path, "two"),
        project=project, architecture=architecture, approval=approval,
        provider="fake", model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        prior_failures=prior_failures, memo=memo,
    )
    result = _run(second, task, run_id="run.k2", attempt=1)

    assert asked == [2], "a first attempt has nothing to learn from"
    assert len(second_provider.requests) == 0, (
        f"a clean first attempt must find the retry's answer under {recorded_key[:8]}"
    )
    assert result.evidence[0].details["generation_reused"] is True


def test_a_retry_never_replays_the_answer_that_just_failed(tmp_path):
    """The prompt carries the prior failure, so the key changes with it."""

    project, architecture, plan, approval = _fixture()
    task = plan.task_index["web"]

    def _prompt(prior):
        return build_task_prompt(
            workspace=tmp_path,
            project=project,
            architecture=architecture,
            task=task,
            approval=approval,
            dependency_summaries={"domain": "Exposes a stable getNote operation."},
            prior_failures=prior,
        )

    schema = file_bundle_schema()
    clean = generation_cache_key(
        _prompt(()), provider="fake", model="test-model", response_schema=schema
    )
    after_failure = generation_cache_key(
        _prompt(
            [
                PriorAttemptFailure(
                    attempt=1,
                    gate="types",
                    summary="types exited with 2",
                    diagnostics=("apps/web/note.tsx(1,1): error TS2304",),
                )
            ]
        ),
        provider="fake",
        model="test-model",
        response_schema=schema,
    )

    assert clean != after_failure
    assert len(clean) == 64


def test_the_key_separates_provider_model_and_schema(tmp_path):
    project, architecture, plan, approval = _fixture()
    prompt = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["web"],
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
    )
    base = dict(provider="fake", model="test-model", response_schema=file_bundle_schema())
    key = generation_cache_key(prompt, **base)

    assert generation_cache_key(prompt, **{**base, "provider": "other"}) != key
    assert generation_cache_key(prompt, **{**base, "model": "other"}) != key
    assert (
        generation_cache_key(
            prompt, **{**base, "response_schema": file_bundle_schema(CodingLimits(max_files=3))}
        )
        != key
    )
    assert generation_cache_key(prompt, **base) == key, "and it is stable"


def test_the_pinned_interface_is_shown_only_to_the_task_that_implements_it(
    tmp_path,
):
    """It is a protected input scoped out of current_files, so a task told to
    satisfy it would otherwise fail typecheck for a reason it was never given."""

    project, architecture, plan, approval = _fixture()
    # Beside the component's own source: one interface per component.
    interface = tmp_path / "packages/domain/src/operations-contract.ts"
    interface.parent.mkdir(parents=True)
    interface.write_text("export interface Operations {\n  getNote(input: string): string;\n}\n")

    web = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["web"],
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
    )
    domain = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["domain"],
        approval=approval,
    )

    assert "pinned_operations_interface" not in web.user_prompt
    assert "export interface Operations" in domain.user_prompt
    assert "packages/domain/src/operations.ts" in domain.user_prompt, (
        "it is told where to implement, which is beside its own interface"
    )
    assert "export a const named" in domain.user_prompt.lower()


def test_the_pinned_interface_cannot_be_rewritten_by_the_worker():
    """It sits inside the domain node's ownership because that is where it has
    to be importable from -- without an explicit rule it would be the one
    protected input a worker could legally edit."""

    assert coding.is_protected_generation_path(
        PurePosixPath("packages/contracts/src/operations-contract.ts")
    )
    assert coding.is_protected_generation_path(
        PurePosixPath("apps/web/src/operations-contract.ts")
    ), "every component's interface, not one blessed path"
    assert not coding.is_protected_generation_path(
        PurePosixPath("packages/contracts/src/operations.ts")
    ), "the implementation is the worker's to write"


def test_a_protected_input_is_not_offered_as_one_of_your_current_files(tmp_path):
    """The parser rejects writes to protected paths, so presenting them as
    editable sets a worker up to fail -- and makes every node's inputs change
    whenever a shared, spec-derived file does."""

    project, architecture, plan, approval = _fixture()
    task = plan.task_index["domain"]
    owned = task.owned_paths[0]
    root = tmp_path / owned
    root.mkdir(parents=True)
    (root / "index.ts").write_text("export const mine = true;\n")
    (root / "product-intent.ts").write_text("export const approved = [];\n")

    prompt = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=task,
        approval=approval,
    )

    assert "export const mine" in prompt.user_prompt
    offered = json.loads(prompt.user_prompt[prompt.user_prompt.index("{"):])["current_files"]
    paths = [f["path"] if isinstance(f, dict) else f for f in (offered if isinstance(offered, list) else list(offered))]
    assert not any("product-intent.ts" in path for path in paths), (
        "a compiled-from-intent module is context, not workspace"
    )
    assert prompt.current_file_count == 1


def test_a_generation_the_gates_rejected_is_never_remembered(tmp_path):
    """Otherwise a later run replays a known-bad answer into a fresh
    workspace, and pays the gates to reject it a second time."""

    project, architecture, plan, approval = _fixture()
    memo = _Memo()
    task = plan.task_index["web"]

    _run(
        CodingWorker(
            _gateway(RecordingProvider(_valid_bundle())),
            workspace=_workspace(tmp_path, "rejected"),
            project=project,
            architecture=architecture,
            approval=approval,
            provider="fake",
            model="test-model",
            dependency_summaries={"domain": "Domain generation completed."},
            memo=memo,
        ),
        task,
        verified=False,
    )

    assert memo.entries == {}, "nothing was verified, so nothing is replayable"


def test_the_worker_cannot_decide_its_own_answer_is_worth_keeping(tmp_path):
    project, architecture, plan, approval = _fixture()
    memo = _Memo()
    worker = CodingWorker(
        _gateway(RecordingProvider(_valid_bundle())),
        workspace=_workspace(tmp_path, "staged"),
        project=project,
        architecture=architecture,
        approval=approval,
        provider="fake",
        model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        memo=memo,
    )

    worker.run_task(
        run_id="run.staged",
        durable_task_id="run.staged:implement:web",
        attempt=1,
        task=plan.task_index["web"],
    )
    staged = dict(memo.entries)
    committed = worker.commit_memo()

    assert staged == {}, "run_task must stage rather than write"
    assert committed is True and memo.entries, "the gates' verdict commits it"
    assert worker.commit_memo() is False, "and it commits exactly once"


def test_the_key_is_fixed_at_the_first_attempt_of_a_run(tmp_path):
    """A failed attempt leaves its source behind for the next one to repair.
    Keying on what is in the workspace *now* would record the winning answer
    under "the workspace after a failure" -- a state no fresh run reproduces."""

    project, architecture, plan, approval = _fixture()
    task = plan.task_index["web"]
    workspace = _workspace(tmp_path, "repair")
    memo = _Memo()
    worker = CodingWorker(
        _gateway(RecordingProvider(_valid_bundle())),
        workspace=workspace,
        project=project,
        architecture=architecture,
        approval=approval,
        provider="fake",
        model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        memo=memo,
    )

    first = worker.run_task(
        run_id="run.repair",
        durable_task_id="run.repair:implement:web",
        attempt=1,
        task=task,
    )
    # Attempt one wrote source; a second attempt sees it and must still key
    # the task the same way.
    assert (workspace / "apps/web/page.tsx").is_file()
    worker._pending_memo = None
    # The repair replaces what the failed attempt wrote, as a real one does.
    worker.gateway = _gateway(
        RecordingProvider(
            _valid_bundle(
                files=[
                    {
                        "operation": "replace",
                        "path": "apps/web/page.tsx",
                        "content": "export const repaired = true;\n",
                    }
                ]
            )
        )
    )
    second = worker.run_task(
        run_id="run.repair",
        durable_task_id="run.repair:implement:web",
        attempt=2,
        task=task,
    )

    assert (
        first.evidence[0].details["cache_key"]
        == second.evidence[0].details["cache_key"]
    ), "the question did not change; only the state of the repair did"


def test_a_replayed_bundle_states_its_operations_against_this_workspace(tmp_path):
    """The winning attempt of an earlier run is often a repair, whose bundle
    says "replace" for files a fresh scaffold does not have. Refusing that
    would make every memo earned by a retry unusable -- and most are.

    A create/replace mismatch is a real guard on a model's answer: claiming to
    create a file that exists means it has misunderstood something. It is not a
    guard on a replay, where the bundle has already been verified and only the
    destination has moved.
    """

    workspace = _workspace(tmp_path, "replay")
    (workspace / "apps/web").mkdir(parents=True)
    (workspace / "apps/web/here.tsx").write_text("existing\n")
    document = {
        "summary": "s",
        "files": [
            {"operation": "replace", "path": "apps/web/absent.tsx", "content": "a"},
            {"operation": "create", "path": "apps/web/here.tsx", "content": "b"},
        ],
    }

    restated = coding._replayable(document, workspace)

    by_path = {item["path"]: item["operation"] for item in restated["files"]}
    assert by_path == {
        "apps/web/absent.tsx": "create",
        "apps/web/here.tsx": "replace",
    }
    assert restated["summary"] == "s", "only the operations are restated"
    assert [item["content"] for item in restated["files"]] == ["a", "b"]


def test_restating_leaves_a_malformed_document_for_the_parser_to_refuse(tmp_path):
    """It must not quietly repair something the security boundary should see."""

    workspace = _workspace(tmp_path, "malformed")

    for document in ({"summary": "s"}, {"files": "nope"}, {"files": [{"path": 7}]}):
        assert coding._replayable(document, workspace) == dict(document)


def test_a_retry_asks_rather_than_replaying_the_answer_that_just_failed(tmp_path):
    """The key is fixed at the run's baseline so a memo stays reachable across
    runs. Within a run that would mean replaying the same rejected answer on
    every attempt -- the same gate failure, forever."""

    project, architecture, plan, approval = _fixture()
    task = plan.task_index["web"]
    memo = _Memo()
    memo.entries["seeded"] = None  # never consulted; presence is the point

    provider = RecordingProvider(_valid_bundle())
    worker = CodingWorker(
        _gateway(provider),
        workspace=_workspace(tmp_path, "retry"),
        project=project, architecture=architecture, approval=approval,
        provider="fake", model="test-model",
        dependency_summaries={"domain": "Domain generation completed."},
        prior_failures=lambda _task, attempt: [
            PriorAttemptFailure(
                attempt=attempt - 1, gate="property",
                summary="property exited with 1",
                diagnostics=("apps/web/note.tsx(1,1): wrong",),
            )
        ],
        memo=memo,
    )

    result = worker.run_task(
        run_id="run.retry.memo",
        durable_task_id="run.retry.memo:implement:web",
        attempt=2,
        task=task,
    )

    assert memo.reads == 0, "a retry must not even consult the memo"
    assert len(provider.requests) == 1, "it asks, with the failure in hand"
    assert "property exited with 1" in provider.requests[0].user_prompt
    assert result.evidence[0].details["generation_reused"] is False


def test_a_huge_prior_failure_is_cut_to_its_budget_not_the_retry(tmp_path):
    """A live run reopened a component whose retry died on PromptLimitError:
    the failure it was shown was larger than the prompt could carry."""
    project, architecture, plan, approval = _fixture()
    failure = PriorAttemptFailure(
        attempt=1,
        gate="acceptance",
        summary="the assembled application failed acceptance on pages this task owns",
        diagnostics=tuple(
            [f"scenario.{i} · {i} · Expect to see ‘thing {i}’: Error: not found" for i in range(12)]
            + [f"apps/web/note.tsx line {i}: " + "x" * 200 for i in range(400)]
        ),
    )
    prompt = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["web"],
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
        prior_failures=[failure],
    )
    assert prompt.prompt_bytes <= coding.DEFAULT_LIMITS.max_prompt_bytes
    assert "Expect to see ‘thing 11’" in prompt.user_prompt, "the named steps come first and survive"
    assert "withheld_diagnostic_lines" in prompt.user_prompt


def test_a_scenario_names_the_pages_the_task_owns(tmp_path):
    project, architecture, plan, approval = _fixture()
    scenario_id = project.acceptance_scenarios[0].id

    def pages(spec, scenario):
        assert spec is project
        return ("apps/web/note.tsx", "packages/domain/src/index.ts") if scenario.id == scenario_id else ()

    with_pages = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["web"],
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
        scenario_pages=pages,
    )
    context = json.loads(with_pages.user_prompt[with_pages.user_prompt.index("{"):])
    scenarios = {s["id"]: s for s in context["approved_intent"]["acceptance_scenarios"]}
    assert scenarios[scenario_id]["pages"] == ["apps/web/note.tsx"], "only the pages this task owns"
    assert context["pages_to_write"] == ["apps/web/note.tsx"]
    assert with_pages.user_prompt.startswith("Deliverables, in order. 1. Rewrite these placeholder pages")
    assert "apps/web/note.tsx" in with_pages.user_prompt.split("Produce the smallest")[0]
    assert "runs its browser steps against those files" in with_pages.system_prompt

    without = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["web"],
        approval=approval,
        dependency_summaries={"domain": "Exposes a stable getNote operation."},
    )
    plain = json.loads(without.user_prompt[without.user_prompt.index("{"):])
    protected = plain["write_authority"]["protected"]
    assert "package.json" in protected["file_names"] and "tests" in protected["directory_names"]
    assert any(".d.ts" in rule for rule in protected["rules"])
    assert "Never write a path that write_authority.protected describes" in without.system_prompt
    assert all("pages" not in s for s in plain["approved_intent"]["acceptance_scenarios"])
    assert plain["pages_to_write"] == []
    assert without.user_prompt.startswith("Produce the smallest")


# --------------------------------------------------------------------------
# Persistence: the data package's factory and migrator are protected, and the
# factory is handed to the one task that must reach the database through it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["packages/db/src/database.ts", "packages/db/src/migrate.ts"],
)
def test_the_database_boundary_cannot_be_rewritten_by_the_data_worker(path):
    """Inside the data node's ownership because that is where it must be
    importable from; protected because a worker that could rewrite the factory
    could pick an engine the gates never ran, and one that could rewrite the
    migrator could journal a migration it did not apply."""

    assert coding.is_protected_generation_path(PurePosixPath(path))
    with pytest.raises(
        FileBundleValidationError, match="protected verification or toolchain"
    ):
        parse_file_bundle(
            _valid_bundle(
                files=[{"operation": "replace", "path": path, "content": "export {};\n"}]
            ),
            owned_paths=("packages/db",),
        )
    # Exactly those two, not the whole package: the schema, the migrations and
    # the operations are the worker's to write.
    assert not coding.is_protected_generation_path(PurePosixPath("packages/db/src/schema.ts"))
    assert not coding.is_protected_generation_path(
        PurePosixPath("packages/db/migrations/0001_todos.sql")
    )
    assert not coding.is_protected_generation_path(PurePosixPath("packages/db/src/operations.ts"))
    assert not coding.is_protected_generation_path(
        PurePosixPath("packages/domain/src/database.ts")
    ), "the rule names the data package, not a filename"


def _persisting_fixture(tmp_path):
    from richbuild.planner import plan_nextjs_architecture
    from richbuild.target_packs.nextjs import NextJsTargetPack, NextJsTargetPackConfig

    project = ProjectSpec(
        id="project.todo",
        name="Todo",
        goal="Keep a todo list that is stored in the database.",
        audiences=("members",),
        requirements=(
            Requirement(
                id="requirement.todo",
                title="Todo list",
                statement="A member adds a todo and it is stored.",
            ),
        ),
        acceptance_scenarios=(
            AcceptanceScenario(
                id="scenario.todo",
                title="A todo persists",
                given=("The list is empty.",),
                when=("A member adds 'Buy milk'.",),
                then=("'Buy milk' is listed.",),
                requirement_ids=("requirement.todo",),
                oracle=(
                    {"action": "open_requirement"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "text", "value": "Buy milk"},
                    },
                ),
            ),
        ),
    )
    architecture = plan_nextjs_architecture(project).architecture
    assert any(node.kind is NodeKind.DATA for node in architecture.nodes)
    workspace = tmp_path / "workspace"
    NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name="todo", project_spec=project, architecture=architecture
        )
    ).scaffold(workspace)
    plan = compile_architecture(architecture, project)
    approval = ApprovalWitness(
        project_id=project.id,
        project_revision=project.revision,
        architecture_id=architecture.id,
        architecture_revision=architecture.revision,
    )
    return workspace, project, architecture, plan, approval


def test_the_database_factory_is_shown_only_to_the_task_that_owns_the_data_package(
    tmp_path,
):
    """It is protected, so it is scoped out of current_files; a worker told to
    persist without being shown the one door to the database would invent a
    second one."""

    workspace, project, architecture, plan, approval = _persisting_fixture(tmp_path)

    data = build_task_prompt(
        workspace=workspace,
        project=project,
        architecture=architecture,
        task=plan.task_index["data"],
        approval=approval,
    )
    domain = build_task_prompt(
        workspace=workspace,
        project=project,
        architecture=architecture,
        task=plan.task_index["domain"],
        approval=approval,
        dependency_summaries={"data": "Stores todos."},
    )

    assert "pinned_database_factory" in data.user_prompt
    assert "DATABASE_URL or RICH_DATABASE_DIR is required" in data.user_prompt
    assert "packages/db/src/database.ts" in data.user_prompt
    assert "--> statement-breakpoint" in data.user_prompt
    assert "never assume existing rows" in data.user_prompt
    # Not one of "your current files": the parser would refuse the edit.
    assert '"path": "packages/db/src/database.ts", "sha256"' not in data.user_prompt
    assert "packages/db/src/migrate.ts" not in data.user_prompt

    assert "pinned_database_factory" not in domain.user_prompt
    assert "force-dynamic" in domain.user_prompt, (
        "every task in a persisting application is told the build has no database"
    )
    assert "importing a database driver" in domain.user_prompt
    # The first live proof: the web worker satisfied its contract and left the
    # scaffold's placeholder page, so the browser never found the field.
    assert "exactly the controls each oracle step names" in domain.user_prompt
    assert "placeholder page" in domain.user_prompt
    # M7's live proof: told only not to import a driver, the web worker kept
    # the todos in a `globalThis` array. `next start` is one long-lived
    # process, so the array survived `page.reload()`, every browser step
    # passed, and only the trusted probe -- reading a database whose tables
    # were all empty -- caught it. Forbidding the mechanism was not enough;
    # the behaviour has to be named.
    assert "hold no state of your own" in domain.user_prompt
    assert "no module-level or global mutable value" in domain.user_prompt
    assert "survives a reload" in domain.user_prompt


def test_an_application_without_a_data_component_hears_nothing_about_databases(
    tmp_path,
):
    project, architecture, plan, approval = _fixture()
    prompt = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=plan.task_index["domain"],
        approval=approval,
    )

    assert "force-dynamic" not in prompt.user_prompt
    assert "pinned_database_factory" not in prompt.user_prompt


def test_a_retry_prompt_withholds_the_oldest_failures_rather_than_not_happening(
    tmp_path,
):
    """Three bounded failures beside a large owned tree overflowed the prompt
    on a live run, so the second and third attempts never happened -- the
    task failed on PromptLimitError without the model ever reading the gate
    output. The oldest failures go first, the worker is told how many, and
    only a prompt that overflows with no failure at all is refused."""

    project, architecture, plan, approval = _fixture()
    task = plan.task_index["web"]
    summaries = {"domain": "Exposes a stable getNote operation."}
    failures = [
        PriorAttemptFailure(
            attempt=index,
            gate="types",
            summary=f"types exited with {index}",
            diagnostics=tuple(
                f"apps/web/note.tsx({line},1): error TS{index}000: failure number {index} "
                + "x" * 90
                for line in range(1, 30)
            ),
        )
        for index in range(1, 4)
    ]
    base = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=task,
        approval=approval,
        dependency_summaries=summaries,
    )
    one_failure = len(json.dumps(failures[2].to_dict()).encode("utf-8"))
    limits = coding.CodingLimits(max_prompt_bytes=base.prompt_bytes + one_failure + 1500)

    retry = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=task,
        approval=approval,
        dependency_summaries=summaries,
        prior_failures=failures,
        limits=limits,
    )

    assert retry.prompt_bytes <= limits.max_prompt_bytes
    assert "failure number 3" in retry.user_prompt, "the most recent is what fits"
    assert "failure number 2" not in retry.user_prompt
    assert "failure number 1" not in retry.user_prompt
    assert '"prior_attempt_failures_withheld":2' in retry.user_prompt
    assert "2 oldest failure(s) did not fit" in retry.user_prompt
    assert "observed by the harness" in retry.user_prompt

    # With room for all three, nothing is withheld and nothing says so.
    roomy = build_task_prompt(
        workspace=tmp_path,
        project=project,
        architecture=architecture,
        task=task,
        approval=approval,
        dependency_summaries=summaries,
        prior_failures=failures,
    )
    assert "failure number 1" in roomy.user_prompt
    assert "did not fit" not in roomy.user_prompt
    assert "prior_attempt_failures_withheld" not in roomy.user_prompt

    # A prompt that overflows with no failure at all is still refused: that
    # is a task too large for the worker, not a retry too large to explain.
    with pytest.raises(coding.PromptLimitError):
        build_task_prompt(
            workspace=tmp_path,
            project=project,
            architecture=architecture,
            task=task,
            approval=approval,
            dependency_summaries=summaries,
            prior_failures=failures,
            limits=coding.CodingLimits(max_prompt_bytes=base.prompt_bytes - 1),
        )
