"""canvas.py — HTTP backend for the RICH canvas (engine as a JSON API + static app).

A stdlib HTTP server that drives the REAL engine and serves the built React app
(`web/dist`). The frontend lives in `web/` (React + Vite + React Flow).

  • Plan layer  → skills.plan(contract, allow_decompose=True) on ONE node (1 LLM call).
  • Vibe ✨     → recursively PLAN a subtree down to MAX_DEPTH (the "auto-plan" button).
  • Vibe-edit   → an architecture transaction (previewable) on the canvas tree.
  • Build ▶     → persist the planned tree's decisions so build() reuses them
                  (the resumption / decision-reuse seam), then assemble() + run main.py.
                  Optional `node_id` = scoped single-module rebuild (invalidate_node).
  • /api/node   → a built module's generated src + tests + test report (the build loop).

The backend is selected by RICH_BACKEND (`claude` by default, `codex` supported).
Long operations run in a single background job (one at a time — the engine writes to
build/ and is not reentrant, and this caps quota burn). The frontend polls /api/job
for streamed stdout + the result, and /api/statuses for live per-module status.

Run:   npm --prefix web run build   (once / after frontend changes)
       python canvas.py             # serve at http://localhost:8765
Dev:   npm --prefix web run dev     # Vite hot reload, proxies /api → :8765
Env:   RICH_CANVAS_PORT (default 8765)
"""

import json
import copy
import os
import re
import sys
import threading
import time
import traceback
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Route the three skills through the selected backend ────────────
import backend
BACKEND_NAME = backend.install_from_env()

import skills  # noqa: E402
import build as buildmod  # noqa: E402
from build import (  # noqa: E402
    build,
    assemble,
    BuildFailure,
    MAX_DEPTH,
    _contract_hash,
)
from node import (  # noqa: E402
    BUILD_ROOT,
    Node,
    save_contract,
    save_decision,
    save_status,
)

HERE = Path(__file__).resolve().parent
WEB_DIST = HERE / "web" / "dist"
PORT = int(os.environ.get("RICH_CANVAS_PORT", "8765"))
PROJECT_PATH = Path(os.environ.get("RICH_CANVAS_PROJECT", HERE / "rich.canvas.json"))
V2_STATE_DIR = Path(
    os.environ.get("RICH_V2_STATE_DIR", HERE / ".rich" / "state")
)
PROJECT_VERSION = 1
MODULE_KINDS = {"pure", "stateful", "adapter", "ui"}

# Keep the repository runnable without an editable install while v2 lives in src/.
_SRC_ROOT = HERE / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from rich_v2.api import MAX_BODY_BYTES, V2Application  # noqa: E402
from rich_v2.runtime import default_architect  # noqa: E402
from rich_v2.store import RichStore  # noqa: E402

_v2_application: V2Application | None = None
_v2_application_lock = threading.Lock()


def _get_v2_application() -> V2Application:
    global _v2_application
    if _v2_application is None:
        with _v2_application_lock:
            if _v2_application is None:
                # A model-backed architect when one can be built, and a
                # deterministic planner when it cannot. default_architect
                # returns None rather than raising precisely so a Canvas with
                # no Claude Code login still starts and still plans; the draft
                # response says which one answered.
                _v2_application = V2Application(
                    RichStore(V2_STATE_DIR), architect=default_architect()
                )
    return _v2_application


# ── Contract <-> canvas-node conversion ────────────────────────────
# A canvas node is the JSON the frontend owns:
#   {id, description, operations[], dependencies[], stateful,
#    kind, external, is_leaf: null|true|false, children[], edges[], status}
# An engine contract is what skills.plan / build expect.

def module_kind(cnode: dict) -> str:
    kind = cnode.get("kind") or cnode.get("module_kind")
    if kind not in MODULE_KINDS:
        kind = "stateful" if cnode.get("stateful") else "pure"
    return kind


def default_external(provider: str = "") -> dict:
    return {
        "provider": provider,
        "mock_policy": "unit_tests_mock_provider",
        "live_smoke": False,
    }


def normalize_canvas_node(cnode: dict) -> dict:
    cnode.setdefault("description", "")
    cnode.setdefault("operations", [])
    cnode.setdefault("dependencies", [])
    cnode.setdefault("children", [])
    cnode.setdefault("edges", [])
    cnode.setdefault("status", "unplanned")
    cnode.setdefault("lane", "")
    cnode["kind"] = module_kind(cnode)
    if cnode["kind"] == "adapter" and cnode.get("children"):
        cnode["kind"] = "pure"
        cnode["external"] = {}
    cnode["stateful"] = bool(cnode.get("stateful") or cnode["kind"] == "stateful")
    if cnode["kind"] == "adapter":
        ext = cnode.get("external") if isinstance(cnode.get("external"), dict) else {}
        cnode["external"] = {**default_external(), **ext}
    for child in cnode.get("children", []) or []:
        normalize_canvas_node(child)
    return cnode


def node_to_contract(cnode: dict) -> dict:
    normalize_canvas_node(cnode)
    contract = {
        "id": cnode["id"],
        "description": cnode.get("description", ""),
        "interface": {"operations": cnode.get("operations", []) or []},
        "dependencies": cnode.get("dependencies", []) or [],
        "behavior": [{"id": "goal", "prose": cnode.get("description", "")}],
        "module_kind": cnode.get("kind", "pure"),
    }
    if cnode.get("kind") == "adapter":
        contract["external"] = cnode.get("external") or default_external()
        provider = contract["external"].get("provider") or "external provider"
        contract["behavior"].append({
            "id": "external_boundary",
            "prose": (
                f"This module is an external adapter for {provider}. Unit tests must "
                "mock or fake provider I/O; live network/API calls belong only in an "
                "explicit opt-in smoke test."
            ),
        })
    if cnode.get("stateful"):
        contract["stateful"] = True
    return contract


def contract_to_node(cc: dict) -> dict:
    """An engine child contract (from a PLAN decision) → a fresh canvas node."""
    iface = cc.get("interface", {}) or {}
    node = {
        "id": cc["id"],
        "description": cc.get("description", ""),
        "operations": iface.get("operations", []) or [],
        "dependencies": cc.get("dependencies", []) or [],
        "kind": cc.get("module_kind") or ("stateful" if cc.get("stateful") else "pure"),
        "external": cc.get("external") or {},
        "stateful": bool(cc.get("stateful", False)),
        "is_leaf": None,        # not yet planned
        "children": [],
        "edges": [],
        "status": "unplanned",
    }
    return normalize_canvas_node(node)


def find_node(tree: dict, node_id: str) -> dict | None:
    if tree.get("id") == node_id:
        return tree
    for c in tree.get("children", []) or []:
        hit = find_node(c, node_id)
        if hit is not None:
            return hit
    return None


def flatten_tree(tree: dict | None) -> list[dict]:
    if not tree:
        return []
    out = []
    def walk(n: dict):
        out.append(n)
        for child in n.get("children", []) or []:
            walk(child)
    walk(tree)
    return out


def parent_of(tree: dict, node_id: str) -> dict | None:
    found = None
    def walk(n: dict):
        nonlocal found
        for child in n.get("children", []) or []:
            if child.get("id") == node_id:
                found = n
                return
            walk(child)
    walk(tree)
    return found


def sanitize_module_id(raw: str | None, fallback: str = "module") -> str:
    text = (raw or fallback).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text or not re.match(r"^[a-z]", text):
        text = fallback
    return text[:48].rstrip("_") or fallback


