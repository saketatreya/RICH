import json

import pytest

from richbuild import (
    AcceptanceScenario,
    Approval,
    ApprovalGate,
    ApprovalStatus,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpec,
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    BuildRun,
    BuildTask,
    Contract,
    EdgeKind,
    Evidence,
    EvidenceKind,
    EvidenceStatus,
    ModelValidationError,
    NodeKind,
    OperationContract,
    PortDirection,
    PortSpec,
    ProjectSpec,
    Requirement,
    RunStatus,
    TaskKind,
    TaskStatus,
    UnsupportedSchemaVersion,
    validate_release_traceability,
)


SHA256 = "a" * 64


def _project(**overrides):
    values = {
        "id": "project.todo",
        "name": "Semantic todo",
        "goal": "Let a founder reliably track work",
        "audiences": ("Technical founder",),
        "requirements": (
            Requirement(
                id="req.todo.add",
                title="Add todo",
                statement="A user can add a named todo item.",
            ),
        ),
        "acceptance_scenarios": (
            AcceptanceScenario(
                id="scenario.todo.add",
                title="Add a todo",
                given=("The todo list is empty.",),
                when=("The user submits a named todo.",),
                then=("The todo appears in the persisted list.",),
                requirement_ids=("req.todo.add",),
                oracle=(
                    {"action": "navigate", "value": "/"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "role", "value": "heading"},
                    },
                ),
            ),
        ),
    }
    values.update(overrides)
    return ProjectSpec(**values)


def _operation(operation_id, name, requirement_id="req.todo.add"):
    return OperationContract(
        id=operation_id,
        name=name,
        description=f"{name} a todo",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        output_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        requirement_ids=(requirement_id,),
    )


def _architecture(**overrides):
    root_contract = Contract(
        id="contract.app",
        node_id="node.app",
        operations=(_operation("op.app.add", "add"),),
    )
    domain_contract = Contract(
        id="contract.domain",
        node_id="node.domain",
        operations=(_operation("op.domain.add", "add"),),
    )
    root = ArchitectureNode(
        id="node.app",
        name="Todo application",
        kind=NodeKind.APPLICATION,
        contract_id=root_contract.id,
        requirement_ids=("req.todo.add",),
        owned_paths=("apps/web",),
        ports=(
            PortSpec(
                id="port.app.command",
                name="Todo command",
                direction=PortDirection.OUTPUT,
                operation_id="op.app.add",
                schema={"$ref": "#/$defs/TodoCommand"},
            ),
        ),
    )
    domain = ArchitectureNode(
        id="node.domain",
        name="Todo domain",
        kind=NodeKind.DOMAIN,
        contract_id=domain_contract.id,
        requirement_ids=("req.todo.add",),
        owned_paths=("packages/domain",),
        ports=(
            PortSpec(
                id="port.domain.command",
                name="Todo command",
                direction=PortDirection.INPUT,
                operation_id="op.domain.add",
                schema={"$ref": "#/$defs/TodoCommand"},
            ),
        ),
    )
    values = {
        "id": "architecture.todo",
        "project_id": "project.todo",
        "root_node_id": root.id,
        "target_pack": "nextjs.v1",
        "nodes": (root, domain),
        "edges": (
            ArchitectureEdge(
                id="edge.contains.domain",
                kind=EdgeKind.CONTAINS,
                source_node_id=root.id,
                target_node_id=domain.id,
            ),
            ArchitectureEdge(
                id="edge.call.domain",
                kind=EdgeKind.CALL,
                source_node_id=root.id,
                target_node_id=domain.id,
                source_port_id="port.app.command",
                target_port_id="port.domain.command",
            ),
        ),
        "contracts": (root_contract, domain_contract),
    }
    values.update(overrides)
    return ArchitectureSpec(**values)


