import hashlib
import time

import pytest

from rich_v2.store import NotFoundError, RevisionConflict, RichStore, StoreError


def _project_with_revisions(store: RichStore):
    project = store.create_project("Semantic todo", project_id="project_todo")
    spec = store.save_revision(
        project["id"],
        kind="product_spec",
        schema_version="2.0",
        document={"id": "todo", "requirements": ["add", "list"]},
        expected_revision=0,
    )
    architecture = store.save_revision(
        project["id"],
        kind="architecture",
        schema_version="2.0",
        document={"id": "todo_arch", "nodes": ["ui", "domain", "db"]},
        expected_revision=1,
    )
    return project, spec, architecture


def test_project_revisions_are_optimistic_and_durable(tmp_path):
    store = RichStore(tmp_path)
    project, spec, architecture = _project_with_revisions(store)

    assert spec.number == 1
    assert architecture.number == 2
    assert RichStore(tmp_path).get_project(project["id"])["current_revision"] == 2

    with pytest.raises(RevisionConflict, match="not expected revision 1"):
        store.save_revision(
            project["id"],
            kind="product_spec",
            schema_version="2.0",
            document={"stale": True},
            expected_revision=1,
        )


def test_run_tasks_and_events_survive_reopen(tmp_path):
    store = RichStore(tmp_path)
    project, spec, architecture = _project_with_revisions(store)
    run = store.create_run(
        project["id"],
        spec_revision_id=spec.id,
        architecture_revision_id=architecture.id,
        budget={"max_model_calls": 10},
    )
    dependency = store.create_task(
        run["id"], node_id="schema", kind="implement"
    )
    task = store.create_task(
        run["id"],
        node_id="domain",
        kind="implement",
        dependency_task_ids=(dependency["id"],),
    )
    task = store.set_task_status(
        task["id"], "running", expected_status="ready", increment_attempt=True
    )
    first = store.append_event(run["id"], "task.started", {"attempt": 1}, task_id=task["id"])
    second = store.append_event(run["id"], "task.succeeded", {}, task_id=task["id"])

    reopened = RichStore(tmp_path)
    assert reopened.get_task(task["id"])["attempt"] == 1
    assert reopened.get_task(task["id"])["dependency_task_ids"] == (
        dependency["id"],
    )
    assert reopened.list_events(run["id"], after_sequence=first["sequence"]) == [second]


def test_artifacts_are_content_addressed_and_deduplicated(tmp_path):
    store = RichStore(tmp_path)
    content = b"verified source archive"

    first = store.put_artifact(content, media_type="application/tar+gzip")
    second = store.put_artifact(content, media_type="application/tar+gzip")

    assert first.digest == hashlib.sha256(content).hexdigest()
    assert first.digest == second.digest
    assert first.path.read_bytes() == content
    assert len(list((tmp_path / "artifacts" / "sha256").rglob(first.digest[2:]))) == 1


def test_run_execution_lease_is_fenced_and_recoverable_after_expiry(tmp_path):
    store = RichStore(tmp_path)
    project = store.create_project("Lease", project_id="project.lease")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )

    lease = store.claim_run_execution(run["id"], lease_seconds=0.02)
    assert store.is_run_execution_owner(run["id"], lease.owner_token)
    with pytest.raises(StoreError, match="active execution owner"):
        store.claim_run_execution(run["id"])

    renewed = store.renew_run_execution(
        run["id"], owner_token=lease.owner_token, lease_seconds=0.02
    )
    assert renewed.owner_token == lease.owner_token
    assert not store.release_run_execution(run["id"], owner_token="stale")
    time.sleep(0.03)
    successor = store.claim_run_execution(run["id"])
    assert successor.owner_token != lease.owner_token
    assert not store.release_run_execution(
        run["id"], owner_token=lease.owner_token
    )
    assert store.release_run_execution(
        run["id"], owner_token=successor.owner_token
    )