def unique_module_id(tree: dict, raw: str | None, exclude: set[str] | None = None) -> str:
    exclude = exclude or set()
    existing = {n.get("id") for n in flatten_tree(tree)} - exclude
    base = sanitize_module_id(raw)
    candidate = base
    i = 2
    while candidate in existing:
        candidate = f"{base}_{i}"
        i += 1
    return candidate


def dependency_id(dep) -> str | None:
    if isinstance(dep, dict):
        return dep.get("id") or dep.get("name")
    if isinstance(dep, str):
        return dep
    return None


def rewrite_node_references(tree: dict, old_id: str, new_id: str | None = None) -> None:
    """Rename or remove every graph reference to a module id."""
    for n in flatten_tree(tree):
        edges = []
        for edge in n.get("edges", []) or []:
            if edge.get("from") == old_id or edge.get("to") == old_id:
                if new_id is None:
                    continue
                edge = {**edge}
                if edge.get("from") == old_id:
                    edge["from"] = new_id
                if edge.get("to") == old_id:
                    edge["to"] = new_id
            edges.append(edge)
        n["edges"] = edges

        deps = []
        for dep in n.get("dependencies", []) or []:
            dep_id = dependency_id(dep)
            if dep_id == old_id:
                if new_id is None:
                    continue
                if isinstance(dep, dict):
                    dep = {**dep, "id": new_id}
                    if dep.get("name") == old_id:
                        dep["name"] = new_id
                else:
                    dep = new_id
            deps.append(dep)
        n["dependencies"] = deps


def detach_node(tree: dict, node_id: str) -> dict | None:
    parent = parent_of(tree, node_id)
    if not parent:
        return None
    for i, child in enumerate(parent.get("children", []) or []):
        if child.get("id") == node_id:
            removed = parent["children"].pop(i)
            if not parent["children"]:
                parent["is_leaf"] = None
                parent["edges"] = []
            return removed
    return None


_EXTERNAL_STRONG_RE = re.compile(
    r"\b(http|https|api|external|network|request|database|filesystem|file|shell|open-meteo|url)\b",
    re.I,
)
_EXTERNAL_ROLE_RE = re.compile(
    r"\b(client|gateway|adapter|repository|dao)\b",
    re.I,
)


def infer_provider(cnode: dict) -> str:
    text = f"{cnode.get('id', '')} {cnode.get('description', '')}".lower()
    if "weather" in text or "open-meteo" in text:
        return "Open-Meteo"
    if "http" in text or "api" in text:
        return "HTTP API"
    if "database" in text:
        return "database"
    if "file" in text:
        return "filesystem"
    return "external provider"


def looks_external(cnode: dict) -> bool:
    text = f"{cnode.get('id', '')} {cnode.get('description', '')}"
    if _EXTERNAL_STRONG_RE.search(text):
        return True
    node_id = cnode.get("id", "")
    return bool(_EXTERNAL_ROLE_RE.search(node_id))


def project_document(tree: dict | None, history: list | None = None) -> dict:
    return {
        "version": PROJECT_VERSION,
        "saved_at": round(time.time(), 3),
        "tree": normalize_canvas_node(copy.deepcopy(tree)) if tree else None,
        "history": history or [],
    }


def load_project() -> dict:
    if not PROJECT_PATH.exists():
        return project_document(None)
    try:
        data = json.loads(PROJECT_PATH.read_text())
        if data.get("tree"):
            normalize_canvas_node(data["tree"])
        data.setdefault("version", PROJECT_VERSION)
        data.setdefault("history", [])
        return data
    except Exception as e:
        return {"version": PROJECT_VERSION, "tree": None, "history": [],
                "error": f"could not read {PROJECT_PATH}: {e}"}


def save_project(tree: dict | None, history: list | None = None) -> dict:
    doc = project_document(tree, history)
    PROJECT_PATH.write_text(json.dumps(doc, indent=2))
    return doc


def validation_cards(tree: dict | None) -> list[dict]:
    if not tree:
        return []
    normalize_canvas_node(tree)
    cards: list[dict] = []
    nodes = flatten_tree(tree)
    ids: dict[str, int] = {}
    for n in nodes:
        ids[n.get("id", "")] = ids.get(n.get("id", ""), 0) + 1

    def card(severity: str, node: dict | None, title: str, message: str,
             action: str | None = None):
        cards.append({
            "severity": severity,
            "node_id": node.get("id") if node else None,
            "title": title,
            "message": message,
            "action": action,
        })

    for n in nodes:
        node_id = n.get("id", "")
        if not re.match(r"^[a-z][a-z0-9_]*$", node_id or ""):
            card("error", n, "Invalid module id",
                 "Module ids must be lowercase snake_case and start with a letter.")
        if ids.get(node_id, 0) > 1:
            card("error", n, "Duplicate module id",
                 f"More than one module is named {node_id!r}.")
        if n.get("is_leaf") is None:
            card("warning", n, "Needs disposition",
                 "Mark this module as a leaf or decompose it before building.")
        if not n.get("operations"):
            card("error", n, "Missing operation",
                 "Every module needs at least one operation in its interface.")
        can_be_adapter = n.get("is_leaf") is not False and not n.get("children")
        if looks_external(n) and module_kind(n) != "adapter" and can_be_adapter:
            card("warning", n, "Likely external adapter",
                 "This module sounds like it talks to an external service. Mark it as an adapter so unit tests use fakes instead of live I/O.",
                 "mark_adapter")
        if module_kind(n) == "adapter":
            ext = n.get("external") or {}
            if not ext.get("provider"):
                card("warning", n, "Adapter provider missing",
                     "Name the provider so RICH can generate clearer fakes and smoke-test boundaries.",
                     "mark_adapter")

        child_ids = {c.get("id") for c in n.get("children", []) or []}
        for e in n.get("edges", []) or []:
            if e.get("from") not in child_ids or e.get("to") not in child_ids:
                card("error", n, "Invalid dataflow edge",
                     f"Edge {e.get('from')} -> {e.get('to')} must connect children of this module.")
            if e.get("from") == e.get("to"):
                card("error", n, "Self edge",
                     "A dataflow edge cannot connect a module to itself.")
        for child in n.get("children", []) or []:
            dep_ids = {d.get("id") if isinstance(d, dict) else d
                       for d in child.get("dependencies", []) or []}
            for dep_id in dep_ids:
                if dep_id not in child_ids:
                    card("error", child, "Invalid held capability",
                         f"Held dependency {dep_id!r} must be a sibling module.")

        # Cycle check per sibling dataflow graph.
        graph = {cid: set() for cid in child_ids}
        for e in n.get("edges", []) or []:
            if e.get("from") in child_ids and e.get("to") in child_ids:
                graph[e["from"]].add(e["to"])
        visiting, visited = set(), set()
        def visit(cid: str) -> bool:
            if cid in visiting:
                return True
            if cid in visited:
                return False
            visiting.add(cid)
            for nxt in graph.get(cid, set()):
                if visit(nxt):
                    return True
            visiting.remove(cid)
            visited.add(cid)
            return False
        if any(visit(cid) for cid in list(graph)):
            card("error", n, "Dataflow cycle",
                 "Child dataflow edges must form a DAG.")
    return cards