def _release_records():
    task = BuildTask(
        id="task.domain",
        run_id="run.todo",
        node_id="node.domain",
        kind=TaskKind.IMPLEMENT,
        status=TaskStatus.SUCCEEDED,
        attempt=1,
    )
    source = Artifact(
        id="artifact.source",
        run_id="run.todo",
        kind=ArtifactKind.SOURCE,
        status=ArtifactStatus.AVAILABLE,
        digest=SHA256,
        uri="cas://sha256/" + SHA256,
        produced_by_task_id=task.id,
        requirement_ids=("req.todo.add",),
    )
    evidence = Evidence(
        id="evidence.acceptance",
        run_id="run.todo",
        task_id=task.id,
        node_id="node.app",
        kind=EvidenceKind.ACCEPTANCE,
        status=EvidenceStatus.PASSED,
        requirement_ids=("req.todo.add",),
        acceptance_scenario_ids=("scenario.todo.add",),
        artifact_ids=(source.id,),
    )
    run = BuildRun(
        id="run.todo",
        project_id="project.todo",
        spec_revision_id="revision.spec.1",
        architecture_revision_id="revision.architecture.1",
        status=RunStatus.SUCCEEDED,
        task_ids=(task.id,),
        evidence_ids=(evidence.id,),
        artifact_ids=(source.id,),
    )
    return run, (task,), (evidence,), (source,)


@pytest.mark.parametrize(
    ("model", "loader"),
    [
        (_project(), ProjectSpec.from_dict),
        (_architecture(), ArchitectureSpec.from_dict),
        (_architecture().contracts[0], Contract.from_dict),
    ],
)
def test_versioned_documents_round_trip_as_plain_json(model, loader):
    document = model.to_dict()

    assert document["schema_version"] == "2.0"
    assert json.loads(json.dumps(document)) == document
    assert loader(document) == model


def test_loader_rejects_unknown_fields_and_schema_versions():
    document = _project().to_dict()
    document["silent_model_guess"] = True

    with pytest.raises(ModelValidationError, match="unknown fields"):
        ProjectSpec.from_dict(document)

    document.pop("silent_model_guess")
    document["schema_version"] = "3.0"
    with pytest.raises(UnsupportedSchemaVersion, match="3.0"):
        ProjectSpec.from_dict(document)


def test_project_rejects_requirements_without_acceptance_oracles():
    uncovered = Requirement(
        id="req.todo.complete",
        title="Complete todo",
        statement="A user can complete an item.",
    )

    with pytest.raises(ModelValidationError, match="uncovered requirements"):
        _project(requirements=(*_project().requirements, uncovered))


def test_project_rejects_unknown_requirement_trace():
    scenario = AcceptanceScenario(
        id="scenario.unknown",
        title="Unknown requirement",
        when=("The user acts.",),
        then=("Something happens.",),
        requirement_ids=("req.unknown",),
        oracle=(
            {"action": "navigate", "value": "/"},
            {
                "action": "assert_visible",
                "locator": {"kind": "role", "value": "heading"},
            },
        ),
    )

    with pytest.raises(ModelValidationError, match="unknown requirements"):
        _project(acceptance_scenarios=(scenario,))


def test_acceptance_scenario_requires_a_bounded_executable_oracle():
    with pytest.raises(ModelValidationError, match="oracle cannot be empty"):
        AcceptanceScenario(
            id="scenario.no-oracle",
            title="No executable proof",
            when=("The user acts.",),
            then=("The result appears.",),
            requirement_ids=("req.todo.add",),
            oracle=(),
        )

    with pytest.raises(ModelValidationError, match="observable assertion"):
        AcceptanceScenario(
            id="scenario.no-assertion",
            title="No assertion",
            when=("The user acts.",),
            then=("The result appears.",),
            requirement_ids=("req.todo.add",),
            oracle=({"action": "navigate", "value": "/"},),
        )

    with pytest.raises(ModelValidationError, match="local URL path"):
        AcceptanceScenario(
            id="scenario.external-navigation",
            title="External navigation",
            when=("The user leaves the application.",),
            then=("An external page appears.",),
            requirement_ids=("req.todo.add",),
            oracle=(
                {"action": "navigate", "value": "https://attacker.invalid"},
                {
                    "action": "assert_visible",
                    "locator": {"kind": "role", "value": "heading"},
                },
            ),
        )