def test_successor_fences_every_authoritative_run_mutation(tmp_path):
    store = RichStore(tmp_path)
    project = store.create_project("Fenced execution")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )
    task = store.create_task(
        run["id"],
        node_id="application",
        kind="implement",
    )
    first_source = store.put_artifact(b"first owner source")

    first = store.claim_run_execution(run["id"], lease_seconds=0.02)
    store.set_run_status(
        run["id"],
        "running",
        expected_status="ready",
        owner_token=first.owner_token,
    )
    store.set_task_status(
        task["id"],
        "running",
        expected_status="ready",
        increment_attempt=True,
        owner_token=first.owner_token,
    )
    store.append_event(
        run["id"],
        "owner_a.started",
        task_id=task["id"],
        owner_token=first.owner_token,
    )
    store.attach_artifact(
        run["id"],
        first_source.digest,
        role="source",
        task_id=task["id"],
        owner_token=first.owner_token,
    )

    time.sleep(0.03)
    successor = store.claim_run_execution(run["id"])
    stale_source = store.put_artifact(b"stale owner source")
    stale_evidence = store.put_artifact(
        b'{"status":"passed"}',
        media_type="application/vnd.rich.evidence+json",
    )

    stale_mutations = (
        lambda: store.set_task_status(
            task["id"],
            "verifying",
            expected_status="running",
            owner_token=first.owner_token,
        ),
        lambda: store.set_task_status(
            task["id"],
            "canceled",
            expected_status="running",
            owner_token=first.owner_token,
        ),
        lambda: store.set_run_status(
            run["id"],
            "failed",
            expected_status="running",
            owner_token=first.owner_token,
        ),
        lambda: store.set_run_status(
            run["id"],
            "canceled",
            expected_status="running",
            owner_token=first.owner_token,
        ),
        lambda: store.append_event(
            run["id"],
            "owner_a.stale_event",
            task_id=task["id"],
            owner_token=first.owner_token,
        ),
        lambda: store.attach_artifact(
            run["id"],
            stale_source.digest,
            role="source:stale",
            task_id=task["id"],
            owner_token=first.owner_token,
        ),
        lambda: store.attach_artifact(
            run["id"],
            stale_evidence.digest,
            role="evidence:unit",
            task_id=task["id"],
            owner_token=first.owner_token,
        ),
        # Even an idempotent-looking reattachment must validate authority
        # before it returns successfully.
        lambda: store.attach_artifact(
            run["id"],
            first_source.digest,
            role="source",
            task_id=task["id"],
            owner_token=first.owner_token,
        ),
    )
    for mutation in stale_mutations:
        with pytest.raises(RevisionConflict, match="ownership was lost"):
            mutation()

    assert store.get_run(run["id"])["status"] == "running"
    assert store.get_task(task["id"])["status"] == "running"
    assert {
        attachment["digest"]
        for attachment in store.list_run_artifacts(run["id"])
    } == {first_source.digest}
    assert {
        event["event_type"] for event in store.list_events(run["id"])
    } == {"owner_a.started"}

    store.append_event(
        run["id"],
        "owner_b.took_over",
        owner_token=successor.owner_token,
    )
    store.set_task_status(
        task["id"],
        "verifying",
        expected_status="running",
        owner_token=successor.owner_token,
    )
    store.set_task_status(
        task["id"],
        "succeeded",
        expected_status="verifying",
        owner_token=successor.owner_token,
    )
    store.set_run_status(
        run["id"],
        "verifying",
        expected_status="running",
        owner_token=successor.owner_token,
    )
    store.set_run_status(
        run["id"],
        "succeeded",
        expected_status="verifying",
        owner_token=successor.owner_token,
    )

    assert store.get_run(run["id"])["status"] == "succeeded"
    assert store.get_task(task["id"])["status"] == "succeeded"


def test_artifact_reads_and_deduplication_fail_closed_after_cas_tampering(tmp_path):
    store = RichStore(tmp_path)
    artifact = store.put_artifact(b"good", media_type="text/plain")
    artifact.path.write_bytes(b"evil")

    with pytest.raises(StoreError, match="immutable content verification"):
        store.get_artifact(artifact.digest)
    with pytest.raises(StoreError, match="immutable content verification"):
        store.put_artifact(b"good", media_type="text/plain")


def test_approvals_are_single_decision_records(tmp_path):
    store = RichStore(tmp_path)
    project = store.create_project("Approval")
    approval = store.request_approval(
        project["id"], gate="architecture", request={"dependencies": ["next"]}
    )

    assert approval["status"] == "requested"
    decided = store.decide_approval(
        approval["id"], approved=True, decision={"actor": "founder"}
    )

    assert decided["status"] == "approved"
    assert decided["decision"] == {"actor": "founder"}
    with pytest.raises(RevisionConflict, match="already been decided"):
        store.decide_approval(approval["id"], approved=False)


