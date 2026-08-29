from dataclasses import replace

import pytest

from richbuild.compiler import (
    CompilationError,
    DiagnosticCode,
    compile_architecture,
    validate_architecture,
)
from richbuild.models import (
    AcceptanceScenario,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpec,
    Contract,
    EdgeKind,
    NodeKind,
    OperationContract,
    PortDirection,
    PortSpec,
    ProjectSpec,
    Requirement,
)


def _project(*requirement_ids: str) -> ProjectSpec:
    requirements = tuple(
        Requirement(
            id=requirement_id,
            title=requirement_id,
            statement=f"Implement {requirement_id}",
        )
        for requirement_id in requirement_ids
    )
    return ProjectSpec(
        id="project.compiler",
        name="Compiler fixture",
        goal="Compile a deterministic graph",
        audiences=("engineer",),
        requirements=requirements,
        acceptance_scenarios=(
            AcceptanceScenario(
                id="scenario.all",
                title="All requirements work",
                when=("the application runs",),
                then=("all declared behavior is available",),
                requirement_ids=tuple(requirement_ids),
                oracle=(
                    {"action": "navigate", "value": "/"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "role", "value": "heading"},
                    },
                ),
            ),
        ),
    )


def _contract(node_id: str, requirement_id: str) -> Contract:
    return Contract(
        id=f"contract.{node_id}",
        node_id=node_id,
        operations=(
            OperationContract(
                id=f"operation.{node_id}",
                name="run",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                requirement_ids=(requirement_id,),
            ),
        ),
    )


def _port(node_id: str, direction: PortDirection) -> PortSpec:
    return PortSpec(
        id=f"port.{node_id}.{direction.value}",
        name=direction.value,
        direction=direction,
        schema={"type": "object"},
        operation_id=f"operation.{node_id}",
    )


def _node(
    node_id: str,
    requirement_id: str,
    *,
    ports: tuple[PortSpec, ...] = (),
    owned_path: str | None = None,
) -> ArchitectureNode:
    return ArchitectureNode(
        id=node_id,
        name=node_id,
        kind=NodeKind.MODULE,
        contract_id=f"contract.{node_id}",
        ports=ports,
        requirement_ids=(requirement_id,),
        owned_paths=((owned_path or f"packages/{node_id}"),),
    )


def _linear_architecture() -> tuple[ProjectSpec, ArchitectureSpec]:
    project = _project("requirement.root", "requirement.parse", "requirement.render")
    root = _node("root", "requirement.root", owned_path="apps/web")
    parse = _node(
        "parse",
        "requirement.parse",
        ports=(_port("parse", PortDirection.OUTPUT),),
    )
    render = _node(
        "render",
        "requirement.render",
        ports=(_port("render", PortDirection.INPUT),),
    )
    edges = (
        ArchitectureEdge(
            id="edge.contains.render",
            kind=EdgeKind.CONTAINS,
            source_node_id="root",
            target_node_id="render",
        ),
        ArchitectureEdge(
            id="edge.data",
            kind=EdgeKind.DATA,
            source_node_id="parse",
            target_node_id="render",
            source_port_id="port.parse.output",
            target_port_id="port.render.input",
        ),
        ArchitectureEdge(
            id="edge.contains.parse",
            kind=EdgeKind.CONTAINS,
            source_node_id="root",
            target_node_id="parse",
        ),
    )
    architecture = ArchitectureSpec(
        id="architecture.compiler",
        project_id=project.id,
        root_node_id="root",
        target_pack="nextjs",
        nodes=(render, root, parse),
        edges=edges,
        contracts=(
            _contract("render", "requirement.render"),
            _contract("root", "requirement.root"),
            _contract("parse", "requirement.parse"),
        ),
    )
    return project, architecture


def test_compilation_is_deterministic_and_emits_reverse_consumers():
    project, architecture = _linear_architecture()

    first = compile_architecture(architecture, project)
    reordered = replace(
        architecture,
        nodes=tuple(reversed(architecture.nodes)),
        edges=tuple(reversed(architecture.edges)),
        contracts=tuple(reversed(architecture.contracts)),
    )
    second = compile_architecture(reordered, project)

    assert first.to_dict() == second.to_dict()
    assert [task.node_id for task in first.tasks] == ["parse", "render", "root"]
    assert first.task_index["parse"].dependency_ids == ()
    assert first.task_index["parse"].consumer_ids == ("render", "root")
    assert first.task_index["render"].dependency_ids == ("parse",)
    assert first.task_index["render"].consumer_ids == ("root",)
    assert first.task_index["root"].dependency_ids == ("parse", "render")
    assert first.task_index["root"].consumer_ids == ()