def apply_transaction(tree: dict, transaction: dict) -> dict:
    """Apply a constrained, auditable canvas transaction in place."""
    for op in transaction.get("ops", []) or []:
        if isinstance(op, dict) and op.get("op") == "replace_tree" and isinstance(op.get("tree"), dict):
            return normalize_canvas_node(copy.deepcopy(op["tree"]))
    normalize_canvas_node(tree)
    for op in transaction.get("ops", []) or []:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        node = find_node(tree, op.get("node_id", ""))

        if kind == "create_child":
            parent = find_node(tree, op.get("parent_id") or op.get("node_id", ""))
            if not parent:
                continue
            spec = op.get("node") or op.get("child") or {}
            if not isinstance(spec, dict):
                spec = {}
            child_id = unique_module_id(tree, spec.get("id") or op.get("child_id") or "module")
            child = normalize_canvas_node({
                "id": child_id,
                "description": spec.get("description") or op.get("description") or "",
                "operations": spec.get("operations") or op.get("operations") or [],
                "dependencies": spec.get("dependencies") or [],
                "kind": spec.get("kind") or spec.get("module_kind") or op.get("kind") or "pure",
                "external": spec.get("external") or {},
                "stateful": bool(spec.get("stateful", False)),
                "is_leaf": spec.get("is_leaf", True),
                "children": spec.get("children") or [],
                "edges": spec.get("edges") or [],
                "status": spec.get("status") or "planned",
            })
            parent.setdefault("children", []).append(child)
            parent["is_leaf"] = False
            parent.setdefault("edges", [])
            parent["status"] = "planned"

        elif kind == "delete_node":
            target_id = op.get("node_id", "")
            if target_id and target_id != tree.get("id"):
                removed = find_node(tree, target_id)
                if removed:
                    removed_ids = [n["id"] for n in flatten_tree(removed)]
                    detach_node(tree, target_id)
                    for removed_id in removed_ids:
                        rewrite_node_references(tree, removed_id, None)

        elif kind == "rename_node":
            old_id = op.get("node_id") or op.get("old_id")
            target = find_node(tree, old_id or "")
            if not target:
                continue
            new_id = unique_module_id(tree, op.get("new_id") or op.get("id"), exclude={old_id})
            if new_id and new_id != old_id:
                target["id"] = new_id
                rewrite_node_references(tree, old_id, new_id)

        elif kind == "mark_adapter" and node:
            node["kind"] = "adapter"
            node["stateful"] = False
            node["external"] = {**default_external(infer_provider(node)),
                                **(node.get("external") or {}),
                                **(op.get("external") or {})}
            node["status"] = "planned" if node.get("is_leaf") is not None else node.get("status", "unplanned")

        elif kind == "set_kind" and node:
            new_kind = op.get("kind") or op.get("module_kind")
            if new_kind in MODULE_KINDS:
                node["kind"] = new_kind
                node["stateful"] = new_kind == "stateful"
                if new_kind == "adapter":
                    node["external"] = {**default_external(infer_provider(node)),
                                        **(node.get("external") or {}),
                                        **(op.get("external") or {})}
                normalize_canvas_node(node)

        elif kind == "update_contract" and node:
            for key in ("description", "operations", "dependencies", "stateful", "kind", "external"):
                if key in op:
                    node[key] = op[key]
            normalize_canvas_node(node)

        elif kind == "set_leaf" and node:
            node["is_leaf"] = bool(op.get("is_leaf", True))
            if node["is_leaf"]:
                removed_ids = [n["id"] for child in node.get("children", []) or []
                               for n in flatten_tree(child)]
                node["children"] = []
                node["edges"] = []
                for removed_id in removed_ids:
                    rewrite_node_references(tree, removed_id, None)
            node["status"] = "planned"

        elif kind == "add_edge":
            parent = find_node(tree, op.get("parent_id") or op.get("node_id", ""))
            if not parent:
                continue
            child_ids = {c.get("id") for c in parent.get("children", []) or []}
            from_id = op.get("from") or op.get("from_id")
            to_id = op.get("to") or op.get("to_id")
            if from_id in child_ids and to_id in child_ids and from_id != to_id:
                edge = {"from": from_id, "to": to_id, "name": op.get("name") or "value"}
                parent.setdefault("edges", [])
                if edge not in parent["edges"]:
                    parent["edges"].append(edge)

        elif kind == "remove_edge":
            parent = find_node(tree, op.get("parent_id") or op.get("node_id", ""))
            if not parent:
                continue
            from_id = op.get("from") or op.get("from_id")
            to_id = op.get("to") or op.get("to_id")
            name = op.get("name")
            parent["edges"] = [
                e for e in parent.get("edges", []) or []
                if not (e.get("from") == from_id and e.get("to") == to_id
                        and (name is None or e.get("name") == name))
            ]

        elif kind == "add_dependency" and node:
            parent = parent_of(tree, node.get("id"))
            dep_id = op.get("dep_id") or op.get("dependency_id") or dependency_id(op.get("dependency"))
            sibling_ids = {c.get("id") for c in parent.get("children", [])} if parent else set()
            if dep_id in sibling_ids and dep_id != node.get("id"):
                dep = {"name": op.get("name") or dep_id, "id": dep_id}
                node.setdefault("dependencies", [])
                if not any(dependency_id(d) == dep_id for d in node["dependencies"]):
                    node["dependencies"].append(dep)

        elif kind == "remove_dependency" and node:
            dep_id = op.get("dep_id") or op.get("dependency_id") or dependency_id(op.get("dependency"))
            node["dependencies"] = [
                d for d in node.get("dependencies", []) or []
                if dependency_id(d) != dep_id
            ]

    normalize_canvas_node(tree)
    return tree


def vibe_transaction(tree: dict, prompt: str, node_id: str | None = None) -> dict:
    normalize_canvas_node(tree)
    prompt_l = (prompt or "").lower()
    candidates = [find_node(tree, node_id)] if node_id else flatten_tree(tree)
    candidates = [c for c in candidates if c]
    if not candidates:
        candidates = flatten_tree(tree)

    ops = []
    summaries = []
    target = candidates[0] if candidates else None

    wants_leaf = any(tok in prompt_l for tok in ("make leaf", "mark leaf", "as a leaf", "no children"))
    if wants_leaf and target:
        ops.append({"op": "set_leaf", "node_id": target["id"], "is_leaf": True})
        summaries.append(f"marked {target['id']} as a leaf")

    wants_ui = any(tok in prompt_l for tok in (" ui", "user interface", "interface layer", "front end", "frontend"))
    if wants_ui and target:
        ops.append({"op": "set_kind", "node_id": target["id"], "kind": "ui"})
        summaries.append(f"marked {target['id']} as the UI boundary")

    wants_cache = any(tok in prompt_l for tok in ("cache", "caching", "memo", "memoize", "remember"))
    if wants_cache and target:
        parent = parent_of(tree, target["id"])
        parent_id = parent["id"] if parent and target.get("is_leaf") is True else target["id"]
        cache_id = unique_module_id(tree, f"{target['id']}_cache")
        ops.append({
            "op": "create_child",
            "parent_id": parent_id,
            "node": {
                "id": cache_id,
                "description": f"Cache provider results and computed values for {target['id']}.",
                "kind": "stateful",
                "stateful": True,
                "is_leaf": True,
                "operations": [
                    {
                        "name": "lookup",
                        "inputs": {"key": "string"},
                        "outputs": {"hit": "boolean", "value": "dict"},
                        "errors": [],
                    },
                    {
                        "name": "remember",
                        "inputs": {"key": "string", "value": "dict"},
                        "outputs": {"ok": "boolean"},
                        "errors": [],
                    },
                ],
            },
        })
        if parent and target.get("is_leaf") is True:
            ops.append({"op": "add_dependency", "node_id": target["id"],
                        "dep_id": cache_id, "name": "cache"})
        summaries.append(f"added {cache_id} as a stateful cache boundary")

    wants_external = any(tok in prompt_l for tok in (
        "external", "adapter", "api", "http", "network", "weather", "testable",
        "mock", "provider", "production"
    ))
    if wants_external:
        for n in flatten_tree(tree):
            can_be_adapter = n.get("is_leaf") is not False and not n.get("children")
            if can_be_adapter and looks_external(n) and module_kind(n) != "adapter":
                provider = infer_provider(n)
                ops.append({"op": "mark_adapter", "node_id": n["id"],
                            "external": default_external(provider)})
                summaries.append(f"marked {n['id']} as {provider} adapter")
    if not ops:
        if target and target.get("is_leaf") is not False and not target.get("children"):
            provider = infer_provider(target)
            ops.append({"op": "mark_adapter", "node_id": target["id"],
                        "external": default_external(provider)})
            summaries.append(f"marked {target['id']} as an adapter boundary")

    return {
        "label": prompt.strip() or "vibe architecture",
        "summary": "; ".join(summaries) if summaries else "no architecture change",
        "source": "deterministic",
        "ops": ops,
    }