def test_stable_ids_and_json_values_fail_closed():
    with pytest.raises(ModelValidationError, match="stable"):
        Requirement(id="has spaces", title="Bad", statement="Bad id")

    with pytest.raises(ModelValidationError, match="non-JSON"):
        _project(metadata={"opaque": object()})


def test_architecture_validates_contracts_ports_containment_and_project_trace():
    project = _project()
    architecture = _architecture()

    architecture.validate_against_project(project)

    assert architecture.node_index["node.domain"].contract_id == "contract.domain"
    assert architecture.contract_index["contract.domain"].traced_requirement_ids == {
        "req.todo.add"
    }


def test_architecture_requires_real_typed_ports_for_call_edges():
    root, domain = _architecture().nodes

    with pytest.raises(ModelValidationError, match="requires source and target ports"):
        ArchitectureEdge(
            id="edge.untyped",
            kind=EdgeKind.CALL,
            source_node_id=root.id,
            target_node_id=domain.id,
        )


def test_architecture_rejects_missing_containment_and_fake_contract_allocation():
    architecture = _architecture()
    call_edge = next(
        edge for edge in architecture.edges if edge.kind is EdgeKind.CALL
    )

    with pytest.raises(ModelValidationError, match="contains parent"):
        _architecture(edges=(call_edge,))

    root, domain = architecture.nodes
    untraced_domain = ArchitectureNode(
        id=domain.id,
        name=domain.name,
        kind=domain.kind,
        contract_id=domain.contract_id,
        ports=domain.ports,
        requirement_ids=(),
        owned_paths=domain.owned_paths,
    )
    with pytest.raises(ModelValidationError, match="exactly match"):
        _architecture(nodes=(root, untraced_domain))


def test_architecture_rejects_requirement_drift_from_project_revision():
    project = _project(revision=2)
    architecture = _architecture()

    with pytest.raises(ModelValidationError, match="revision 1, not 2"):
        architecture.validate_against_project(project)


def test_runtime_records_round_trip_and_enforce_legal_transitions():
    task = BuildTask(
        id="task.domain",
        run_id="run.todo",
        node_id="node.domain",
        kind=TaskKind.IMPLEMENT,
    )

    ready = task.transitioned(TaskStatus.READY)
    running = ready.transitioned(TaskStatus.RUNNING)

    assert running.attempt == 1
    assert BuildTask.from_dict(running.to_dict()) == running
    with pytest.raises(ModelValidationError, match="invalid task status transition"):
        running.transitioned(TaskStatus.CACHED)


def test_passed_evidence_requires_an_immutable_artifact():
    with pytest.raises(ModelValidationError, match="immutable result artifact"):
        Evidence(
            id="evidence.empty",
            run_id="run.todo",
            kind=EvidenceKind.UNIT,
            status=EvidenceStatus.PASSED,
        )


def test_available_artifact_requires_content_address_and_uri():
    with pytest.raises(ModelValidationError, match="require digest and uri"):
        Artifact(
            id="artifact.source",
            run_id="run.todo",
            kind=ArtifactKind.SOURCE,
            status=ArtifactStatus.AVAILABLE,
        )

    with pytest.raises(ModelValidationError, match="SHA-256"):
        Artifact(
            id="artifact.source",
            run_id="run.todo",
            kind=ArtifactKind.SOURCE,
            digest="not-a-digest",
        )


def test_approvals_are_explicit_single_decisions():
    requested = Approval(
        id="approval.architecture",
        project_id="project.todo",
        run_id="run.todo",
        gate=ApprovalGate.ARCHITECTURE,
        revision_id="revision.architecture.1",
        requested_capabilities=("network:npm", "secret:vercel"),
    )

    approved = requested.decided(
        ApprovalStatus.APPROVED,
        decided_by="founder@example.test",
        reason="Architecture and authority reviewed.",
    )

    assert approved.status is ApprovalStatus.APPROVED
    assert Approval.from_dict(approved.to_dict()) == approved
    with pytest.raises(ModelValidationError, match="already been decided"):
        approved.decided(ApprovalStatus.REJECTED, decided_by="other")


