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
    CharSet,
    ContractV2,
    EdgeKind,
    Invariant,
    NodeKind,
    ObligationExample,
    ObligationRelation,
    OperationContract,
    PortDirection,
    PortSpec,
    ProjectSpecV2,
    ProofObligation,
    RecordField,
    Requirement,
    RequirementKind,
    ValueType,
    ValueTypeKind,
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
_MAX_TEXT = 1024
_TEXT = ValueType(
    kind=ValueTypeKind.STRING,
    max_length=_MAX_TEXT,
    char_set=CharSet.ASCII_PRINTABLE,
)
# A deterministic planner cannot know a requirement's real domain, so it
# declares the smallest honest one: a bounded, identified request and a bounded
# outcome.  These are placeholders in content but not in kind -- unlike the
# untyped `{request: string}` they replace, they are finitely sampleable, so an
# obligation over them is something a gate can actually run.  An architect model
# is what replaces the content (see docs/v2-architecture.md).
_REQUEST_TYPE = ValueType(
    kind=ValueTypeKind.RECORD,
    record_fields=(
        RecordField(
            "requestId",
            ValueType(
                kind=ValueTypeKind.STRING,
                min_length=1,
                max_length=64,
                char_set=CharSet.ASCII_IDENTIFIER,
            ),
        ),
        RecordField("payload", _TEXT),
    ),
)
_RESPONSE_TYPE = ValueType(
    kind=ValueTypeKind.RECORD,
    record_fields=(
        RecordField(
            "status",
            ValueType(kind=ValueTypeKind.ENUM, members=("accepted", "rejected")),
        ),
        RecordField("detail", _TEXT),
    ),
)
_REQUEST_SCHEMA = _REQUEST_TYPE.json_schema()
_RESPONSE_SCHEMA = _RESPONSE_TYPE.json_schema()


@dataclass(frozen=True, slots=True)
class ArchitectureProposal:
    architecture: ArchitectureSpecV2
    decisions: tuple[str, ...]
    risks: tuple[str, ...]
    requires_approval: bool = True
    # Who actually produced this. A template and a model are not
    # interchangeable, and when a model attempt fails and the baseline stands in
    # for it, the reviewer has to be told rather than left to guess.
    source: str = "planner"


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
    operations: list[OperationContract] = []
    obligations: list[ProofObligation] = []
    invariants: list[Invariant] = []
    for requirement in requirements:
        fragment = _safe_fragment(requirement.id) or "requirement"
        operation_id = f"operation:{node_id}:{fragment}"
        obligation_id = f"obligation:{node_id}:{fragment}:example"
        operations.append(
            OperationContract(
                id=operation_id,
                name=f"{operation_prefix}_{fragment}",
                description=requirement.statement,
                input_schema=_REQUEST_SCHEMA,
                output_schema=_RESPONSE_SCHEMA,
                requirement_ids=(requirement.id,),
                input_type=_REQUEST_TYPE,
                output_type=_RESPONSE_TYPE,
            )
        )
        # One ground example per operation, always.  It is the only relation a
        # deterministic planner can assert without inventing semantics, and it
        # is what makes every stronger obligation an architect adds later
        # non-vacuous: the anti-vacuity rule is satisfied by construction.
        obligations.append(
            ProofObligation(
                id=obligation_id,
                relation=ObligationRelation.EXAMPLE,
                subject_operation_id=operation_id,
                requirement_ids=(requirement.id,),
                example=ObligationExample(
                    argument={
                        "requestId": fragment[:64],
                        "payload": _printable(requirement.title),
                    },
                    result={
                        "status": "accepted",
                        "detail": _printable(requirement.statement),
                    },
                ),
                description=(
                    f"{operation_prefix} accepts a well-formed request for "
                    f"{requirement.id}."
                ),
            )
        )
        if requirement.kind is not RequirementKind.FUNCTIONAL:
            # A quality constraint is already phrased as a property of the
            # whole system rather than a step through it, so it is an invariant
            # verbatim.  Until now these reached the build only as acceptance
            # scenarios; this is the first place they survive as invariants.
            invariants.append(
                Invariant(
                    id=f"invariant:{node_id}:{fragment}",
                    statement=requirement.statement,
                    requirement_ids=(requirement.id,),
                    obligation_ids=(obligation_id,),
                )
            )
    relevant_scenarios = [
        scenario.to_dict()
        for scenario in project.acceptance_scenarios
        if set(scenario.requirement_ids) & requirement_set
    ]
    return ContractV2(
        id=contract_id,
        node_id=node_id,
        operations=tuple(operations),
        invariants=tuple(invariants),
        obligations=tuple(obligations),
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


def _printable(text: str) -> str:
    """Coerce approved prose into the declared character set, losslessly enough.

    An example value has to inhabit the type it illustrates, and product prose
    routinely carries curly quotes and dashes that ``ASCII_PRINTABLE`` does not.
    Substituting is honest here because the example demonstrates shape, not
    wording; the requirement's own text is carried verbatim elsewhere in the
    contract.
    """

    alphabet = set(CharSet.ASCII_PRINTABLE.alphabet)
    coerced = "".join(character if character in alphabet else "?" for character in text)
    return coerced[:_MAX_TEXT] or "?"


def _contains(parent: str, child: str) -> ArchitectureEdge:
    return ArchitectureEdge(
        id=f"contains:{parent}:{child}",
        kind=EdgeKind.CONTAINS,
        source_node_id=parent,
        target_node_id=child,
    )
