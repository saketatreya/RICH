"""Turn an approved product spec into a proposed architecture, with a model.

``planner.py`` produces the same three-layer shape for every product, gives
every requirement to every layer, and types every operation as an untyped
request/response pair. It is a baseline, and it says so. What it cannot do is
decide what a particular product is made of.

This module asks a model that question and then refuses to take its word for
anything. The division is deliberate and is the same one v1 settled on: the
model supplies semantics -- which components exist, which requirement each
serves, what each operation takes and returns, which claims about those
operations are worth checking -- and deterministic code supplies structure.
Node identifiers, owned paths, containment edges, ports and the composition
root are derived here, never proposed, because getting them wrong is a
mechanical failure with a mechanical fix and there is no reason to spend model
attempts on it.

Nothing the model returns is trusted. A proposal is decoded into typed objects,
those are assembled into an ``ArchitectureSpec``, and that constructor is the
gate: unallocated requirements, a vacuous obligation, a predicate that does not
return a boolean, an operation whose declared type disagrees with its schema --
all of it fails there. When it does, the validator's own message is handed back
for a bounded repair attempt, because the thing that knows exactly what is
wrong is the thing that just rejected it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .budget import Usage
from .models import (
    MAX_ENUM_MEMBERS,
    MAX_RECORD_FIELDS,
    MAX_SAMPLE_SIZE,
    MAX_VALUE_LENGTH,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpec,
    Contract,
    EdgeKind,
    ErrorContract,
    Invariant,
    ModelValidationError,
    NodeKind,
    ObligationExample,
    OBLIGATION_SLOTS,
    ObligationRelation,
    ObligationTier,
    OperationContract,
    ProjectSpec,
    PortDirection,
    PortSpec,
    ProofObligation,
    RequirementKind,
    ValueType,
    value_type_from_request,
    value_type_request_schema,
)
from .planner import ArchitectureProposal, plan_nextjs_architecture
from .providers import GenerationRole, ModelGateway, ModelRequest


class ArchitectProposalError(ValueError):
    """A model's decomposition cannot be assembled into an architecture."""


# The Next.js pack has a fixed workspace layout, so these are the layers it can
# express. Decomposing a small application into a dozen feature nodes would be
# worse engineering, not better: what a layered application actually has is
# layers, and what varies between products is what lives in them.
LAYER_NODE_IDS: dict[str, str] = {
    "domain": "domain",
    "ui": "web",
    "data": "data",
    "adapter": "adapters",
}
LAYER_NODE_KINDS: dict[str, NodeKind] = {
    "domain": NodeKind.DOMAIN,
    "ui": NodeKind.UI,
    "data": NodeKind.DATA,
    "adapter": NodeKind.ADAPTER,
}
LAYER_OWNED_PATHS: dict[str, tuple[str, ...]] = {
    "domain": ("packages/contracts", "packages/domain"),
    "ui": ("apps/web", "packages/ui"),
    "data": ("packages/db",),
    "adapter": ("packages/adapters",),
}
LAYER_NAMES: dict[str, str] = {
    "domain": "Product domain and use cases",
    "ui": "Web application and accessible UI",
    "data": "Persistence boundary",
    "adapter": "External service adapters",
}
# A web product without a UI or without domain logic is not a decomposition
# error the model should be allowed to make.
REQUIRED_LAYERS = ("domain", "ui")
ROOT_NODE_ID = "app"
ROOT_OWNED_PATHS = (".rich/generated",)
# Who may call whom. Fixed rather than proposed: an architecture where the UI
# reaches past the domain into the database is not a variation on this design,
# it is a different one.
LAYER_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("ui", "domain"),
    ("domain", "data"),
    ("domain", "adapter"),
)
_OPERATION_NAME = re.compile(r"^[a-z][A-Za-z0-9]{0,63}$")
_MAX_OPERATIONS_PER_COMPONENT = 12
_MAX_COMPONENTS = len(LAYER_NODE_IDS)
_VALUE_TYPE_DEPTH = 3
_DEFAULT_SAMPLE_SIZE = 64
MODEL_SOURCE = "model"
# The baseline standing in for a model attempt that could not be assembled.
# Named, because a reviewer must never have to guess which one answered.
FALLBACK_SOURCE = "planner-fallback"


@dataclass(frozen=True, slots=True)
class ErrorProposal:
    code: str
    description: str


@dataclass(frozen=True, slots=True)
class OperationProposal:
    name: str
    description: str
    input_type: ValueType
    output_type: ValueType
    errors: tuple[ErrorProposal, ...] = ()


@dataclass(frozen=True, slots=True)
class ObligationProposal:
    relation: ObligationRelation
    operation: str
    witness: str | None = None
    predicate: str | None = None
    guard: str | None = None
    argument: Any = None
    result: Any = None
    sample_size: int | None = None