def test_complete_requirement_to_release_trace_is_valid():
    project = _project()
    architecture = _architecture()
    run, tasks, evidence, artifacts = _release_records()

    validate_release_traceability(
        project=project,
        architecture=architecture,
        run=run,
        tasks=tasks,
        evidence=evidence,
        artifacts=artifacts,
    )


def test_succeeded_run_rejects_skipped_blocking_evidence():
    run, tasks, _, artifacts = _release_records()
    skipped = Evidence(
        id="evidence.acceptance",
        run_id=run.id,
        task_id=tasks[0].id,
        kind=EvidenceKind.ACCEPTANCE,
        status=EvidenceStatus.SKIPPED,
        requirement_ids=("req.todo.add",),
        acceptance_scenario_ids=("scenario.todo.add",),
    )

    with pytest.raises(
        ModelValidationError, match="passed acceptance evidence does not cover"
    ):
        run.validate_records(
            tasks=tasks,
            evidence=(skipped,),
            artifacts=artifacts,
            required_requirement_ids=("req.todo.add",),
            required_acceptance_scenario_ids=("scenario.todo.add",),
        )


def test_failed_acceptance_evidence_records_no_unobserved_coverage():
    record = Evidence(
        id="evidence.acceptance.error",
        run_id="run.todo",
        task_id="task.todo",
        kind=EvidenceKind.ACCEPTANCE,
        status=EvidenceStatus.ERROR,
        requirement_ids=("req.todo.add",),
        acceptance_scenario_ids=(),
    )

    assert not record.satisfies_gate
    assert record.acceptance_scenario_ids == ()


def test_release_rejects_scenario_evidence_with_incomplete_requirement_trace():
    project = _project()
    architecture = _architecture()
    run, tasks, _, artifacts = _release_records()
    incomplete = Evidence(
        id="evidence.acceptance",
        run_id=run.id,
        task_id=tasks[0].id,
        kind=EvidenceKind.ACCEPTANCE,
        status=EvidenceStatus.PASSED,
        requirement_ids=("req.other",),
        acceptance_scenario_ids=("scenario.todo.add",),
        artifact_ids=(artifacts[0].id,),
    )

    with pytest.raises(ModelValidationError, match="unknown requirements"):
        validate_release_traceability(
            project=project,
            architecture=architecture,
            run=run,
            tasks=tasks,
            evidence=(incomplete,),
            artifacts=artifacts,
        )


def test_run_rejects_unknown_task_dependencies():
    run, _, evidence, artifacts = _release_records()
    task = BuildTask(
        id="task.domain",
        run_id=run.id,
        node_id="node.domain",
        kind=TaskKind.IMPLEMENT,
        status=TaskStatus.SUCCEEDED,
        dependency_task_ids=("task.missing",),
        attempt=1,
    )

    with pytest.raises(ModelValidationError, match="unknown dependencies"):
        run.validate_records(tasks=(task,), evidence=evidence, artifacts=artifacts)


@pytest.mark.parametrize(
    ("owned_path", "reason"),
    [
        ("apps/we\x00b", "null byte"),
        ("apps/web/", "trailing slash"),
        ("apps/" + "w" * 256, "oversized component"),
    ],
)
def test_owned_paths_are_held_to_the_same_rules_as_the_path_guard(owned_path, reason):
    """models keeps its own copy of the path rules on purpose (it imports no
    sibling), so the copy has to be the same rules: paths.py refuses a null
    byte, a trailing slash and an oversized component, and so must this."""

    from richbuild.models import ArchitectureNode, ModelValidationError, NodeKind

    def node(path):
        return ArchitectureNode(
            id="node.web",
            name="Web",
            kind=NodeKind.UI,
            contract_id="contract:web",
            requirement_ids=("req.web",),
            owned_paths=(path,),
            ports=(),
        )

    assert node("apps/web").owned_paths == ("apps/web",)
    with pytest.raises(ModelValidationError, match=reason):
        node(owned_path)