VIBE_TX_SYSTEM = """You are a RICH canvas architecture editor.
Return ONLY a JSON object with:
  label: short title
  summary: one sentence
  ops: a list of architecture operations

Allowed ops:
- create_child: {op,parent_id,node:{id,description,kind,operations,dependencies,is_leaf,stateful,external}}
- delete_node: {op,node_id}
- rename_node: {op,node_id,new_id}
- mark_adapter: {op,node_id,external:{provider,mock_policy,live_smoke}}
- set_kind: {op,node_id,kind}
- update_contract: {op,node_id,description,operations,dependencies,stateful,kind,external}
- set_leaf: {op,node_id,is_leaf}
- add_edge/remove_edge: {op,parent_id,from,to,name}
- add_dependency/remove_dependency: {op,node_id,dep_id,name}

Edit architecture only. Do not write code. Prefer small, auditable changes.
Module ids must be lowercase snake_case. Every new module needs at least one operation."""


def llm_vibe_transaction(tree: dict, prompt: str, node_id: str | None = None) -> dict | None:
    if os.environ.get("RICH_CANVAS_LLM_VIBE", "").lower() not in {"1", "true", "yes", "on"}:
        return None
    if not skills.is_available():
        return None
    try:
        context = {
            "selected_node_id": node_id,
            "tree": tree,
        }
        raw = skills.call_with_retry(
            system_prompt=VIBE_TX_SYSTEM,
            user_prompt=(
                "Current canvas JSON:\n"
                f"{json.dumps(context, indent=2)[:18000]}\n\n"
                f"User architecture prompt: {prompt}"
            ),
            temperature=0.1,
            max_tokens=3072,
        )
        tx = skills.parse_json_response(raw, context="VIBE_TRANSACTION")
        if not isinstance(tx, dict) or not isinstance(tx.get("ops"), list) or not tx["ops"]:
            return None
        tx.setdefault("label", prompt.strip() or "vibe architecture")
        tx.setdefault("summary", "architecture transaction applied")
        tx["source"] = "llm"
        return tx
    except Exception as e:
        print(f"  [vibe-edit] LLM transaction failed, using deterministic fallback: {e}")
        return None


def transaction_for_prompt(tree: dict, prompt: str, node_id: str | None = None) -> dict:
    return llm_vibe_transaction(tree, prompt, node_id) or vibe_transaction(tree, prompt, node_id)


def _brief_text_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_brief_text_value(v) for v in value if _brief_text_value(v))
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_brief_text_value(v)}" for k, v in value.items()
                         if _brief_text_value(v))
    return str(value).strip()


def _brief_enum(value: str, allowed: set[str], default: str) -> str:
    value = _brief_text_value(value).lower()
    return value if value in allowed else default


def _brief_seed(brief: dict, current_tree: dict | None = None) -> dict:
    goal = _brief_text_value(brief.get("goal"))
    current_goal = _brief_text_value((current_tree or {}).get("description"))
    text = " ".join(filter(None, [goal, _brief_text_value(brief.get("root_id")),
                                  _brief_text_value(brief.get("external_services")),
                                  _brief_text_value(brief.get("inputs")),
                                  _brief_text_value(brief.get("outputs")),
                                  _brief_text_value(brief.get("state")),
                                  _brief_text_value(brief.get("shape")),
                                  current_goal])).lower()

    root_id = _brief_text_value(brief.get("root_id")) or _brief_text_value((current_tree or {}).get("id"))
    if not root_id:
        if "weather" in text:
            root_id = "weather_reporter"
        elif "report" in text:
            root_id = "reporter"
        else:
            root_id = "app"
    root_id = sanitize_module_id(root_id, "app")

    description = goal or current_goal or "Build a small software system."

    external = _brief_text_value(brief.get("external_services"))
    if not external:
        if "weather" in text:
            external = "Open-Meteo"
        elif "stripe" in text or "payment" in text:
            external = "Stripe"
        elif "database" in text:
            external = "database"
        elif "file" in text or "filesystem" in text:
            external = "filesystem"

    inputs = _brief_text_value(brief.get("inputs"))
    if not inputs:
        if "weather" in text:
            inputs = "city: London"
        elif "search" in text or "query" in text:
            inputs = "query: string"
        else:
            inputs = "input: string"

    outputs = _brief_text_value(brief.get("outputs"))
    if not outputs:
        if "weather" in text:
            outputs = "report: concise weather summary"
        elif "report" in text:
            outputs = "report: string"
        else:
            outputs = "result: string"

    state = _brief_enum(brief.get("state"), {"stateless", "cached", "persistent"}, "")
    if not state:
        state = "cached" if external or "cache" in text or "production" in text else "stateless"

    shape = _brief_enum(brief.get("shape"), {"simple", "modular", "production"}, "")
    if not shape:
        shape = "production" if external or state != "stateless" else "modular"

    rigor = _brief_text_value(brief.get("rigor"))
    if not rigor:
        rigor = "strict fakeable unit tests" if external else "strict unit tests"

    return {
        "goal": description,
        "root_id": root_id,
        "external_services": external,
        "inputs": inputs,
        "outputs": outputs,
        "state": state,
        "shape": shape,
        "rigor": rigor,
    }


BRIEF_SUGGEST_SYSTEM = """You are an architecture assistant that fills in a root-node brief.
Return ONLY a JSON object with these keys:
  root_id, goal, external_services, inputs, outputs, state, shape, rigor, rationale

Rules:
- Use the user's goal as the anchor, but fill in practical defaults where details are missing.
- root_id must be lowercase snake_case.
- goal should be a concise one-sentence root-node description.
- external_services should be a comma-separated string or empty string if none.
- inputs and outputs should be short example strings.
- state must be one of stateless, cached, persistent.
- shape must be one of simple, modular, production.
- rigor should describe unit-test discipline in one short phrase.
- rationale should be a short list of strings explaining the choices.
Keep it concrete and buildable. No markdown, no prose outside JSON."""


