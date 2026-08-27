"""An amendment rebuilds what it changed, and replays the rest.

This is the claim the whole modular thesis rests on, driven end to end against
a real model: build once, amend one requirement, and watch the components that
never served it replay their previous answer while the ones that did are
written again.

The point is not that it is faster. It is that the blast radius is *derived*
from the contracts rather than guessed, and that reusing an answer never
reuses a verdict -- every gate runs again on the second pass exactly as it did
on the first.
"""

from dataclasses import replace
import shutil
from pathlib import Path

import pytest

from richbuild.change import compile_change
from richbuild.models import ProjectSpec
from richbuild.planner import plan_nextjs_architecture
from richbuild.store import RichStore


def _narrow(architecture, node_id, keep):
    """Give one node responsibility for only some requirements.

    The deterministic planner allocates every requirement to every layer, which
    is honest for a layered baseline and buys no change locality at all. The
    architect is asked for the minimum allocation; here the separation is built
    by hand so the test measures the engine rather than the model's judgement.
    """

    keep = set(keep)
    contracts = []
    for contract in architecture.contracts:
        if contract.node_id != node_id:
            contracts.append(contract)
            continue
        obligations = tuple(
            item for item in contract.obligations if keep & set(item.requirement_ids)
        )
        surviving = {item.id for item in obligations}
        # Operations, obligations and invariants are narrowed in one step: a
        # contract validates on construction, so dropping obligations first
        # would leave an invariant citing something that no longer exists.
        contracts.append(
            replace(
                contract,
                operations=tuple(
                    item
                    for item in contract.operations
                    if keep & set(item.requirement_ids)
                ),
                obligations=obligations,
                invariants=tuple(
                    replace(
                        invariant,
                        obligation_ids=tuple(
                            identifier
                            for identifier in invariant.obligation_ids
                            if identifier in surviving
                        ),
                    )
                    for invariant in contract.invariants
                    if keep & set(invariant.requirement_ids)
                ),
            )
        )
    operation_ids = {item.id for contract in contracts for item in contract.operations}
    nodes = tuple(
        replace(
            node,
            requirement_ids=tuple(sorted(keep)),
            ports=tuple(
                port
                for port in node.ports
                if port.operation_id is None or port.operation_id in operation_ids
            ),
        )
        if node.id == node_id
        else node
        for node in architecture.nodes
    )
    return replace(architecture, nodes=nodes, contracts=tuple(contracts))


def _amend(spec: ProjectSpec, requirement_id: str, statement: str) -> ProjectSpec:
    """Reword one requirement and the oracle that checks it.

    Rebuilt through the dictionary form because a scenario's steps are typed
    objects, and the browser oracle asserts the statement verbatim -- so an
    amendment that left the oracle behind would be checking the old claim.
    """

    document = spec.to_dict()
    for requirement in document["requirements"]:
        if requirement["id"] == requirement_id:
            requirement["statement"] = statement
    for scenario in document["acceptance_scenarios"]:
        if requirement_id not in scenario["requirement_ids"]:
            continue
        for step in scenario["oracle"]:
            locator = step.get("locator")
            if step.get("action") == "assert_visible" and isinstance(locator, dict):
                locator["value"] = statement
    return ProjectSpec.from_dict(document)


def _require_login():
    if shutil.which("claude") is None:
        pytest.skip("live test; the `claude` CLI is not on PATH")
    if not (Path.home() / ".claude" / ".credentials.json").exists():
        pytest.skip("live test; run `claude` once to log in first")


@pytest.mark.live
def test_an_amendment_replays_the_components_it_did_not_touch(tmp_path):
    from test_closed_loop_live import _prepare, _execute  # noqa: PLC0415

    _require_login()
    state = _prepare(tmp_path, narrow=lambda architecture: _narrow(
        architecture, "domain", {"req.checklist"}
    ))
    store: RichStore = state["store"]

    first = _execute(state)
    assert first.succeeded, "the first build must land before a change means anything"

    reused_first = _reused_nodes(store, state["run"]["id"])
    assert reused_first == set(), "a first build has nothing to replay"

    # Amend a requirement the domain layer does not serve.
    before_spec = state["project"]
    after_spec = _amend(
        before_spec,
        "req.a11y",
        "The launch checklist is fully operable with a keyboard alone.",
    )
    before_arch = state["architecture"]
    after_arch = _narrow(
        plan_nextjs_architecture(after_spec).architecture, "domain", {"req.checklist"}
    )

    change = compile_change(
        before_spec=before_spec,
        after_spec=after_spec,
        before_architecture=before_arch,
        after_architecture=after_arch,
    )

    assert "req.a11y" in change.requirements.modified
    assert "domain" in change.reusable, (
        f"domain never served req.a11y; stale={change.stale}"
    )
    assert change.stale, "the amendment has to cost something"

    for node_id in change.stale:
        store.forget_generation_memos(project_id=before_spec.id, node_id=node_id)

    second_state = _prepare(
        tmp_path / "second",
        project=after_spec,
        architecture=after_arch,
        run_id="run.closed-loop-2",
    )
    # The memo store is shared: that is the whole mechanism.
    second_state["store"] = store
    second = _execute(second_state)

    reused = _reused_nodes(store, second_state["run"]["id"])
    assert second.succeeded, "the amended build must still pass every gate"
    assert "domain" in reused, (
        f"domain was rebuilt despite being untouched; replayed={sorted(reused)}"
    )
    assert not (reused & set(change.stale)), (
        f"a stale component replayed instead of being rewritten: {sorted(reused)}"
    )

    gates = _passed_gate_kinds(store, second_state["run"]["id"])
    assert gates == {"lint", "static", "unit", "property", "build", "acceptance"}, (
        f"the second pass skipped a gate: {sorted(gates)}"
    )


def _reused_nodes(store: RichStore, run_id: str) -> set[str]:
    """Which components replayed a remembered generation rather than asking."""

    reused: set[str] = set()
    for event in store.list_events(run_id):
        if event["event_type"] != "evidence.recorded":
            continue
        if event["payload"].get("kind") != "generation":
            continue
        task_id = event.get("task_id") or ""
        if "Reused" in str(event["payload"].get("summary", "")):
            reused.add(task_id.rsplit(":", 1)[-1])
    return reused


def _passed_gate_kinds(store: RichStore, run_id: str) -> set[str]:
    return {
        event["payload"]["kind"]
        for event in store.list_events(run_id)
        if event["event_type"] == "evidence.recorded"
        and event["payload"].get("status") == "passed"
        and event["payload"].get("kind")
        in {"lint", "static", "unit", "property", "build", "acceptance"}
    }