def test_state_store_rejects_unknown_and_impossible_transitions(tmp_path):
    store = RichStore(tmp_path)
    project = store.create_project("State machine")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )
    task = store.create_task(run["id"], node_id="web", kind="implement")

    with pytest.raises(RevisionConflict, match="invalid run"):
        store.set_run_status(run["id"], "succeeded", expected_status="ready")
    with pytest.raises(RevisionConflict, match="invalid task"):
        store.set_task_status(
            task["id"], "succeeded", expected_status="ready"
        )
    with pytest.raises(ValueError, match="unknown run"):
        store.create_run(
            project["id"],
            spec_revision_id=None,
            architecture_revision_id=None,
            status="invented",
        )


def test_same_artifact_can_be_attached_to_multiple_tasks(tmp_path):
    store = RichStore(tmp_path)
    project = store.create_project("Artifacts")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
    )
    first = store.create_task(run["id"], node_id="first", kind="implement")
    second = store.create_task(run["id"], node_id="second", kind="implement")
    artifact = store.put_artifact(b"shared")

    store.attach_artifact(
        run["id"], artifact.digest, role="source", task_id=first["id"]
    )
    store.attach_artifact(
        run["id"], artifact.digest, role="source", task_id=second["id"]
    )
    store.attach_artifact(
        run["id"], artifact.digest, role="source", task_id=second["id"]
    )

    attachments = store.list_run_artifacts(run["id"])
    assert {item["task_id"] for item in attachments} == {
        first["id"],
        second["id"],
    }


def test_missing_objects_fail_loudly(tmp_path):
    store = RichStore(tmp_path)

    with pytest.raises(NotFoundError):
        store.get_project("project_missing")


def test_idempotency_claim_replays_response_and_rejects_key_reuse(tmp_path):
    store = RichStore(tmp_path)
    request = {"project_id": "project.demo", "name": "Demo"}

    lease = store.claim_idempotency(
        "create-demo", operation="project.create", request=request
    )
    store.complete_idempotency(
        "create-demo",
        owner_token=lease.owner_token,
        status_code=201,
        response={"id": "project.demo"},
    )

    replay = store.claim_idempotency(
        "create-demo", operation="project.create", request=request
    )
    assert replay.status_code == 201
    assert replay.response == {"id": "project.demo"}
    with pytest.raises(RevisionConflict, match="different request"):
        store.claim_idempotency(
            "create-demo",
            operation="project.create",
            request={"project_id": "project.other", "name": "Other"},
        )


def test_failed_idempotent_request_can_abandon_claim(tmp_path):
    store = RichStore(tmp_path)
    request = {"value": 1}
    lease = store.claim_idempotency(
        "retryable", operation="demo", request=request
    )

    with pytest.raises(Exception, match="in progress"):
        store.claim_idempotency("retryable", operation="demo", request=request)

    store.abandon_idempotency(
        "retryable", owner_token=lease.owner_token
    )
    reclaimed = store.claim_idempotency(
        "retryable", operation="demo", request=request
    )
    assert reclaimed.owner_token != lease.owner_token


def test_expired_idempotency_lease_is_recoverable_and_fences_old_owner(tmp_path):
    store = RichStore(tmp_path)
    request = {"value": 1}
    abandoned = store.claim_idempotency(
        "crashed",
        operation="demo",
        request=request,
        lease_seconds=0.001,
    )
    time.sleep(0.01)

    recovered = store.claim_idempotency(
        "crashed", operation="demo", request=request
    )

    assert recovered.owner_token != abandoned.owner_token
    with pytest.raises(StoreError, match="not an active claim"):
        store.complete_idempotency(
            "crashed",
            owner_token=abandoned.owner_token,
            status_code=200,
            response={"stale": True},
        )


def test_an_approval_cannot_be_opened_at_a_gate_nothing_checks(tmp_path):
    """The gate names the authority. A misspelled one would record a decision
    that looks granted and authorizes nothing."""

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.gates")

    approval = store.request_approval(
        project["id"], gate="architecture", request={"revision_id": "rev.1"}
    )
    assert approval["gate"] == "architecture"

    for unknown in ("architecure", "ARCHITECTURE", "", "spec"):
        with pytest.raises(ValueError, match="unknown approval gate"):
            store.request_approval(
                project["id"], gate=unknown, request={"revision_id": "rev.1"}
            )
