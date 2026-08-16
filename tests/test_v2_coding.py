import json
import os
from decimal import Decimal

import pytest

import rich_v2.coding as coding
from rich_v2.budget import BudgetLedger, RunBudget, Usage
from rich_v2.coding import (
    ApprovalMismatchError,
    ApprovalWitness,
    AtomicSourceWriter,
    CodingLimits,
    CodingWorker,
    FileBundleValidationError,
    PromptLimitError,
    SourceTransactionError,
    build_task_prompt,
    file_bundle_schema,
    parse_file_bundle,
)
from rich_v2.compiler import compile_architecture
from rich_v2.models import (
    AcceptanceScenario,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpecV2,
    ContractV2,
    EdgeKind,
    NodeKind,
    OperationContract,
    ProjectSpecV2,
    Requirement,
)
from rich_v2.providers import ModelGateway, ModelResponse


def _fixture():
    project = ProjectSpecV2(
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
        ContractV2(
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
        ContractV2(
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
    architecture = ArchitectureSpecV2(
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

    assert limits.max_prompt_bytes <= limits.max_input_tokens
    assert limits.max_prompt_bytes == 24_000
    assert limits.max_input_tokens == 32_000
    assert limits.max_output_tokens == 8_000
    assert limits.max_cost_usd == Decimal("0.208")
    with pytest.raises(ValueError, match="cannot exceed max_input_tokens"):
        CodingLimits(max_prompt_bytes=16_001, max_input_tokens=16_000)


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
