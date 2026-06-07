"""RICH read-only tree viewer (Phase 8 instrumentation).

A function from RICH's on-disk tree to a picture. It PARSES a build/ directory and
RENDERS the decomposition DAG + its verification state. It is strictly READ-ONLY and
side-effect-free on the tree: it never calls build, never writes/edits node artifacts,
never composes nodes, never triggers a run. It only READS build/<id>/{contract.yaml,
decision.json,status.json,deps.yaml} and WRITES a standalone HTML/SVG to an output path
OUTSIDE build/.

This is the standalone BACK-HALF of the eventual canvas (inspect a tree), built in
isolation. Authoring (edit/compose/rebuild) is deliberately NOT here — inspect, not author.

Two views of the same tree (§3):
  1. STRUCTURE — decomposition (containment) + dependency edges; fan-in (a shared
     dependency, out-degree>1 in {from} terms) flagged; the DRAGON's signature — a node
     that is STATEFUL and SHARED (stateful + >1 consumer) — called out distinctly.
  2. VERIFICATION STATE — same layout, nodes colored by status.json (verified=green,
     failed=red, planned/other=amber), with persisted `reason` annotations (REPLAN /
     budget / cut-off) shown ONLY when actually on disk.

Usage:  python tree_viewer.py [build_dir=build] [out_html=/tmp/rich_tree.html]
"""
import html
import json
import subprocess
import sys
from pathlib import Path

import yaml


# ── load (read-only) ────────────────────────────────────────────────

def load_tree(build_dir: str | Path) -> dict:
    """Parse a build/ directory into a tree record. Pure read; no writes anywhere."""
    root = Path(build_dir)
    nodes: dict[str, dict] = {}
    containment: list[tuple[str, str]] = []   # (parent, child)
    dep_edges: list[tuple[str, str, str]] = []  # (consumer, dependency, name)

    if not root.exists():
        raise FileNotFoundError(f"build dir not found: {root}")

    # A node is a SUBDIR holding a decision.json or contract.yaml (skip flat .py / main.py / __pycache__).
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if d.name == "__pycache__":
            continue
        dec_f, con_f, st_f, dep_f = (d / "decision.json", d / "contract.yaml",
                                     d / "status.json", d / "deps.yaml")
        if not dec_f.exists() and not con_f.exists():
            continue
        nid = d.name
        contract = yaml.safe_load(con_f.read_text()) if con_f.exists() else {}
        decision = json.loads(dec_f.read_text()) if dec_f.exists() else {}
        status_d = json.loads(st_f.read_text()) if st_f.exists() else {}
        nodes[nid] = {
            "id": nid,
            "is_leaf": decision.get("is_leaf", True),
            "stateful": bool((contract or {}).get("stateful", False)),
            "status": status_d.get("status", "?"),
            "reason": status_d.get("reason"),
            "decision": decision,
            "contract": contract or {},
        }
        # containment: an internal node lists its children's contracts
        for child in decision.get("children", []) or []:
            containment.append((nid, child["id"]))
        # dependency edges among this node's children: {from: dep, to: consumer}
        for e in decision.get("edges", []) or []:
            dep_edges.append((e.get("to"), e.get("from"), e.get("name", "")))
        # a leaf's own held deps (deps.yaml / contract.dependencies): dep -> this node
        held = []
        if dep_f.exists():
            held = yaml.safe_load(dep_f.read_text()) or []
        if not held:
            held = (contract or {}).get("dependencies", []) or []
        for dep in held:
            if isinstance(dep, dict) and "id" in dep:
                dep_edges.append((nid, dep["id"], dep.get("name", "")))

    # dedupe dep edges (root edges + leaf deps can name the same relationship)
    dep_edges = sorted(set(e for e in dep_edges if e[0] in nodes and e[1] in nodes))

    # shared-dependency in-degree = number of distinct consumers depending on a node
    consumers: dict[str, set] = {nid: set() for nid in nodes}
    for consumer, dependency, _name in dep_edges:
        consumers[dependency].add(consumer)
    for nid, n in nodes.items():
        n["shared_count"] = len(consumers[nid])
        n["fan_in"] = len(consumers[nid]) > 1
        n["dragon"] = n["stateful"] and n["fan_in"]   # the Phase-8 signature

    # root(s) = nodes never appearing as a child
    children_ids = {c for _p, c in containment}
    roots = [nid for nid in nodes if nid not in children_ids]
    return {"nodes": nodes, "containment": sorted(set(containment)),
            "dep_edges": dep_edges, "roots": roots}


# ── render (DOT → SVG via graphviz) ─────────────────────────────────

_STATUS_FILL = {"verified": "#d6f5d6", "failed": "#f8d4d4",
                "planned": "#fdf3d0", "implemented": "#d8e8f8"}