def brief_suggestion_enabled() -> bool:
    flag = os.environ.get("RICH_CANVAS_LLM_BRIEF", "").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def llm_brief_suggestion(brief: dict, current_tree: dict | None = None) -> dict | None:
    if not brief_suggestion_enabled():
        return None
    if not skills.is_available():
        return None
    try:
        raw = skills.call_with_retry(
            system_prompt=BRIEF_SUGGEST_SYSTEM,
            user_prompt=(
                "Current brief JSON:\n"
                f"{json.dumps(brief, indent=2)[:12000]}\n\n"
                "Current canvas JSON:\n"
                f"{json.dumps(current_tree, indent=2)[:12000] if current_tree else 'null'}"
            ),
            temperature=0.15,
            max_tokens=1200,
        )
        data = skills.parse_json_response(raw, context="BRIEF_SUGGESTION")
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        print(f"  [brief] LLM suggestion failed, using deterministic fallback: {e}")
        return None


def suggest_root_brief(brief: dict, current_tree: dict | None = None) -> dict:
    base = _brief_seed(brief, current_tree)
    llm = llm_brief_suggestion(brief, current_tree) or {}
    suggestion = {**base}
    for key in ("root_id", "goal", "external_services", "inputs", "outputs", "state", "shape", "rigor"):
        value = _brief_text_value(llm.get(key))
        if value:
            suggestion[key] = value
    suggestion["root_id"] = sanitize_module_id(suggestion["root_id"], "app")
    suggestion["state"] = _brief_enum(suggestion["state"], {"stateless", "cached", "persistent"},
                                      base["state"])
    suggestion["shape"] = _brief_enum(suggestion["shape"], {"simple", "modular", "production"},
                                       base["shape"])
    if not suggestion["goal"]:
        suggestion["goal"] = base["goal"]
    if not suggestion["external_services"]:
        suggestion["external_services"] = base["external_services"]
    if not suggestion["inputs"]:
        suggestion["inputs"] = base["inputs"]
    if not suggestion["outputs"]:
        suggestion["outputs"] = base["outputs"]
    if not suggestion["rigor"]:
        suggestion["rigor"] = base["rigor"]

    rationale = llm.get("rationale")
    if isinstance(rationale, str):
        rationale = [rationale]
    if not isinstance(rationale, list):
        rationale = []
    if not rationale:
        rationale = [
            "Fill the root node with a concrete id and a clear interface sketch.",
            "Keep provider boundaries explicit when the goal hints at external services.",
        ]
        if suggestion["state"] != "stateless":
            rationale.append("Use a cache or persistent state boundary only when the goal benefits from it.")

    preview = {
        "id": suggestion["root_id"],
        "description": suggestion["goal"],
        "external_services": suggestion["external_services"],
        "inputs": suggestion["inputs"],
        "outputs": suggestion["outputs"],
        "state": suggestion["state"],
        "shape": suggestion["shape"],
        "rigor": suggestion["rigor"],
    }

    return {
        "source": "llm" if llm else "deterministic",
        "suggestion": suggestion,
        "preview": preview,
        "rationale": rationale,
    }


def _op(name: str, inputs: dict, outputs: dict, errors: list[str] | None = None) -> dict:
    return {"name": name, "inputs": inputs, "outputs": outputs, "errors": errors or []}


def _brief_text(brief: dict) -> str:
    parts = []
    for key in ("goal", "inputs", "outputs", "external_services", "state", "rigor", "shape"):
        value = brief.get(key)
        if value:
            parts.append(str(value))
    flags = brief.get("flags") if isinstance(brief.get("flags"), list) else []
    parts.extend(str(flag) for flag in flags)
    return " ".join(parts)


def _root_id_from_brief(brief: dict) -> str:
    raw = brief.get("root_id") or brief.get("id") or brief.get("goal") or "app"
    root_id = sanitize_module_id(raw, "app")
    if len(root_id) > 32:
        root_id = "_".join(root_id.split("_")[:4]) or "app"
    return root_id


def _provider_from_brief(brief: dict) -> str:
    text = _brief_text(brief).lower()
    services = str(brief.get("external_services") or "").strip()
    if "open-meteo" in text or "weather" in text:
        return "Open-Meteo"
    if services:
        return services.splitlines()[0].split(",")[0].strip()
    if "stripe" in text:
        return "Stripe"
    if "database" in text:
        return "database"
    if "file" in text:
        return "filesystem"
    return "external provider"


def _node(node_id: str, description: str, operations: list[dict], *,
          kind: str = "pure", lane: str = "", dependencies: list[dict] | None = None,
          stateful: bool = False, external: dict | None = None) -> dict:
    return normalize_canvas_node({
        "id": node_id,
        "description": description,
        "operations": operations,
        "dependencies": dependencies or [],
        "kind": kind,
        "lane": lane,
        "external": external or {},
        "stateful": stateful,
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "planned",
    })


def weather_architecture_tree(brief: dict) -> dict:
    goal = str(brief.get("goal") or "Fetch current weather for a city and print a short report.")
    root_id = _root_id_from_brief(brief)
    provider = _provider_from_brief(brief)
    text = _brief_text(brief).lower()
    wants_cache = (
        "cache" in text or "caching" in text or "production" in text
        or str(brief.get("state") or "").lower() in {"cached", "persistent"}
        or str(brief.get("shape") or "").lower() == "production"
    )

    children = [
        _node(
            "city_input",
            "Validate and normalize the requested city before any provider call.",
            [_op("validate", {"input": "string"}, {"city": "string"}, ["invalid_city"])],
            lane="interface",
        ),
        _node(
            "weather_api_adapter",
            f"Fetch current weather data from {provider}. This is the only live provider boundary.",
            [_op("fetch", {"city": "string"}, {"weather_data": "dict"}, ["city_not_found", "fetch_failed"])],
            kind="adapter",
            lane="external",
            external=default_external(provider),
        ),
        _node(
            "weather_fetcher",
            "Coordinate weather retrieval through injected cache and adapter capabilities.",
            [_op("fetch", {"city": "string"}, {"weather_data": "dict"}, ["city_not_found", "fetch_failed"])],
            lane="domain",
            dependencies=[{"name": "weather_api", "id": "weather_api_adapter"}],
        ),
        _node(
            "weather_formatter",
            "Turn normalized weather data into a concise user-facing report.",
            [_op("format", {"weather_data": "dict"}, {"result": "string"}, [])],
            lane="presentation",
        ),
    ]
    edges = [
        {"from": "city_input", "to": "weather_fetcher", "name": "city"},
        {"from": "weather_api_adapter", "to": "weather_fetcher", "name": "weather_api"},
        {"from": "weather_fetcher", "to": "weather_formatter", "name": "weather_data"},
    ]

    if wants_cache:
        children.insert(2, _node(
            "weather_cache",
            "Hold recent weather lookups so provider failures and repeated requests do not dominate the workflow.",
            [
                _op("lookup", {"city": "string"}, {"hit": "boolean", "weather_data": "dict"}, []),
                _op("remember", {"city": "string", "weather_data": "dict"}, {"ok": "boolean"}, []),
            ],
            kind="stateful",
            lane="state",
            stateful=True,
        ))
        fetcher = next(c for c in children if c["id"] == "weather_fetcher")
        fetcher["dependencies"].append({"name": "cache", "id": "weather_cache"})
        edges.insert(2, {"from": "weather_cache", "to": "weather_fetcher", "name": "cache"})

    root = {
        "id": root_id,
        "description": goal,
        "operations": [_op("run", {"input": "string"}, {"result": "string"}, ["invalid_city", "fetch_failed"])],
        "dependencies": [],
        "kind": "pure",
        "lane": "orchestration",
        "external": {},
        "stateful": False,
        "is_leaf": False,
        "children": children,
        "edges": edges,
        "status": "planned",
    }
    return normalize_canvas_node(root)