@dataclass(frozen=True, slots=True)
class ComponentProposal:
    layer: str
    purpose: str
    requirement_ids: tuple[str, ...]
    operations: tuple[OperationProposal, ...]
    obligations: tuple[ObligationProposal, ...] = ()


@dataclass(frozen=True, slots=True)
class DecompositionProposal:
    components: tuple[ComponentProposal, ...]
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def architect_response_schema(
    project: ProjectSpec | None = None,
) -> dict[str, Any]:
    """The exact shape a model must answer in.

    Given the project, requirement ids become an enum, so naming one that does
    not exist stops being a rejection and becomes unrepresentable. Everything
    else here is the same idea applied where a decoder can carry it.
    """

    value_type = value_type_request_schema(_VALUE_TYPE_DEPTH)
    operation = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": _OPERATION_NAME.pattern},
            "description": {"type": "string"},
            "input_type": value_type,
            "output_type": value_type,
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["code", "description"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["name", "description", "input_type", "output_type", "errors"],
        "additionalProperties": False,
    }
    obligation = {
        "type": "object",
        "properties": {
            "relation": {
                "type": "string",
                "enum": [member.value for member in ObligationRelation],
            },
            # Operation names, not free text. A live proposal put a whole
            # prose sentence in the guard slot, which a string type invites and
            # an operand slot should never accept.
            "operation": {"type": "string", "pattern": _OPERATION_NAME.pattern},
            "witness": _nullable_name(),
            "predicate": _nullable_name(),
            "guard": _nullable_name(),
            # An example's argument and result are arbitrary JSON, which no
            # schema here can constrain -- the operation's own declared type is
            # what decides whether they inhabit it, and that check happens in
            # the contract constructor rather than the decoder.
            "argument": {},
            "result": {},
            "sample_size": {
                "anyOf": [
                    {"type": "integer", "minimum": 1, "maximum": MAX_SAMPLE_SIZE},
                    {"type": "null"},
                ]
            },
        },
        "required": [
            "relation",
            "operation",
            "witness",
            "predicate",
            "guard",
            "argument",
            "result",
            "sample_size",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "rationale": {"type": "string"},
            "components": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_COMPONENTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "layer": {
                            "type": "string",
                            "enum": sorted(LAYER_NODE_IDS),
                        },
                        "purpose": {"type": "string"},
                        "requirement_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": _requirement_id_schema(project),
                        },
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": _MAX_OPERATIONS_PER_COMPONENT,
                            "items": operation,
                        },
                        "obligations": {"type": "array", "items": obligation},
                    },
                    "required": [
                        "layer",
                        "purpose",
                        "requirement_ids",
                        "operations",
                        "obligations",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["rationale", "components"],
        "additionalProperties": False,
    }


def _nullable_name() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "string", "pattern": _OPERATION_NAME.pattern},
            {"type": "null"},
        ]
    }


def _requirement_id_schema(project: ProjectSpec | None) -> dict[str, Any]:
    if project is None:
        return {"type": "string"}
    return {"type": "string", "enum": sorted(project.requirement_index)}


def decode_proposal(document: Mapping[str, Any]) -> DecompositionProposal:
    """Decode a model answer into typed objects, rejecting anything malformed."""

    if not isinstance(document, Mapping):
        raise ArchitectProposalError("architect answer must be an object")
    raw_components = document.get("components")
    if not isinstance(raw_components, Sequence) or isinstance(raw_components, str):
        raise ArchitectProposalError("architect answer requires a components array")
    if not raw_components:
        raise ArchitectProposalError("architect proposed no components")
    if len(raw_components) > _MAX_COMPONENTS:
        raise ArchitectProposalError(
            f"architect proposed more than {_MAX_COMPONENTS} components"
        )

    components: list[ComponentProposal] = []
    for entry in raw_components:
        if not isinstance(entry, Mapping):
            raise ArchitectProposalError("each component must be an object")
        layer = entry.get("layer")
        if layer not in LAYER_NODE_IDS:
            raise ArchitectProposalError(
                f"unknown layer {layer!r}; expected one of {sorted(LAYER_NODE_IDS)}"
            )
        operations = tuple(
            _decode_operation(item) for item in _sequence(entry, "operations", layer)
        )
        if not operations:
            raise ArchitectProposalError(
                f"component {layer!r} declares no operations"
            )
        if len(operations) > _MAX_OPERATIONS_PER_COMPONENT:
            raise ArchitectProposalError(
                f"component {layer!r} declares more than "
                f"{_MAX_OPERATIONS_PER_COMPONENT} operations"
            )
        names = [operation.name for operation in operations]
        if len(set(names)) != len(names):
            raise ArchitectProposalError(
                f"component {layer!r} declares duplicate operation names"
            )
        components.append(
            ComponentProposal(
                layer=layer,
                purpose=str(entry.get("purpose", "")).strip(),
                requirement_ids=tuple(
                    str(value)
                    for value in _sequence(entry, "requirement_ids", layer)
                ),
                operations=operations,
                obligations=tuple(
                    _decode_obligation(item, layer)
                    for item in _sequence(entry, "obligations", layer)
                ),
            )
        )

    layers = [component.layer for component in components]
    if len(set(layers)) != len(layers):
        raise ArchitectProposalError("architect proposed a layer more than once")
    missing = [layer for layer in REQUIRED_LAYERS if layer not in layers]
    if missing:
        raise ArchitectProposalError(
            f"architect omitted required layers: {missing}"
        )
    return DecompositionProposal(
        components=tuple(components),
        rationale=str(document.get("rationale", "")).strip(),
    )