@pytest.mark.parametrize("edge_kind", [EdgeKind.CALL, EdgeKind.CAPABILITY])
def test_consumer_edges_compile_provider_and_resource_dependencies_first(edge_kind):
    project = _project(
        "requirement.root",
        "requirement.consumer",
        "requirement.provider",
    )
    root = _node("root", "requirement.root", owned_path="apps/web")
    consumer = _node(
        "consumer",
        "requirement.consumer",
        ports=(_port("consumer", PortDirection.OUTPUT),),
    )
    provider = _node(
        "provider",
        "requirement.provider",
        ports=(_port("provider", PortDirection.INPUT),),
    )
    resource = ArchitectureNode(
        id="database",
        name="Database",
        kind=NodeKind.RESOURCE,
        contract_id=None,
        owned_paths=("infra/database",),
    )
    architecture = ArchitectureSpec(
        id=f"architecture.{edge_kind.value}",
        project_id=project.id,
        root_node_id="root",
        target_pack="nextjs",
        nodes=(root, consumer, provider, resource),
        edges=(
            ArchitectureEdge("contains.consumer", EdgeKind.CONTAINS, "root", "consumer"),
            ArchitectureEdge("contains.provider", EdgeKind.CONTAINS, "root", "provider"),
            ArchitectureEdge("contains.database", EdgeKind.CONTAINS, "root", "database"),
            ArchitectureEdge(
                f"{edge_kind.value}.provider",
                edge_kind,
                "consumer",
                "provider",
                "port.consumer.output",
                "port.provider.input",
            ),
            ArchitectureEdge(
                "resource.database",
                EdgeKind.RESOURCE,
                "provider",
                "database",
            ),
        ),
        contracts=(
            _contract("root", "requirement.root"),
            _contract("consumer", "requirement.consumer"),
            _contract("provider", "requirement.provider"),
        ),
    )

    compiled = compile_architecture(architecture, project)

    assert [task.node_id for task in compiled.tasks] == [
        "provider",
        "consumer",
        "root",
    ]
    assert "database" not in compiled.task_index
    assert compiled.task_index["provider"].dependency_ids == ()
    assert compiled.task_index["consumer"].dependency_ids == ("provider",)
    assert compiled.task_index["provider"].consumer_ids == ("consumer", "root")


def test_event_cycles_are_async_but_synchronous_cycles_fail_closed():
    project = _project("requirement.root", "requirement.a", "requirement.b")
    root = _node("root", "requirement.root", owned_path="apps/web")
    a = _node(
        "a",
        "requirement.a",
        ports=(
            _port("a", PortDirection.INPUT),
            _port("a", PortDirection.OUTPUT),
        ),
    )
    b = _node(
        "b",
        "requirement.b",
        ports=(
            _port("b", PortDirection.INPUT),
            _port("b", PortDirection.OUTPUT),
        ),
    )
    containment = (
        ArchitectureEdge("contains.a", EdgeKind.CONTAINS, "root", "a"),
        ArchitectureEdge("contains.b", EdgeKind.CONTAINS, "root", "b"),
    )
    event_edges = (
        ArchitectureEdge(
            "event.a-b",
            EdgeKind.EVENT,
            "a",
            "b",
            "port.a.output",
            "port.b.input",
        ),
        ArchitectureEdge(
            "event.b-a",
            EdgeKind.EVENT,
            "b",
            "a",
            "port.b.output",
            "port.a.input",
        ),
    )
    architecture = ArchitectureSpec(
        id="architecture.cycles",
        project_id=project.id,
        root_node_id="root",
        target_pack="nextjs",
        nodes=(root, a, b),
        edges=containment + event_edges,
        contracts=(
            _contract("root", "requirement.root"),
            _contract("a", "requirement.a"),
            _contract("b", "requirement.b"),
        ),
    )

    assert compile_architecture(architecture, project).tasks

    synchronous = replace(
        architecture,
        edges=containment
        + (
            replace(event_edges[0], id="data.a-b", kind=EdgeKind.DATA),
            replace(event_edges[1], id="data.b-a", kind=EdgeKind.DATA),
        ),
    )
    with pytest.raises(CompilationError) as caught:
        compile_architecture(synchronous, project)

    cycle = next(
        item
        for item in caught.value.diagnostics
        if item.code is DiagnosticCode.SYNCHRONOUS_CYCLE
    )
    assert cycle.node_ids == ("a", "b")
    assert cycle.edge_ids == ("data.a-b", "data.b-a")