_STATUS_PEN = {"verified": "#2e8b57", "failed": "#c0392b",
               "planned": "#b8860b", "implemented": "#2c6fb0"}


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def to_dot(tree: dict, view: str) -> str:
    """view: 'structure' (shapes/fan-in/dragon) or 'state' (status colors + reasons)."""
    nodes, lines = tree["nodes"], []
    lines.append(f'digraph rich_{view} {{')
    lines.append('  rankdir=TB; bgcolor="white"; fontname="Helvetica";')
    lines.append('  node [fontname="Helvetica", fontsize=11, style="filled,rounded"];')
    lines.append('  edge [fontname="Helvetica", fontsize=9];')

    for nid, n in nodes.items():
        kind = "leaf" if n["is_leaf"] else "internal"
        shape = "folder" if not n["is_leaf"] else "box"
        badges = []
        if n["stateful"]:
            badges.append("⟳ stateful")
        if n["fan_in"]:
            badges.append(f"⇉ shared ×{n['shared_count']}")
        if view == "structure":
            fill = "#fbe9ff" if n["stateful"] else "#eef2f7"
            pen = "#7d3c98" if n["stateful"] else "#5d6d7e"
            penw = "1.5"
        else:  # state view
            fill = _STATUS_FILL.get(n["status"], "#eeeeee")
            pen = _STATUS_PEN.get(n["status"], "#888888")
            penw = "1.5"
            badges.append(f"[{n['status']}]")
            if n.get("reason"):
                badges.append("⚑ " + n["reason"][:48])
        # the dragon overrides — it is the point of this phase
        if n["dragon"]:
            pen, penw = "#b03a2e", "3"
            if view == "structure":
                fill = "#ffe0d6"
            badges.insert(0, "🐉 SHARED MUTABLE")
        elif n["fan_in"] and view == "structure":
            penw = "2.5"

        label = "<br/>".join([f"<b>{html.escape(nid)}</b>",
                              html.escape(kind),
                              *[html.escape(b) for b in badges]])
        lines.append(f'  "{_esc(nid)}" [shape={shape}, fillcolor="{fill}", '
                     f'color="{pen}", penwidth={penw}, label=<{label}>];')

    # containment edges (decomposition tree) — light, drive the layout (rank)
    for parent, child in tree["containment"]:
        lines.append(f'  "{_esc(parent)}" -> "{_esc(child)}" '
                     f'[style=dashed, color="#b0b0b0", arrowhead=none, label="contains", '
                     f'fontcolor="#b0b0b0"];')

    # dependency edges (consumer -> dependency); red into a dragon
    for consumer, dependency, name in tree["dep_edges"]:
        into_dragon = nodes[dependency]["dragon"]
        col = "#b03a2e" if into_dragon else "#2c3e50"
        penw = "2.2" if into_dragon else "1.2"
        lines.append(f'  "{_esc(consumer)}" -> "{_esc(dependency)}" '
                     f'[color="{col}", penwidth={penw}, constraint=false, '
                     f'label="{_esc(name)}", fontcolor="{col}"];')

    lines.append("}")
    return "\n".join(lines)


def _svg(dot: str) -> str:
    """Render DOT to inline SVG via graphviz. Returns the <svg> markup (or a notice)."""
    try:
        out = subprocess.run(["dot", "-Tsvg"], input=dot, capture_output=True,
                             text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout[out.stdout.find("<svg"):]
        return f"<pre>graphviz error:\n{html.escape(out.stderr)}</pre>"
    except FileNotFoundError:
        return ("<p><b>graphviz 'dot' not found</b> — DOT source below; "
                "render with <code>dot -Tsvg</code>.</p><pre>" + html.escape(dot) + "</pre>")


def render(build_dir: str | Path = "build", out_html: str | Path = "/tmp/rich_tree.html",
           title: str | None = None) -> str:
    """Read build_dir, write a standalone HTML with both views. Returns out path."""
    tree = load_tree(build_dir)
    title = title or f"RICH tree — {Path(build_dir).resolve()}"
    n = len(tree["nodes"])
    dragons = [k for k, v in tree["nodes"].items() if v["dragon"]]
    fanins = [k for k, v in tree["nodes"].items() if v["fan_in"]]
    summary = (f"{n} nodes · roots={tree['roots']} · fan-in={fanins or 'none'} · "
               f"dragon(shared-mutable)={dragons or 'none'}")
    doc = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>body{font-family:Helvetica,Arial,sans-serif;margin:24px;color:#222}"
        "h1{font-size:18px}h2{font-size:14px;margin-top:28px}"
        ".sub{color:#666;font-size:12px;margin:4px 0 16px}"
        ".legend{font-size:11px;color:#444;margin:8px 0 4px}"
        ".box{border:1px solid #ddd;border-radius:6px;padding:8px;overflow:auto}</style>",
        f"<h1>{html.escape(title)}</h1>",
        f"<div class='sub'>{html.escape(summary)}</div>",
        "<div class='legend'>🐉 = stateful + shared (dragon) · ⇉ = shared dependency (fan-in) · "
        "⟳ = stateful · folder = internal node · box = leaf</div>",
        "<h2>1 — Decomposition structure</h2>",
        "<div class='box'>" + _svg(to_dot(tree, "structure")) + "</div>",
        "<div class='legend'>green=verified · red=failed · amber=planned · ⚑=persisted reason</div>",
        "<h2>2 — Verification state</h2>",
        "<div class='box'>" + _svg(to_dot(tree, "state")) + "</div>",
    ]
    out = Path(out_html)
    out.write_text("\n".join(doc))   # writes OUTSIDE build/ — no side effect on the tree
    return str(out)


if __name__ == "__main__":
    bd = sys.argv[1] if len(sys.argv) > 1 else "build"
    oh = sys.argv[2] if len(sys.argv) > 2 else "/tmp/rich_tree.html"
    path = render(bd, oh)
    t = load_tree(bd)
    print(f"rendered {len(t['nodes'])} nodes -> {path}")
    print(f"  roots:   {t['roots']}")
    print(f"  fan-in:  {[k for k,v in t['nodes'].items() if v['fan_in']]}")
    print(f"  dragon:  {[k for k,v in t['nodes'].items() if v['dragon']]}")
    for nid, n in t["nodes"].items():
        flags = "".join(c for c, on in [("S", n["stateful"]), ("F", n["fan_in"]),
                                        ("D", n["dragon"])] if on)
        print(f"    {nid:20s} {'leaf' if n['is_leaf'] else 'internal':9s} "
              f"{n['status']:9s} shared×{n['shared_count']} {flags}")