def _sequence(entry: Mapping[str, Any], key: str, layer: Any) -> list[Any]:
    value = entry.get(key, [])
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ArchitectProposalError(f"component {layer!r} {key} must be an array")
    return list(value)


def _decode_operation(entry: Any) -> OperationProposal:
    if not isinstance(entry, Mapping):
        raise ArchitectProposalError("each operation must be an object")
    name = str(entry.get("name", ""))
    if not _OPERATION_NAME.fullmatch(name):
        raise ArchitectProposalError(
            f"operation name {name!r} must be lowerCamelCase, 1-64 characters"
        )
    try:
        input_type = value_type_from_request(entry.get("input_type") or {})
        output_type = value_type_from_request(entry.get("output_type") or {})
    except ModelValidationError as exc:
        raise ArchitectProposalError(f"operation {name!r} has an invalid type: {exc}")
    errors = []
    for item in entry.get("errors") or ():
        if not isinstance(item, Mapping):
            raise ArchitectProposalError(f"operation {name!r} has a malformed error")
        errors.append(
            ErrorProposal(
                code=str(item.get("code", "")).strip(),
                description=str(item.get("description", "")).strip(),
            )
        )
    return OperationProposal(
        name=name,
        description=str(entry.get("description", "")).strip(),
        input_type=input_type,
        output_type=output_type,
        errors=tuple(errors),
    )


def _decode_obligation(entry: Any, layer: Any) -> ObligationProposal:
    if not isinstance(entry, Mapping):
        raise ArchitectProposalError(f"component {layer!r} has a malformed obligation")
    relation = entry.get("relation")
    try:
        resolved = ObligationRelation(relation)
    except ValueError:
        raise ArchitectProposalError(
            f"unknown obligation relation {relation!r}"
        ) from None
    return ObligationProposal(
        relation=resolved,
        operation=str(entry.get("operation", "")),
        witness=_optional_name(entry.get("witness")),
        predicate=_optional_name(entry.get("predicate")),
        guard=_optional_name(entry.get("guard")),
        argument=entry.get("argument"),
        result=entry.get("result"),
        sample_size=(
            int(entry["sample_size"])
            if isinstance(entry.get("sample_size"), int)
            and not isinstance(entry.get("sample_size"), bool)
            else None
        ),
    )


