"""Deterministic baseline architecture proposals for the first web target pack.

This module does not replace an architect model. It supplies a validated, explainable
baseline and, critically, keeps every approved requirement and acceptance scenario in
the contracts it creates. An architect may propose a different graph, but it must pass
the same typed validation and user approval gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .models import (
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpecV2,
    ContractV2,
    EdgeKind,
    NodeKind,
    OperationContract,
    PortDirection,
    PortSpec,
    ProjectSpecV2,
    Requirement,
    RequirementKind,
)
from .target_packs.nextjs import NextJsTargetPack


_DATA = re.compile(
    r"\b(store|persist|database|data|record|task|document|message|profile|upload|delete)\w*\b",
    re.I,
)
_EXTERNAL = re.compile(
    r"\b(api|payment|email|webhook|integration|external|provider|stripe|github|search)\w*\b",
    re.I,
)
_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["request"],
    "properties": {
        "request": {"type": "string", "minLength": 1},
    },
}
_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["result"],
    "properties": {
        "result": {"type": "string"},
    },
}


@dataclass(frozen=True, slots=True)
class ArchitectureProposal:
    architecture: ArchitectureSpecV2
    decisions: tuple[str, ...]
    risks: tuple[str, ...]
    requires_approval: bool = True


def plan_nextjs_architecture(project: ProjectSpecV2) -> ArchitectureProposal:
    """Create a deterministic full-stack proposal without reducing product semantics."""

    all_requirements = tuple(requirement.id for requirement in project.requirements)
    searchable = " ".join(
        [project.goal, *project.constraints]
        + [
            f"{requirement.title} {requirement.statement}"
            for requirement in project.requirements
        ]
    )
    discovery = project.metadata.get("discovery", {})
    needs_data = bool(_DATA.search(searchable) or discovery.get("data_policy"))
    needs_adapters = bool(
        _EXTERNAL.search(searchable) or discovery.get("integration_failure_policy")
    )

    data_requirements = _matching_requirements(project.requirements, _DATA)
    adapter_requirements = _matching_requirements(project.requirements, _EXTERNAL)
    functional_requirements = tuple(
        requirement.id
        for requirement in project.requirements
        if requirement.kind is RequirementKind.FUNCTIONAL
    )
    if needs_data and not data_requirements:
        data_requirements = functional_requirements or all_requirements
    if needs_adapters and not adapter_requirements:
        adapter_requirements = functional_requirements or all_requirements

    root_contract = _contract(
        project,
        node_id="app",
        contract_id="contract:app",
        requirement_ids=all_requirements,
        operation_prefix="accept",
    )
    web_contract = _contract(
        project,
        node_id="web",
        contract_id="contract:web",
        requirement_ids=all_requirements,
        operation_prefix="present",
    )
    domain_contract = _contract(
        project,
        node_id="domain",
        contract_id="contract:domain",
        requirement_ids=all_requirements,
        operation_prefix="execute",
    )
    contracts = [root_contract, web_contract, domain_contract]

    web_out = PortSpec(
        id="web.command.out",
        name="User command",
        direction=PortDirection.OUTPUT,
        schema=_REQUEST_SCHEMA,
        operation_id=web_contract.operations[0].id,
    )
    web_in = PortSpec(
        id="web.result.in",
        name="Rendered result",
        direction=PortDirection.INPUT,
        schema=_RESPONSE_SCHEMA,
        operation_id=web_contract.operations[0].id,
    )
    domain_in = PortSpec(
        id="domain.command.in",
        name="Validated command",
        direction=PortDirection.INPUT,
        schema=_REQUEST_SCHEMA,
        operation_id=domain_contract.operations[0].id,
    )
    domain_out = PortSpec(
        id="domain.capability.out",
        name="Capability request",
        direction=PortDirection.OUTPUT,
        schema=_REQUEST_SCHEMA,
        operation_id=domain_contract.operations[0].id,
    )

    nodes = [
        ArchitectureNode(
            id="app",
            name=f"{project.name} application",
            kind=NodeKind.APPLICATION,
            contract_id=root_contract.id,
            requirement_ids=all_requirements,
            owned_paths=(".rich/generated",),
            metadata={"role": "composition_root"},
        ),
        ArchitectureNode(
            id="web",
            name="Web application and accessible UI",
            kind=NodeKind.UI,
            contract_id=web_contract.id,
            ports=(web_out, web_in),
            requirement_ids=all_requirements,
            owned_paths=("apps/web", "packages/ui"),
            metadata={"framework": "nextjs-app-router"},
        ),
        ArchitectureNode(
            id="domain",
            name="Product domain and use cases",
            kind=NodeKind.DOMAIN,
            contract_id=domain_contract.id,
            ports=(domain_in, domain_out),
            requirement_ids=all_requirements,
            owned_paths=("packages/contracts", "packages/domain"),
            metadata={"rule": "framework_independent"},
        ),
    ]
    edges = [
        _contains("app", "web"),
        _contains("app", "domain"),
        ArchitectureEdge(
            id="call:web:domain",
            kind=EdgeKind.CALL,
            source_node_id="web",
            target_node_id="domain",
            source_port_id=web_out.id,
            target_port_id=domain_in.id,
            metadata={
                "semantics": "validated user command enters the domain",
                "response_contract": "domain operation output returns to the web caller",
            },
        ),
    ]
    decisions = [
        "Use the pinned Next.js App Router monorepo target pack.",
        "Keep domain behavior framework-independent and expose it through typed contracts.",
        "Treat browser responses as operation returns rather than a reverse dependency edge.",
    ]
    risks = []

    if needs_data:
        data_contract = _contract(
            project,
            node_id="data",
            contract_id="contract:data",
            requirement_ids=data_requirements,
            operation_prefix="persist",
        )
        contracts.append(data_contract)
        data_in = PortSpec(
            id="data.request.in",
            name="Persistence request",
            direction=PortDirection.INPUT,
            schema=_REQUEST_SCHEMA,
            operation_id=data_contract.operations[0].id,
        )
        nodes.extend(
            [
                ArchitectureNode(
                    id="data",
                    name="PostgreSQL persistence boundary",
                    kind=NodeKind.DATA,
                    contract_id=data_contract.id,
                    ports=(data_in,),
                    requirement_ids=data_requirements,
                    owned_paths=("packages/db",),
                    metadata={"orm": "drizzle", "database": "postgresql"},
                ),
                ArchitectureNode(
                    id="postgres",
                    name="PostgreSQL resource",
                    kind=NodeKind.RESOURCE,
                    contract_id=None,
                    owned_paths=(),
                    metadata={"resource": "postgresql", "preview_branching": "neon"},
                ),
            ]
        )
        edges.extend(
            [
                _contains("app", "data"),
                _contains("app", "postgres"),
                ArchitectureEdge(
                    id="capability:domain:data",
                    kind=EdgeKind.CAPABILITY,
                    source_node_id="domain",
                    target_node_id="data",
                    source_port_id=domain_out.id,
                    target_port_id=data_in.id,
                    metadata={"semantics": "domain requests persistence through a port"},
                ),
                ArchitectureEdge(
                    id="resource:data:postgres",
                    kind=EdgeKind.RESOURCE,
                    source_node_id="data",
                    target_node_id="postgres",
                    metadata={"lifetime": "project_preview"},
                ),
            ]
        )
        decisions.append("Use source-controlled Drizzle migrations against PostgreSQL.")

    if needs_adapters:
        adapter_contract = _contract(
            project,
            node_id="adapters",
            contract_id="contract:adapters",
            requirement_ids=adapter_requirements,
            operation_prefix="integrate",
        )
        contracts.append(adapter_contract)
        adapter_in = PortSpec(
            id="adapters.request.in",
            name="External capability request",
            direction=PortDirection.INPUT,
            schema=_REQUEST_SCHEMA,
            operation_id=adapter_contract.operations[0].id,
        )
        nodes.append(
            ArchitectureNode(
                id="adapters",
                name="External service adapters",
                kind=NodeKind.ADAPTER,
                contract_id=adapter_contract.id,
                ports=(adapter_in,),
                requirement_ids=adapter_requirements,
                owned_paths=("packages/adapters",),
                metadata={
                    "network": "denied_in_unit_tests",
                    "secrets": "executor_capability_handles_only",
                },
            )
        )
        edges.extend(
            [
                _contains("app", "adapters"),
                ArchitectureEdge(
                    id="capability:domain:adapters",
                    kind=EdgeKind.CAPABILITY,
                    source_node_id="domain",
                    target_node_id="adapters",
                    source_port_id=domain_out.id,
                    target_port_id=adapter_in.id,
                    metadata={"semantics": "domain calls an approved external capability"},
                ),
            ]
        )
        decisions.append("Isolate external services behind provider-neutral adapters.")
        if not discovery.get("integration_failure_policy"):
            risks.append(
                "External integration requirements exist without an explicit outage policy."
            )

    architecture = ArchitectureSpecV2(
        id=f"architecture:{project.id}:r{project.revision}",
        project_id=project.id,
        root_node_id="app",
        target_pack=NextJsTargetPack.target_pack_id,
        nodes=tuple(nodes),
        edges=tuple(edges),
        contracts=tuple(contracts),
        project_spec_revision=project.revision,
        revision=1,
        metadata={
            "planner": "rich_v2.deterministic_web_baseline",
            "requires_user_approval": True,
        },
    )
    architecture.validate_against_project(project)
    return ArchitectureProposal(
        architecture=architecture,
        decisions=tuple(decisions),
        risks=tuple(risks),
    )


def _contract(
    project: ProjectSpecV2,
    *,
    node_id: str,
    contract_id: str,
    requirement_ids: Iterable[str],
    operation_prefix: str,
) -> ContractV2:
    requirement_set = set(requirement_ids)
    requirements = [
        requirement
        for requirement in project.requirements
        if requirement.id in requirement_set
    ]
    if not requirements:
        raise ValueError(f"cannot create empty contract for {node_id!r}")
    operations = tuple(
        OperationContract(
            id=f"operation:{node_id}:{_safe_fragment(requirement.id)}",
            name=f"{operation_prefix}_{_safe_fragment(requirement.id)}",
            description=requirement.statement,
            input_schema=_REQUEST_SCHEMA,
            output_schema=_RESPONSE_SCHEMA,
            requirement_ids=(requirement.id,),
        )
        for requirement in requirements
    )
    relevant_scenarios = [
        scenario.to_dict()
        for scenario in project.acceptance_scenarios
        if set(scenario.requirement_ids) & requirement_set
    ]
    return ContractV2(
        id=contract_id,
        node_id=node_id,
        operations=operations,
        metadata={
            "requirements": [requirement.to_dict() for requirement in requirements],
            "acceptance_scenarios": relevant_scenarios,
            "semantic_source": "approved_project_spec",
        },
    )


def _matching_requirements(
    requirements: Iterable[Requirement], pattern: re.Pattern[str]
) -> tuple[str, ...]:
    return tuple(
        requirement.id
        for requirement in requirements
        if pattern.search(f"{requirement.title} {requirement.statement}")
    )


def _safe_fragment(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()


def _contains(parent: str, child: str) -> ArchitectureEdge:
    return ArchitectureEdge(
        id=f"contains:{parent}:{child}",
        kind=EdgeKind.CONTAINS,
        source_node_id=parent,
        target_node_id=child,
    )