def generic_architecture_tree(brief: dict) -> dict:
    goal = str(brief.get("goal") or "Build a small software system.")
    root_id = _root_id_from_brief(brief)
    provider = _provider_from_brief(brief)
    text = _brief_text(brief).lower()
    has_external = any(tok in text for tok in ("api", "http", "external", "provider", "network"))
    wants_state = any(tok in text for tok in ("cache", "store", "state", "persistent", "database")) \
        or str(brief.get("shape") or "").lower() == "production"

    children = [
        _node("input_boundary", "Validate and normalize user input.", [
            _op("validate", {"input": "string"}, {"request": "dict"}, ["invalid_input"])
        ], lane="interface"),
        _node("core_logic", "Apply the core domain behavior without direct external I/O.", [
            _op("process", {"request": "dict"}, {"result": "dict"}, [])
        ], lane="domain"),
        _node("presenter", "Format the domain result for the user.", [
            _op("present", {"result": "dict"}, {"output": "string"}, [])
        ], lane="presentation"),
    ]
    edges = [
        {"from": "input_boundary", "to": "core_logic", "name": "request"},
        {"from": "core_logic", "to": "presenter", "name": "result"},
    ]
    if has_external:
        adapter = _node("external_adapter", f"Own all interaction with {provider}.", [
            _op("call", {"request": "dict"}, {"provider_result": "dict"}, ["provider_failed"])
        ], kind="adapter", lane="external", external=default_external(provider))
        children.insert(1, adapter)
        core = next(c for c in children if c["id"] == "core_logic")
        core["dependencies"].append({"name": "external_adapter", "id": "external_adapter"})
        edges.insert(1, {"from": "external_adapter", "to": "core_logic", "name": "external_adapter"})
    if wants_state:
        store = _node("state_store", "Hold cached or persisted workflow state behind a fakeable boundary.", [
            _op("get", {"key": "string"}, {"hit": "boolean", "value": "dict"}, []),
            _op("put", {"key": "string", "value": "dict"}, {"ok": "boolean"}, []),
        ], kind="stateful", lane="state", stateful=True)
        children.insert(1, store)
        core = next(c for c in children if c["id"] == "core_logic")
        core["dependencies"].append({"name": "state_store", "id": "state_store"})
        edges.insert(1, {"from": "state_store", "to": "core_logic", "name": "state_store"})

    root = {
        "id": root_id,
        "description": goal,
        "operations": [_op("run", {"input": "string"}, {"output": "string"}, ["invalid_input"])],
        "dependencies": [],
        "kind": "pure",
        "lane": "orchestration",
        "external": {},
        "stateful": False,
        "is_leaf": False,
        "children": children,
        "edges": edges,
        "status": "planned",
    }
    return normalize_canvas_node(root)


def architecture_checklist(tree: dict) -> list[dict]:
    cards = validation_cards(tree)
    errors = [c for c in cards if c.get("severity") == "error"]
    nodes = flatten_tree(tree)
    adapters = [n for n in nodes if module_kind(n) == "adapter"]
    stateful = [n for n in nodes if module_kind(n) == "stateful"]
    deps = [n for n in nodes if n.get("dependencies")]
    return [
        {
            "ok": not errors,
            "title": "Build graph is structurally valid",
            "message": "No duplicate ids, broken sibling references, missing operations, or cycles."
            if not errors else f"{len(errors)} blocking graph issue(s) remain.",
        },
        {
            "ok": bool(adapters),
            "title": "External I/O is explicit",
            "message": f"{len(adapters)} adapter boundary module(s) isolate provider behavior."
            if adapters else "No adapter boundary was inferred from the brief.",
        },
        {
            "ok": all((n.get("external") or {}).get("mock_policy") == "unit_tests_mock_provider"
                      for n in adapters),
            "title": "Unit tests fake providers",
            "message": "Adapter contracts instruct unit tests to fake provider I/O and keep live calls out."
            if adapters else "No external provider test policy is needed.",
        },
        {
            "ok": True,
            "title": "State is intentional",
            "message": f"{len(stateful)} stateful/cache module(s) are isolated."
            if stateful else "The proposed design is stateless.",
        },
        {
            "ok": True,
            "title": "Held capabilities are visible",
            "message": f"{len(deps)} module(s) use injected sibling capabilities instead of hidden imports."
            if deps else "The graph is a simple dataflow pipeline.",
        },
    ]


def architecture_proposal(brief: dict, current_tree: dict | None = None) -> dict:
    text = _brief_text(brief).lower()
    preview = weather_architecture_tree(brief) if "weather" in text else generic_architecture_tree(brief)
    cards = validation_cards(preview)
    checklist = architecture_checklist(preview)
    adapters = [n["id"] for n in flatten_tree(preview) if module_kind(n) == "adapter"]
    stateful = [n["id"] for n in flatten_tree(preview) if module_kind(n) == "stateful"]
    summary_bits = [f"{len(preview.get('children', []))} modules"]
    if adapters:
        summary_bits.append(f"adapter boundary: {', '.join(adapters)}")
    if stateful:
        summary_bits.append(f"state boundary: {', '.join(stateful)}")
    tx = {
        "label": "Architecture proposal",
        "summary": "; ".join(summary_bits),
        "source": "deterministic-brief",
        "ops": [{"op": "replace_tree", "tree": preview}],
    }
    rationale = [
        "Keep provider I/O behind adapter contracts so generated unit tests fake it.",
        "Keep orchestration separate from formatting and provider normalization.",
        "Represent cache/state as its own module when the brief asks for production hardening or persistence.",
    ]
    if current_tree:
        rationale.append("Applying this proposal replaces the current canvas; use Undo to recover the previous graph.")
    return {
        "brief": brief,
        "transaction": tx,
        "preview_tree": preview,
        "cards": cards,
        "checklist": checklist,
        "rationale": rationale,
    }


def inspect_external_io_risks(tree: dict | None) -> list[dict]:
    cards = []
    if not tree:
        return cards
    for n in flatten_tree(tree):
        if n.get("status") != "failed":
            continue
        node_dir = BUILD_ROOT / n["id"]
        src = "\n".join(p.read_text(errors="replace") for p in (node_dir / "src").glob("*.py")) \
            if (node_dir / "src").exists() else ""
        tests = "\n".join(p.read_text(errors="replace") for p in (node_dir / "tests").glob("test_*.py")) \
            if (node_dir / "tests").exists() else ""
        has_live_io = any(tok in src for tok in ("urllib.request", "requests.", "http.client"))
        tests_patch = any(tok in tests for tok in ("monkeypatch", "unittest.mock", "Mock(", "patch("))
        if (has_live_io or looks_external(n)) and module_kind(n) != "adapter":
            cards.append({
                "severity": "warning",
                "node_id": n["id"],
                "title": "External I/O hidden in pure module",
                "message": "This looks like provider/network code but is not marked as an adapter. Unit verification can become flaky or provider-dependent.",
                "action": "mark_adapter",
            })
        if has_live_io and tests and not tests_patch and module_kind(n) == "adapter":
            cards.append({
                "severity": "error",
                "node_id": n["id"],
                "title": "Adapter tests still hit live I/O",
                "message": "This node is already an adapter, but its generated tests still appear to call the provider without monkeypatching or faking I/O. Regenerate this module after preserving the adapter boundary.",
                "action": None,
            })
        elif has_live_io and tests and not tests_patch:
            cards.append({
                "severity": "error",
                "node_id": n["id"],
                "title": "Unit tests may hit live external I/O",
                "message": "Generated tests appear to exercise provider code without monkeypatching/mocking the network boundary. Mark this module as an adapter and regenerate tests.",
                "action": "mark_adapter",
            })
    return cards