def _optional_name(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def compile_decomposition(
    project: ProjectSpec,
    proposal: DecompositionProposal,
    *,
    target_pack: str,
    architecture_id: str | None = None,
) -> ArchitectureSpec:
    """Assemble a validated architecture from a decomposition.

    Every identifier, path and edge below is derived. The model chose what the
    components are and what they promise; it did not choose how they are wired,
    because that is not a judgement call.
    """

    known = set(project.requirement_index)
    by_layer = {component.layer: component for component in proposal.components}
    for component in proposal.components:
        unknown = set(component.requirement_ids) - known
        if unknown:
            raise ArchitectProposalError(
                f"component {component.layer!r} claims unknown requirements: "
                f"{sorted(unknown)}"
            )
    allocated = {
        requirement_id
        for component in proposal.components
        for requirement_id in component.requirement_ids
    }
    unallocated = known - allocated
    if unallocated:
        raise ArchitectProposalError(
            "every requirement must be served by at least one component; "
            f"unallocated: {sorted(unallocated)}"
        )

    ordering = sorted(LAYER_NODE_IDS)
    layers = sorted(by_layer, key=ordering.index)
    # Contracts first: a port describes what crosses a boundary, so the
    # provider's entry type has to exist before the consumer's port can name it.
    skipped: list[str] = []
    contracts_by_layer = {
        layer: _component_contract(
            project, by_layer[layer], LAYER_NODE_IDS[layer], skipped
        )
        for layer in layers
    }
    dependencies = tuple(
        (consumer, provider)
        for consumer, provider in LAYER_DEPENDENCIES
        if consumer in by_layer and provider in by_layer
    )

    root_contract = _root_contract(project)
    nodes: list[ArchitectureNode] = [
        ArchitectureNode(
            id=ROOT_NODE_ID,
            name=f"{project.name} application",
            kind=NodeKind.APPLICATION,
            contract_id=root_contract.id,
            requirement_ids=tuple(sorted(known)),
            owned_paths=ROOT_OWNED_PATHS,
            metadata={"role": "composition_root"},
        )
    ]
    contracts: list[Contract] = [root_contract]
    edges: list[ArchitectureEdge] = []

    for layer in layers:
        component = by_layer[layer]
        node_id = LAYER_NODE_IDS[layer]
        contract = contracts_by_layer[layer]
        contracts.append(contract)
        entry = _entry_operation(contract)
        ports = [
            PortSpec(
                id=f"{node_id}.in",
                name=f"{LAYER_NAMES[layer]} input",
                direction=PortDirection.INPUT,
                schema=entry.input_schema,
                operation_id=entry.id,
            )
        ]
        # One output port per provider, carrying the *provider's* entry input
        # type. A boundary is described by what the consumer must hand over,
        # not by what the consumer happens to return to someone else, and the
        # compiler enforces that the two sides agree.
        for consumer, provider in dependencies:
            if consumer != layer:
                continue
            provider_entry = _entry_operation(contracts_by_layer[provider])
            ports.append(
                PortSpec(
                    id=f"{node_id}.out.{LAYER_NODE_IDS[provider]}",
                    name=f"{LAYER_NAMES[layer]} call into {LAYER_NAMES[provider]}",
                    direction=PortDirection.OUTPUT,
                    schema=provider_entry.input_schema,
                    # No operation_id: a port may only cite an operation of its
                    # own node's contract, and the consumer has none of its own
                    # for an outbound call. The schema is the whole promise.
                    operation_id=None,
                )
            )
        nodes.append(
            ArchitectureNode(
                id=node_id,
                name=LAYER_NAMES[layer],
                kind=LAYER_NODE_KINDS[layer],
                contract_id=contract.id,
                requirement_ids=tuple(sorted(set(component.requirement_ids))),
                owned_paths=LAYER_OWNED_PATHS[layer],
                ports=tuple(ports),
                metadata={"purpose": component.purpose} if component.purpose else {},
            )
        )
        edges.append(
            ArchitectureEdge(
                id=f"contains:{ROOT_NODE_ID}:{node_id}",
                kind=EdgeKind.CONTAINS,
                source_node_id=ROOT_NODE_ID,
                target_node_id=node_id,
            )
        )

    for consumer, provider in dependencies:
        source = LAYER_NODE_IDS[consumer]
        target = LAYER_NODE_IDS[provider]
        edges.append(
            ArchitectureEdge(
                id=f"capability:{source}:{target}",
                kind=EdgeKind.CAPABILITY,
                source_node_id=source,
                target_node_id=target,
                source_port_id=f"{source}.out.{target}",
                target_port_id=f"{target}.in",
            )
        )

    architecture = ArchitectureSpec(
        id=architecture_id or f"architecture:{project.id}",
        project_id=project.id,
        root_node_id=ROOT_NODE_ID,
        target_pack=target_pack,
        nodes=tuple(nodes),
        edges=tuple(edges),
        contracts=tuple(contracts),
        project_spec_revision=project.revision,
        metadata={
            "source": "model_architect",
            "rationale": proposal.rationale,
            # What the vocabulary could not express, kept where a reviewer will
            # see it rather than silently discarded.
            "dropped_obligations": list(skipped),
        },
    )
    architecture.validate_against_project(project)
    return architecture


def _entry_operation(contract: Contract) -> OperationContract:
    """The operation a caller reaches first.

    The first operation a component declares, because that is the one the
    architect led with and because a boundary needs a single, stable answer to
    "what do I hand you".
    """

    return contract.operations[0]


def _root_contract(project: ProjectSpec) -> Contract:
    """The composition root promises the product, not any one capability.

    It traces every requirement because the assembled application is what
    delivers them; a layer traces only its share. One operation is enough to
    say that, and keeping it to one is what stops the root's contract growing
    with the product.
    """

    requirement_ids = tuple(sorted(project.requirement_index))
    compose = OperationContract(
        id=f"operation:{ROOT_NODE_ID}:compose",
        name="composeApplication",
        description=(
            "Wire every approved capability into one running application."
        ),
        input_schema=_COMPOSITION_INPUT.json_schema(),
        output_schema=_COMPOSITION_OUTPUT.json_schema(),
        requirement_ids=requirement_ids,
        input_type=_COMPOSITION_INPUT,
        output_type=_COMPOSITION_OUTPUT,
    )
    return Contract(
        id=f"contract:{ROOT_NODE_ID}",
        node_id=ROOT_NODE_ID,
        operations=(compose,),
        obligations=(
            ProofObligation(
                id=f"obligation:{ROOT_NODE_ID}:compose:example",
                relation=ObligationRelation.EXAMPLE,
                subject_operation_id=compose.id,
                requirement_ids=requirement_ids,
                example=ObligationExample(
                    argument={"requirementId": requirement_ids[0]},
                    result={"wired": True},
                ),
                description="The composition root wires an approved capability.",
            ),
        ),
        metadata={"role": "composition_root"},
    )


def _component_contract(
    project: ProjectSpec,
    component: ComponentProposal,
    node_id: str,
    skipped: list[str] | None = None,
) -> Contract:
    skipped = skipped if skipped is not None else []
    operations: list[OperationContract] = []
    by_name: dict[str, OperationContract] = {}
    requirement_ids = tuple(sorted(set(component.requirement_ids)))
    # Fit each operation's types to the examples this component declares for it,
    # before the contract is built. The declared bound and the declared example
    # are two guesses at one intent; the example is the one that carries it.
    arguments, results = _example_values(component)
    for proposed in component.operations:
        input_type = proposed.input_type.fitted_to(arguments.get(proposed.name, ()))
        output_type = proposed.output_type.fitted_to(results.get(proposed.name, ()))
        operation = OperationContract(
            id=f"operation:{node_id}:{proposed.name}",
            name=proposed.name,
            description=proposed.description or proposed.name,
            input_schema=input_type.json_schema(),
            output_schema=output_type.json_schema(),
            requirement_ids=requirement_ids,
            input_type=input_type,
            output_type=output_type,
            errors=_error_contracts(node_id, proposed),
        )
        operations.append(operation)
        by_name[proposed.name] = operation

    obligations = [
        _obligation(proposed, by_name, node_id, requirement_ids, index)
        for index, proposed in enumerate(component.obligations)
    ]
    obligations, dropped = _typeable_obligations(
        node_id, tuple(operations), tuple(obligations)
    )
    skipped.extend(dropped)
    invariants = tuple(
        Invariant(
            id=f"invariant:{node_id}:{_fragment(requirement_id)}",
            statement=project.requirement_index[requirement_id].statement,
            requirement_ids=(requirement_id,),
        )
        for requirement_id in requirement_ids
        if project.requirement_index[requirement_id].kind
        is not RequirementKind.FUNCTIONAL
    )
    return Contract(
        id=f"contract:{node_id}",
        node_id=node_id,
        operations=tuple(operations),
        invariants=invariants,
        obligations=tuple(obligations),
        metadata={"purpose": component.purpose} if component.purpose else {},
    )


def _typeable_obligations(
    node_id: str,
    operations: tuple[OperationContract, ...],
    obligations: tuple[ProofObligation, ...],
) -> tuple[list[ProofObligation], list[str]]:
    """Keep the claims that type-check; report the ones that do not.

    Some claims a model wants to make are simply outside this vocabulary. It
    reaches for "the title of the created task is valid" and the relations only
    offer "the whole output is valid", so the predicate cannot accept what it
    judges. That is a limit of the language, not an error in the design, and
    discarding a whole decomposition over one unexpressible property trades
    something valuable for something that was never going to work.

    Dropping weakens the specification and never the guarantee: an obligation
    that is not recorded is a claim not made, every obligation that survives is
    fully checked, and anti-vacuity only gets easier as constrained operations
    are removed. The drops are returned so a reviewer sees what was lost.
    """

    kept = list(obligations)
    dropped: list[str] = []
    # Bounded by the obligation count: each pass removes at most one, and the
    # real validator -- not a copy of its rules -- decides.
    for _ in range(len(obligations) + 1):
        try:
            Contract(id=f"contract:{node_id}", node_id=node_id,
                       operations=operations, obligations=tuple(kept))
        except ModelValidationError as exc:
            message = str(exc)
            culprit = next(
                (
                    item
                    for item in kept
                    if item.relation is not ObligationRelation.EXAMPLE
                    and item.id in message
                ),
                None,
            )
            if culprit is None:
                # Nothing identifiable to drop; let the caller's own
                # construction raise with the real message.
                return kept, dropped
            kept.remove(culprit)
            dropped.append(
                f"dropped {culprit.relation.value} on "
                f"{culprit.subject_operation_id.split(':')[-1]}: {message}"
            )
            continue
        break
    return kept, dropped


def _example_values(
    component: ComponentProposal,
) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    """Group a component's example arguments and results by operation name."""

    arguments: dict[str, list[Any]] = {}
    results: dict[str, list[Any]] = {}
    for proposed in component.obligations:
        if proposed.relation is not ObligationRelation.EXAMPLE:
            continue
        arguments.setdefault(proposed.operation, []).append(proposed.argument)
        results.setdefault(proposed.operation, []).append(proposed.result)
    return arguments, results


def _error_contracts(
    node_id: str, proposed: OperationProposal
) -> tuple[ErrorContract, ...]:
    """Normalise error codes rather than refusing them.

    ``ErrorContract`` wants a stable identifier, and a model writes
    ``"NOT FOUND"``. That is a spelling difference, not a design one, and the
    same normalisation already exists for requirement fragments.
    """

    contracts: list[ErrorContract] = []
    seen: set[str] = set()
    for index, error in enumerate(proposed.errors):
        code = re.sub(r"[^A-Za-z0-9._:/-]+", "_", error.code).strip("_").upper()
        if not code or code in seen:
            code = f"{code or 'ERROR'}_{index}"
        seen.add(code)
        contracts.append(
            ErrorContract(
                id=f"error:{node_id}:{proposed.name}:{index}",
                code=code[:128],
                description=error.description or "Unspecified failure.",
            )
        )
    return tuple(contracts)


def _obligation(
    proposed: ObligationProposal,
    by_name: Mapping[str, OperationContract],
    node_id: str,
    requirement_ids: tuple[str, ...],
    index: int,
) -> ProofObligation:
    def resolve(name: str | None, slot: str) -> str | None:
        if name is None:
            return None
        operation = by_name.get(name)
        if operation is None:
            raise ArchitectProposalError(
                f"obligation on {node_id!r} names unknown {slot} operation "
                f"{name!r}; declared operations are {sorted(by_name)}"
            )
        return operation.id

    subject = resolve(proposed.operation, "subject")
    # Which operand slots a relation may carry is a fixed table, not a design
    # decision -- and the schema requires all three keys on every obligation, so
    # a model that fills one in on an `example` was answering the shape it was
    # given. Null the ones this relation cannot hold rather than refusing it.
    allowed = OBLIGATION_SLOTS[proposed.relation]
    is_example = proposed.relation is ObligationRelation.EXAMPLE
    return ProofObligation(
        id=f"obligation:{node_id}:{proposed.operation}:{proposed.relation.value}:{index}",
        relation=proposed.relation,
        subject_operation_id=subject,
        requirement_ids=requirement_ids,
        tier=ObligationTier.SAMPLE,
        witness_operation_id=(
            resolve(proposed.witness, "witness")
            if "witness_operation_id" in allowed
            else None
        ),
        predicate_operation_id=(
            resolve(proposed.predicate, "predicate")
            if "predicate_operation_id" in allowed
            else None
        ),
        guard_operation_id=(
            resolve(proposed.guard, "guard")
            if "guard_operation_id" in allowed
            else None
        ),
        example=(
            ObligationExample(argument=proposed.argument, result=proposed.result)
            if is_example
            else None
        ),
        # A sample size is a knob, not a judgement. Supplying a default when the
        # model omits one turns a whole class of rejected proposals into
        # accepted ones without weakening anything.
        sample_size=(
            None if is_example else (proposed.sample_size or _DEFAULT_SAMPLE_SIZE)
        ),
    )


def _fragment(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower() or "requirement"


_COMPOSITION_INPUT = ValueType.from_dict(
    {
        "kind": "record",
        "record_fields": [
            {
                "name": "requirementId",
                "value_type": {
                    "kind": "string",
                    "min_length": 1,
                    "max_length": 128,
                    "char_set": "ascii_printable",
                },
            }
        ],
    }
)
_COMPOSITION_OUTPUT = ValueType.from_dict(
    {
        "kind": "record",
        "record_fields": [{"name": "wired", "value_type": {"kind": "boolean"}}],
    }
)


def architect_prompt(
    project: ProjectSpec, *, target_pack: str, repair: str | None = None
) -> tuple[str, str]:
    """Return the (system, user) prompt pair for one architect attempt."""

    system_prompt = (
        "You are the RICH architect. Decompose one approved product "
        "specification into the layers of a "
        f"{target_pack} application, and describe what each layer promises.\n"
        "\n"
        "Rules that are checked mechanically, so violating one wastes the "
        "attempt:\n"
        f"- Use only these layers: {sorted(LAYER_NODE_IDS)}. 'domain' and 'ui' "
        "are required; include 'data' only if something must outlive a "
        "request, and 'adapter' only if an external service is involved.\n"
        "- Every requirement id must appear in at least one component. Give a "
        "requirement to a layer when that layer is genuinely responsible for "
        "part of it, not to every layer.\n"
        "- Operation names are lowerCamelCase and unique within a component.\n"
        "- Types are concrete. Bound every integer and every string and list "
        "length, because a domain with no bounds cannot be sampled and any "
        "claim about it cannot be checked. The bounds themselves are bounded: "
        f"a string or list length is at most {MAX_VALUE_LENGTH}, a record has "
        f"at most {MAX_RECORD_FIELDS} fields, an enum at most "
        f"{MAX_ENUM_MEMBERS} members, types nest at most "
        f"{_VALUE_TYPE_DEPTH} deep, and a sample size is between 1 and "
        f"{MAX_SAMPLE_SIZE}. A record must declare at least one field, a "
        "list its element type, and an enum its members.\n"
        "- Every operation you constrain with a non-example obligation needs "
        "an 'example' obligation as well, and so does any operation you name "
        "as a predicate or guard. This is not a formality: the identity "
        "function satisfies 'idempotent' and 'round_trip', and a predicate "
        "that always returns true satisfies 'establishes'. The example is what "
        "rules those out.\n"
        "- An example's argument and result must be values that actually "
        "inhabit the operation's declared input and output types.\n"
        "- 'total' asserts that a declared error never happens, so only use it "
        "on an operation that declares errors.\n"
        "- 'idempotent' and 'preserves' need the output type to equal the "
        "input type. 'round_trip' needs the witness to accept the subject's "
        "output and return its input. A predicate must return a boolean and "
        "accept the type it judges.\n"
        "\n"
        "Prefer few, meaningful operations over one per requirement. Prefer "
        "obligations that a wrong implementation would actually fail."
    )
    document = {
        "project": {
            "id": project.id,
            "name": project.name,
            "goal": project.goal,
            "audiences": list(project.audiences),
            "constraints": list(project.constraints),
        },
        "requirements": [
            requirement.to_dict() for requirement in project.requirements
        ],
        "acceptance_scenarios": [
            scenario.to_dict() for scenario in project.acceptance_scenarios
        ],
    }
    user_prompt = (
        "Decompose this approved specification.\n"
        + json.dumps(document, ensure_ascii=False, sort_keys=True, indent=None)
    )
    if repair:
        user_prompt += (
            "\n\nA previous attempt was rejected. Fix exactly this and change "
            f"nothing else:\n{repair}"
        )
    return system_prompt, user_prompt


def assemble(
    project: ProjectSpec,
    document: Mapping[str, Any],
    *,
    target_pack: str,
    architecture_id: str | None = None,
) -> ArchitectureSpec:
    """Decode and assemble one model answer, or raise a repairable message."""

    proposal = decode_proposal(document)
    try:
        return compile_decomposition(
            project,
            proposal,
            target_pack=target_pack,
            architecture_id=architecture_id,
        )
    except ModelValidationError as exc:
        # Surfaced as a proposal error so a caller has one exception type to
        # feed back, and so a typed-model failure is never mistaken for a bug
        # in the assembly above it.
        raise ArchitectProposalError(str(exc)) from exc


def layers_of(architecture: ArchitectureSpec) -> tuple[str, ...]:
    """The layers an assembled architecture actually contains."""

    present = {node.id for node in architecture.nodes}
    return tuple(
        layer
        for layer, node_id in sorted(LAYER_NODE_IDS.items())
        if node_id in present
    )


def requirement_allocation(
    architecture: ArchitectureSpec,
) -> dict[str, tuple[str, ...]]:
    """Which nodes serve each requirement, ignoring the composition root."""

    allocation: dict[str, list[str]] = {}
    for node in architecture.nodes:
        if node.id == ROOT_NODE_ID:
            continue
        for requirement_id in node.requirement_ids:
            allocation.setdefault(requirement_id, []).append(node.id)
    return {key: tuple(sorted(value)) for key, value in sorted(allocation.items())}


@dataclass(frozen=True, slots=True)
class ArchitectOutcome:
    """One architecture, and an honest record of what it took to get it."""

    architecture: ArchitectureSpec
    rationale: str
    attempts: int
    rejections: tuple[str, ...]
    usage: Usage
    source: str = MODEL_SOURCE


# The prompt carries the whole approved spec and the response schema carries
# the unrolled type vocabulary, so this reservation is larger than a coding
# task's. It is still a bound: an architect that wants more than this has
# misunderstood the product, not run out of room.
ARCHITECT_MAX_INPUT_TOKENS = 64_000
ARCHITECT_MAX_OUTPUT_TOKENS = 16_000
ARCHITECT_TIMEOUT_SECONDS = 600.0
MAX_ARCHITECT_ATTEMPTS = 3


def propose_architecture(
    project: ProjectSpec,
    *,
    gateway: ModelGateway,
    provider: str,
    model: str,
    target_pack: str,
    run_id: str,
    task_id: str,
    correlation_id: str,
    max_cost_usd: Decimal,
    max_attempts: int = MAX_ARCHITECT_ATTEMPTS,
    max_input_tokens: int = ARCHITECT_MAX_INPUT_TOKENS,
    max_output_tokens: int = ARCHITECT_MAX_OUTPUT_TOKENS,
    timeout_seconds: float = ARCHITECT_TIMEOUT_SECONDS,
    architecture_id: str | None = None,
    initial_repair: str | None = None,
) -> ArchitectOutcome:
    """Ask a model for a decomposition, and keep asking only while it is learning.

    A rejected proposal is retried with the validator's own message attached,
    because the component that knows precisely what is wrong is the one that
    just refused it -- and a rejection names a fixable thing ("this example
    does not inhabit its type") rather than a vague one. The loop is bounded:
    an architect that cannot satisfy a typed constraint in a few attempts is
    not going to, and every attempt is charged whether it succeeds or not.
    """

    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    rejections: list[str] = []
    # A caller's correction and the validator's rejection travel the same
    # channel: both are "here is what is wrong with the last answer", and
    # giving a human a separate mechanism would mean two prompts to keep honest.
    repair: str | None = initial_repair
    for attempt in range(1, max_attempts + 1):
        system_prompt, user_prompt = architect_prompt(
            project, target_pack=target_pack, repair=repair
        )
        response = gateway.generate(
            ModelRequest(
                run_id=run_id,
                task_id=task_id,
                correlation_id=f"{correlation_id}:{attempt}",
                role=GenerationRole.ARCHITECT,
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=architect_response_schema(project),
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                max_cost_usd=max_cost_usd,
                timeout_seconds=timeout_seconds,
            )
        )
        document = response.parsed
        if not isinstance(document, Mapping):
            repair = "the answer was not a JSON object"
            rejections.append(repair)
            continue
        try:
            architecture = assemble(
                project,
                document,
                target_pack=target_pack,
                architecture_id=architecture_id,
            )
        except ArchitectProposalError as exc:
            repair = str(exc)
            rejections.append(repair)
            continue
        return ArchitectOutcome(
            architecture=architecture,
            rationale=str(document.get("rationale", "")).strip(),
            attempts=attempt,
            rejections=tuple(rejections),
            usage=gateway.usage,
            source=MODEL_SOURCE,
        )

    # No exception. v1's PLAN answers `{"is_leaf": true}` on every failure path
    # -- parse error, validation error, missing credentials -- because that
    # answer is always structurally valid and always buildable, so a bad plan
    # costs one node rather than the run. The architect's equivalent is the
    # deterministic baseline, which validates by construction. Raising here
    # made a mechanical slip fatal to a surface whose whole purpose is to give
    # a human something to react to.
    return ArchitectOutcome(
        architecture=plan_nextjs_architecture(project).architecture,
        rationale="",
        attempts=max_attempts,
        rejections=tuple(rejections),
        usage=gateway.usage,
        source=FALLBACK_SOURCE,
    )


@dataclass(frozen=True, slots=True)
class ModelArchitect:
    """An ``ArchitectProposer`` backed by one bounded model attempt.

    ``max_attempts`` is two, not the three ``propose_architecture`` allows on
    its own and not the one a first pass here used. The distinction that
    matters is what kind of failure it is. A mechanical slip -- a record
    declared with no fields, an example off by a character -- is precisely
    what an automatic retry is good at, and a human cannot correct it because
    a rejected draft never reaches them at all. A design disagreement is the
    opposite: no number of retries finds an answer the model was never asked
    for. So one repair for mechanics, and the review loop for everything else.
    """

    # A factory, not a gateway. An architecture proposal is not part of a run,
    # so the meaningful ceiling is "one proposal cannot cost more than this",
    # not "this process may only ever produce two proposals". A shared ledger
    # made the second draft fail on budget, which is not a limit anyone chose.
    gateway_factory: Callable[[], ModelGateway]
    provider: str
    model: str
    # The ceiling for the whole proposal, repairs included -- not for one
    # attempt. Reserving the entire allowance per attempt meant a single
    # mechanical repair exhausted the budget and the caller saw a refusal
    # instead of a design.
    max_cost_usd: Decimal = Decimal("2.00")
    max_attempts: int = 2
    project_prefix: str = "architect"

    @property
    def attempt_cost_usd(self) -> Decimal:
        return self.max_cost_usd / self.max_attempts

    def propose(
        self,
        spec: ProjectSpec,
        *,
        target_pack: str,
        repair: str | None = None,
    ) -> ArchitectureProposal:
        outcome = propose_architecture(
            spec,
            gateway=self.gateway_factory(),
            provider=self.provider,
            model=self.model,
            target_pack=target_pack,
            run_id=f"{self.project_prefix}:{spec.id}",
            task_id=f"{self.project_prefix}:{spec.id}:propose",
            correlation_id=f"{self.project_prefix}:{spec.id}:{spec.revision}",
            max_cost_usd=self.attempt_cost_usd,
            max_attempts=self.max_attempts,
            initial_repair=repair,
        )
        return ArchitectureProposal(
            architecture=outcome.architecture,
            decisions=(
                (outcome.rationale,) if outcome.rationale else ()
            ),
            # The rejections travel as risks so a fallback is legible: the
            # reviewer sees the baseline *and* what the model got wrong.
            risks=outcome.rejections,
            source=outcome.source,
        )