def test_requirement_allocation_is_checked_against_exact_project_revision():
    project, architecture = _linear_architecture()
    expanded_project = replace(
        project,
        requirements=project.requirements
        + (
            Requirement(
                id="requirement.export",
                title="Export",
                statement="Export the result",
            ),
        ),
        acceptance_scenarios=(
            replace(
                project.acceptance_scenarios[0],
                requirement_ids=project.acceptance_scenarios[0].requirement_ids
                + ("requirement.export",),
            ),
        ),
        revision=2,
    )

    diagnostics = validate_architecture(architecture, expanded_project)

    assert {
        item.code for item in diagnostics
    } >= {
        DiagnosticCode.PROJECT_REVISION_MISMATCH,
        DiagnosticCode.UNALLOCATED_REQUIREMENT,
    }
    missing = next(
        item
        for item in diagnostics
        if item.code is DiagnosticCode.UNALLOCATED_REQUIREMENT
    )
    assert missing.requirement_ids == ("requirement.export",)


def test_corrupted_references_and_duplicate_ownership_return_structured_diagnostics():
    project, architecture = _linear_architecture()
    root, parse, render = (
        architecture.node_index["root"],
        architecture.node_index["parse"],
        architecture.node_index["render"],
    )
    duplicate_parse = replace(parse, owned_paths=render.owned_paths)
    broken_edge = ArchitectureEdge(
        id="edge.broken",
        kind=EdgeKind.CONTAINS,
        source_node_id="root",
        target_node_id="missing",
    )

    # Frozen models are the normal boundary.  Corrupting one here simulates a
    # bad migration or untrusted process and proves the compiler revalidates.
    object.__setattr__(
        architecture,
        "nodes",
        (root, duplicate_parse, render),
    )
    object.__setattr__(
        architecture,
        "edges",
        architecture.edges + (broken_edge,),
    )

    diagnostics = validate_architecture(architecture, project)
    codes = {item.code for item in diagnostics}

    assert DiagnosticCode.DUPLICATE_OWNED_PATH in codes
    assert DiagnosticCode.UNKNOWN_EDGE_TARGET in codes
    with pytest.raises(CompilationError) as caught:
        compile_architecture(architecture, project)
    assert all(item.to_dict()["code"] for item in caught.value.diagnostics)


def test_overlapping_owned_path_prefixes_fail_closed():
    project, architecture = _linear_architecture()
    parse = replace(
        architecture.node_index["parse"],
        owned_paths=("apps/web/src/parser",),
    )
    overlapping = replace(
        architecture,
        nodes=(
            architecture.node_index["root"],
            parse,
            architecture.node_index["render"],
        ),
    )

    diagnostics = validate_architecture(overlapping, project)

    overlap = next(
        item
        for item in diagnostics
        if item.code is DiagnosticCode.OVERLAPPING_OWNED_PATH
    )
    assert overlap.node_ids == ("parse", "root")
    assert overlap.owned_paths == ("apps/web", "apps/web/src/parser")
    with pytest.raises(CompilationError):
        compile_architecture(overlapping, project)


def test_incompatible_port_schemas_fail_closed():
    project, architecture = _linear_architecture()
    render = architecture.node_index["render"]
    incompatible_port = replace(
        render.ports[0],
        schema={"type": "string"},
    )
    incompatible_render = replace(render, ports=(incompatible_port,))
    incompatible = replace(
        architecture,
        nodes=(
            architecture.node_index["root"],
            architecture.node_index["parse"],
            incompatible_render,
        ),
    )

    diagnostics = validate_architecture(incompatible, project)

    mismatch = next(
        item
        for item in diagnostics
        if item.code is DiagnosticCode.INCOMPATIBLE_PORT_SCHEMA
    )
    assert mismatch.edge_ids == ("edge.data",)
    with pytest.raises(CompilationError):
        compile_architecture(incompatible, project)