# ── Planning ───────────────────────────────────────────────────────

def plan_node(cnode: dict) -> None:
    """Decompose ONE node a single layer (1 LLM call). Mutates cnode in place."""
    normalize_canvas_node(cnode)
    contract = node_to_contract(cnode)
    decision = skills.plan(contract, allow_decompose=True)
    if decision.get("is_leaf", True):
        cnode["is_leaf"] = True
        cnode["children"] = []
        cnode["edges"] = []
    else:
        cnode["is_leaf"] = False
        cnode["children"] = [contract_to_node(cc) for cc in decision.get("children", [])]
        cnode["edges"] = decision.get("edges", []) or []
    cnode["status"] = "planned"


def vibe_node(cnode: dict, depth: int = 0) -> None:
    """Recursively PLAN a subtree down to MAX_DEPTH (auto-plan)."""
    normalize_canvas_node(cnode)
    print(f"  [vibe] planning {cnode['id']} (depth {depth})")
    plan_node(cnode)
    if cnode["is_leaf"]:
        return
    if depth + 1 >= MAX_DEPTH:
        # At the depth boundary the engine forces children to leaves; mirror that
        # here without spending a call per child.
        for ch in cnode["children"]:
            ch["is_leaf"] = True
            ch["status"] = "planned"
        return
    for ch in cnode["children"]:
        vibe_node(ch, depth + 1)


# ── Building ───────────────────────────────────────────────────────

def canvas_to_engine_node(cnode: dict) -> Node:
    normalize_canvas_node(cnode)
    contract = node_to_contract(cnode)
    is_leaf = cnode.get("is_leaf")
    if is_leaf is None:
        is_leaf = True   # unplanned → treat as leaf; build() will leaf it anyway
    n = Node(id=cnode["id"], contract=contract, is_leaf=bool(is_leaf))
    if not is_leaf:
        n.children = [canvas_to_engine_node(c) for c in cnode.get("children", []) or []]
        n.edges = cnode.get("edges", []) or []
    n.dependencies = contract.get("dependencies", []) or []
    return n


def _already_verified(enode: Node) -> bool:
    """True iff this node has a verified status + matching memo on disk — so we must
    NOT clobber it back to 'planned' (preserves resumption / memo reuse)."""
    p = enode.path()
    memo, st = p / "memo.txt", p / "status.json"
    try:
        if memo.exists() and st.exists():
            if (memo.read_text().strip() == _contract_hash(enode.contract)
                    and json.loads(st.read_text()).get("status") == "verified"):
                return True
    except Exception:
        pass
    return False


def persist_planned(enode: Node) -> None:
    """Write contract.yaml + decision.json (+ status=planned) for every node, so
    build()'s decision-reuse path honors the hand-planned tree instead of re-PLANning.
    Verified, hash-matching nodes are left untouched (memo hit on rebuild)."""
    if _already_verified(enode):
        return
    save_contract(enode)
    save_decision(enode)
    (enode.path() / "decision_source.txt").write_text("manual\n")
    save_status(enode, "planned")
    for c in enode.children:
        persist_planned(c)


def collect_statuses(cnode: dict, out: dict) -> dict:
    """Read build/<id>/status.json for every planned node (post-build coloring)."""
    p = BUILD_ROOT / cnode["id"] / "status.json"
    try:
        out[cnode["id"]] = json.loads(p.read_text()).get("status", "unknown")
    except Exception:
        out[cnode["id"]] = cnode.get("status", "unplanned")
    for c in cnode.get("children", []) or []:
        collect_statuses(c, out)
    return out


def read_node_artifacts(node_id: str) -> dict:
    """Read what a build produced for one module so the canvas can show it inline:
    status + reason, the persisted test report, and the generated src/ + tests/ files.
    Read-only; returns empty sections when the node hasn't been built yet."""
    node_dir = BUILD_ROOT / node_id
    out = {"id": node_id, "status": "unplanned", "reason": None,
           "report": None, "src": {}, "tests": {}, "built": node_dir.exists()}
    try:
        st = json.loads((node_dir / "status.json").read_text())
        out["status"] = st.get("status", "unplanned")
        out["reason"] = st.get("reason")
    except Exception:
        pass
    try:
        out["report"] = json.loads((node_dir / "test_report.json").read_text())
    except Exception:
        pass
    for section, sub in (("src", "src"), ("tests", "tests")):
        d = node_dir / sub
        if d.is_dir():
            for f in sorted(d.glob("*.py")):
                try:
                    out[section][f.name] = f.read_text()
                except Exception:
                    pass
    return out


def do_build(tree: dict, only_node_id: str | None = None) -> dict:
    normalize_canvas_node(tree)
    cards = validation_cards(tree)
    blocking = [c for c in cards if c.get("severity") == "error"]
    if blocking:
        return {
            "ok": False,
            "error": "project validation failed",
            "calls": 0,
            "statuses": collect_statuses(tree, {}),
            "main_path": None,
            "run_returncode": None,
            "run_stdout": "",
            "run_stderr": "",
            "cards": cards,
        }

    root_contract = node_to_contract(tree)
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    persist_planned(canvas_to_engine_node(tree))
    save_project(tree)

    # Scoped rebuild: invalidate just this node so build(root) descends past every
    # memo-hit sibling and rebuilds only it (+ the ancestor wiring re-verifying against
    # it). persist_planned already re-stamped it as a manual planned node above.
    if only_node_id:
        buildmod.invalidate_node(only_node_id)

    counter = [0]
    ok, err = True, None
    try:
        root = build(root_contract, allow_decompose=True, llm_call_counter=counter)
    except BuildFailure as e:
        ok, err = False, str(e)
        root = None

    run_out = run_err = ""
    run_rc = None
    main_path = None
    if root is not None:
        main_path = str(assemble(root))
        print(f"  [build] assembled → {main_path}; running main.py …")
        try:
            r = subprocess.run([sys.executable, "main.py"], capture_output=True,
                               text=True, timeout=30, cwd=str(BUILD_ROOT))
            run_out, run_err, run_rc = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            run_err, run_rc = "deliverable run timed out (30s)", -1

    return {
        "ok": ok,
        "error": err,
        "calls": counter[0],
        "statuses": collect_statuses(tree, {}),
        "main_path": main_path,
        "run_returncode": run_rc,
        "run_stdout": run_out,
        "run_stderr": run_err,
        "cards": validation_cards(tree) + inspect_external_io_risks(tree),
    }


# ── Job model (single active background job) ────────────────────────

class Job:
    def __init__(self, kind: str):
        self.id = f"{kind}-{int(time.time() * 1000)}"
        self.kind = kind
        self.status = "running"     # running | done | error
        self.log: list[str] = []
        self.result = None
        self.error = None
        self._lock = threading.Lock()

    def emit(self, line: str):
        with self._lock:
            self.log.append(line)

    def snapshot(self, since: int) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "log": self.log[since:],
                "log_len": len(self.log),
                "result": self.result,
                "error": self.error,
            }


_jobs: dict[str, Job] = {}
_active: str | None = None
_jobs_lock = threading.Lock()


