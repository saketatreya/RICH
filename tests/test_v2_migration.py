import pytest

from rich_v2.migration import MigrationError, import_v1_canvas, inspect_v1_canvas
from rich_v2.store import RichStore


def _legacy_document():
    return {
        "version": 1,
        "tree": {
            "id": "todo_app",
            "description": "A collaborative todo application",
            "status": "verified",
            "operations": [
                {
                    "name": "run",
                    "inputs": {"text": "string"},
                    "outputs": {"result": "dict"},
                    "errors": [],
                }
            ],
            "children": [
                {
                    "id": "domain",
                    "description": "Manage todos",
                    "status": "verified",
                    "operations": [
                        {
                            "name": "add",
                            "inputs": {"title": "string"},
                            "outputs": {"todo": "dict"},
                            "errors": [],
                        }
                    ],
                    "children": [],
                    "edges": [],
                }
            ],
            "edges": [],
        },
    }


def test_v1_verified_status_is_not_promoted_to_v2_evidence():
    draft = inspect_v1_canvas(_legacy_document())

    assert not draft.ready_for_spec_approval
    assert draft.nodes[0]["legacy_status"] == "verified"
    assert any(
        issue.code == "legacy_verification_not_trusted"
        for issue in draft.issues
    )
    assert "status" not in draft.to_dict()


def test_import_persists_source_artifact_and_review_draft(tmp_path):
    store = RichStore(tmp_path)

    imported = import_v1_canvas(store, _legacy_document())

    assert imported.revision.kind == "v1_import_draft"
    assert imported.revision.document["source_artifact_digest"] == imported.source_artifact.digest
    assert imported.source_artifact.path.read_bytes()
    assert imported.project["current_revision"] == 1


def test_repeated_import_uses_a_distinct_project_id(tmp_path):
    store = RichStore(tmp_path)

    first = import_v1_canvas(store, _legacy_document())
    second = import_v1_canvas(store, _legacy_document())

    assert first.project["id"] == "project.todo_app"
    assert second.project["id"] == "project.todo_app.2"


def test_duplicate_legacy_node_ids_fail_instead_of_merging(tmp_path):
    document = _legacy_document()
    document["tree"]["children"].append(document["tree"]["children"][0].copy())

    with pytest.raises(MigrationError, match="duplicate"):
        inspect_v1_canvas(document)