class _Tee:
    """Mirror stdout to the active job's log, line by line."""
    def __init__(self, job: Job, orig):
        self.job, self.orig, self.buf = job, orig, ""

    def write(self, s: str):
        self.orig.write(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.job.emit(line)

    def flush(self):
        self.orig.flush()


def _run_job(job: Job, fn):
    global _active
    old = sys.stdout
    sys.stdout = _Tee(job, old)
    try:
        job.result = fn(job)
        job.status = "done"
    except Exception as e:
        job.error = f"{type(e).__name__}: {e}"
        job.status = "error"
        print(f"ERROR: {job.error}")
        print(traceback.format_exc())
    finally:
        sys.stdout = old
        with _jobs_lock:
            _active = None


def start_job(kind: str, fn) -> Job | None:
    """Start fn(job) in a background thread. Returns None if a job is already running."""
    global _active
    with _jobs_lock:
        if _active is not None:
            return None
        job = Job(kind)
        _jobs[job.id] = job
        _active = job.id
    threading.Thread(target=_run_job, args=(job, fn), daemon=True).start()
    return job


# ── HTTP ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet default logging
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if n < 0 or n > MAX_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        if not n:
            return {}
        value = json.loads(self.rfile.read(n).decode())
        if not isinstance(value, dict):
            raise ValueError("JSON request body must be an object")
        return value

    _CTYPES = {
        ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".woff": "font/woff", ".woff2": "font/woff2", ".map": "application/json",
    }

    def _serve_static(self, path: str):
        """Serve the built React app from web/dist (SPA). Clear message if not built."""
        index = WEB_DIST / "index.html"
        if not index.exists():
            msg = (b"<h1>RICH Canvas frontend not built</h1>"
                   b"<p>Run <code>npm --prefix web install && npm --prefix web run build</code>, "
                   b"then reload. For development use <code>npm --prefix web run dev</code> "
                   b"(Vite proxies /api to this server).</p>")
            return self._send(503, msg, "text/html; charset=utf-8")
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        target = (WEB_DIST / rel).resolve()
        try:
            target.relative_to(WEB_DIST.resolve())
        except ValueError:
            return self._send(403, b"forbidden", "text/plain")
        if not target.is_file():
            target = index  # SPA fallback
        ctype = self._CTYPES.get(target.suffix, "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/v2" or path.startswith("/v2/"):
            response = _get_v2_application().handle(
                "GET",
                path,
                headers={key: value for key, value in self.headers.items()},
                query=parse_qs(parsed.query),
            )
            return self._json(response.status, response.body)
        if path == "/api/job":
            qs = parse_qs(urlparse(self.path).query)
            jid = (qs.get("id") or [""])[0]
            since = int((qs.get("since") or ["0"])[0])
            job = _jobs.get(jid)
            if not job:
                return self._json(404, {"error": "no such job"})
            return self._json(200, job.snapshot(since))
        if path == "/api/config":
            return self._json(200, {
                "max_depth": MAX_DEPTH,
                "backend": BACKEND_NAME,
                "backend_available": backend.is_available(default=BACKEND_NAME),
                "project_path": str(PROJECT_PATH),
            })
        if path == "/api/project":
            return self._json(200, load_project())
        if path == "/api/node":
            qs = parse_qs(urlparse(self.path).query)
            nid = (qs.get("id") or [""])[0]
            if not nid:
                return self._json(400, {"error": "id required"})
            return self._json(200, read_node_artifacts(nid))
        if path == "/api/statuses":
            # Live per-module status (read from build/<id>/status.json on disk) so the
            # canvas can flip node colors as a build progresses.
            doc = load_project()
            tree = doc.get("tree")
            return self._json(200, {"statuses": collect_statuses(tree, {}) if tree else {}})
        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        return self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_body()
        except Exception as e:
            return self._json(400, {"error": f"bad body: {e}"})

        if path == "/v2" or path.startswith("/v2/"):
            response = _get_v2_application().handle(
                "POST",
                path,
                body=body,
                headers={key: value for key, value in self.headers.items()},
                query=parse_qs(parsed.query),
            )
            return self._json(response.status, response.body)

        if path == "/api/plan":
            tree, nid = body.get("tree"), body.get("node_id")

            def fn(job, tree=tree, nid=nid):
                normalize_canvas_node(tree)
                target = find_node(tree, nid)
                if target is None:
                    raise ValueError(f"node {nid!r} not in tree")
                plan_node(target)
                save_project(tree, body.get("history") or [])
                return {"tree": tree, "cards": validation_cards(tree)}
            return self._start("plan", fn)

        if path == "/api/vibe":
            tree, nid = body.get("tree"), body.get("node_id")

            def fn(job, tree=tree, nid=nid):
                normalize_canvas_node(tree)
                target = find_node(tree, nid)
                if target is None:
                    raise ValueError(f"node {nid!r} not in tree")
                vibe_node(target)
                save_project(tree, body.get("history") or [])
                return {"tree": tree, "cards": validation_cards(tree)}
            return self._start("vibe", fn)

        if path == "/api/build":
            tree = body.get("tree")
            only = body.get("node_id")  # optional: rebuild just this module

            def fn(job, tree=tree, only=only):
                return do_build(tree, only_node_id=only)
            return self._start("build", fn)

        if path == "/api/validate":
            tree = body.get("tree")
            return self._json(200, {"cards": validation_cards(tree) + inspect_external_io_risks(tree)})

        if path == "/api/project/save":
            doc = save_project(body.get("tree"), body.get("history") or [])
            return self._json(200, {"ok": True, "project": doc, "path": str(PROJECT_PATH)})

        if path == "/api/architecture/propose":
            brief = body.get("brief") or {}
            if isinstance(brief, str):
                brief = {"goal": brief}
            proposal = architecture_proposal(brief, body.get("tree"))
            return self._json(200, proposal)

        if path == "/api/architecture/suggest-brief":
            brief = body.get("brief") or {}
            if isinstance(brief, str):
                brief = {"goal": brief}
            suggestion = suggest_root_brief(brief, body.get("tree"))
            return self._json(200, suggestion)

        if path == "/api/vibe-edit":
            tree = body.get("tree")
            if not tree:
                return self._json(400, {"error": "tree required"})
            normalize_canvas_node(tree)
            tx = transaction_for_prompt(tree, body.get("prompt", ""), body.get("node_id"))
            before = copy.deepcopy(tree)
            tree = apply_transaction(tree, tx)
            cards = validation_cards(tree) + inspect_external_io_risks(tree)
            # preview mode: compute the change but DON'T commit (no save). The frontend
            # shows a diff with Apply/Discard; Apply commits via /api/project/save.
            if body.get("preview"):
                return self._json(200, {"tree": tree, "transaction": tx, "cards": cards,
                                        "preview": True})
            history = body.get("history") or []
            history = ([{"ts": round(time.time(), 3),
                         "transaction": tx,
                         "before": before,
                         "after": copy.deepcopy(tree)}] + history)[:50]
            save_project(tree, history)
            return self._json(200, {"tree": tree, "transaction": tx,
                                    "history": history, "cards": cards})

        return self._json(404, {"error": "not found"})

    def _start(self, kind: str, fn):
        job = start_job(kind, fn)
        if job is None:
            return self._json(409, {"error": "a job is already running"})
        return self._json(200, {"job_id": job.id})


def main():
    if not backend.is_available(default=BACKEND_NAME):
        print(f"WARNING: selected RICH_BACKEND={BACKEND_NAME!r} is unavailable — "
              "planning/building will fail.",
              file=sys.stderr)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"RICH canvas → http://localhost:{PORT}  (backend: {BACKEND_NAME}, MAX_DEPTH={MAX_DEPTH})")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
