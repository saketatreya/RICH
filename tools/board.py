#!/usr/bin/env python3
"""The delivery board: cards are files, the page is rendered, the health is measured.

``docs/board/cards/*.md`` is the tracker. Each file is one card -- YAML front matter
plus a Markdown body -- so two tracks working in two worktrees never edit the same
file to move different cards. ``docs/board.html`` is *rendered* from them and never
edited by hand; the health strip is computed from git and from a recorded
verification run, so the board cannot claim a test count nobody measured.

    python tools/board.py check                  # validate the cards, exit 1 on a problem
    python tools/board.py render                 # write docs/board.html
    python tools/board.py verify                 # ruff + pytest + typecheck → health.json, then render
    python tools/board.py move ID STATUS [--sha S] [--why TEXT]
    python tools/board.py new ID --title T --milestone M --track T [--status S] [--body B]

Statuses: backlog → next → doing → done, plus blocked (needs ``blocked_by``) and
decision (needs ``verdict``). A ``done`` work card needs the commit that shipped it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "docs" / "board" / "cards"
HEALTH = ROOT / "docs" / "board" / "health.json"
PAGE = ROOT / "docs" / "board.html"

STATUSES = ("backlog", "next", "doing", "blocked", "done", "decision")
TRACKS = ("P", "E", "K", "core", "docs", "release")
KINDS = ("work", "milestone", "decision")
MILESTONES = tuple(f"M{n}" for n in range(23))
RELEASES = ("2.0", "2.1", "2.2")
SHA = re.compile(r"^[0-9a-f]{7,40}$")


class BoardError(ValueError):
    pass


@dataclass
class Card:
    id: str
    title: str
    status: str
    track: str
    milestone: str
    kind: str = "work"
    body: str = ""
    tag: str = ""
    size: str = ""
    release: str = ""
    sha: str = ""
    started: str = ""
    finished: str = ""
    blocked_by: str = ""
    verdict: str = ""
    drive: str = ""
    order: int = 0
    path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── loading ────────────────────────────────────────────────────────────────


def _split(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise BoardError(f"{path.name}: missing front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise BoardError(f"{path.name}: unterminated front matter")
    meta = yaml.safe_load(text[4:end]) or {}
    if not isinstance(meta, dict):
        raise BoardError(f"{path.name}: front matter must be a mapping")
    return meta, text[end + 5 :].strip()


def load_cards(directory: Path = CARDS) -> list[Card]:
    cards: list[Card] = []
    for path in sorted(directory.glob("*.md")):
        meta, body = _split(path.read_text(encoding="utf-8"), path)
        known = {f for f in Card.__dataclass_fields__ if f not in {"path", "extra", "body"}}
        fields = {k: v for k, v in meta.items() if k in known}
        extra = {k: v for k, v in meta.items() if k not in known}
        for key in ("sha", "release", "size", "started", "finished", "milestone"):
            if key in fields and fields[key] is not None:
                fields[key] = str(fields[key])
        try:
            card = Card(body=body, path=path, extra=extra, **fields)
        except TypeError as exc:
            raise BoardError(f"{path.name}: {exc}") from None
        cards.append(card)
    return cards


# ── validation ─────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _commit_exists(sha: str) -> bool | None:
    """True/False, or None when the checkout is shallow and cannot know."""

    if _git("rev-parse", "--is-shallow-repository") == "true":
        return None
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def check(cards: list[Card], *, verify_git: bool = True) -> list[str]:
    """Every rule the board is held to. Returns problems; empty means consistent."""

    problems: list[str] = []
    seen: set[str] = set()
    milestones = {c.milestone: c for c in cards if c.kind == "milestone"}
    for card in cards:
        where = card.path.name if card.path else card.id
        if card.path and card.path.stem != card.id:
            problems.append(f"{where}: id {card.id!r} does not match the filename")
        if card.id in seen:
            problems.append(f"{where}: duplicate id {card.id!r}")
        seen.add(card.id)
        if not card.title.strip():
            problems.append(f"{where}: empty title")
        if card.status not in STATUSES:
            problems.append(f"{where}: status {card.status!r} is not one of {STATUSES}")
        if card.track not in TRACKS:
            problems.append(f"{where}: track {card.track!r} is not one of {TRACKS}")
        if card.kind not in KINDS:
            problems.append(f"{where}: kind {card.kind!r} is not one of {KINDS}")
        if card.milestone != "pre" and card.milestone not in MILESTONES:
            problems.append(f"{where}: milestone {card.milestone!r} is unknown")
        if card.extra:
            problems.append(f"{where}: unknown fields {sorted(card.extra)}")
        if card.kind == "decision":
            if card.status != "decision" or card.verdict not in {"kept", "reversed"}:
                problems.append(f"{where}: a decision needs status 'decision' and a verdict")
            continue
        if card.status == "decision":
            problems.append(f"{where}: only a decision card may have status 'decision'")
        if card.kind == "milestone":
            if card.release not in RELEASES:
                problems.append(f"{where}: a milestone needs a release in {RELEASES}")
            if not card.size:
                problems.append(f"{where}: a milestone needs a size")
        elif card.milestone != "pre" and card.milestone not in milestones:
            problems.append(f"{where}: refers to milestone {card.milestone} which has no card")
        if card.status == "done":
            if card.kind == "work":
                if not SHA.match(card.sha):
                    problems.append(f"{where}: a done card needs the commit that shipped it")
                elif verify_git and _commit_exists(card.sha) is False:
                    problems.append(f"{where}: commit {card.sha} is not in this repository")
            if not card.finished and card.milestone != "pre":
                problems.append(f"{where}: a done card needs a finished date")
        if card.status == "doing" and not card.started:
            problems.append(f"{where}: a card in progress needs a started date")
        if card.status == "blocked" and not card.blocked_by:
            problems.append(f"{where}: a blocked card must say what blocks it")
    for name in MILESTONES:
        found = [c for c in cards if c.kind == "milestone" and c.milestone == name]
        if len(found) != 1:
            problems.append(f"milestone {name} has {len(found)} cards; exactly one is required")
    return problems


# ── health ─────────────────────────────────────────────────────────────────


def _run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    return result.returncode, (result.stdout + result.stderr)


def _working_tree() -> str:
    """The git tree of the working directory as it is now, index untouched.

    Verification runs before the commit that carries its result, so a commit id
    cannot say whether what was verified is what HEAD holds. A tree written from
    a throwaway index can: it covers tracked changes and untracked files alike,
    respects .gitignore, and leaves the real index alone.
    """

    index = ROOT / ".rich" / "board.index"
    index.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    for cmd in (["git", "read-tree", "HEAD"], ["git", "add", "-A"]):
        subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, check=True)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    return tree.stdout.strip()


def _fingerprint(tree: str) -> str:
    """What the gates saw: the tree, minus the board's own rendered files."""

    listing = [
        line
        for line in _git("ls-tree", "-r", tree).splitlines()
        if not line.endswith("\tdocs/board.html") and "\tdocs/board/" not in line
    ]
    return hashlib.sha256("\n".join(listing).encode("utf-8")).hexdigest()[:12]


def verify() -> dict[str, Any]:
    """Run the offline gates and record what they said, at which commit."""

    ruff_code, _ = _run([sys.executable, "-m", "ruff", "check", "."])
    pytest_code, pytest_out = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    # The summary line lists each outcome it saw, in pytest's own order; read
    # every count separately so a failure is a number, never an inference.
    counts = {
        key: int(found.group(1))
        for key in ("passed", "failed", "skipped", "error")
        if (found := re.search(rf"(\d+) {key}", pytest_out))
    }
    tsc_code, _ = _run(["npm", "--prefix", "web", "run", "typecheck"])
    record = {
        "commit": _git("rev-parse", "--short", "HEAD"),
        "fingerprint": _fingerprint(_working_tree()),
        "verified_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "ruff_ok": ruff_code == 0,
        "pytest_ok": pytest_code == 0,
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0) + counts.get("error", 0),
        "skipped": counts.get("skipped", 0),
        "typecheck_ok": tsc_code == 0,
    }
    HEALTH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _github_repository() -> str | None:
    """owner/name from the origin remote, or None when there is no GitHub remote."""

    try:
        url = _git("remote", "get-url", "origin")
    except Exception:  # noqa: BLE001 - no remote is a legitimate state
        return None
    found = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url.strip())
    return f"{found.group(1)}/{found.group(2)}" if found else None


def ci_status(branch: str = "main", *, timeout: float = 6.0) -> dict[str, str]:
    """What GitHub Actions last concluded on the branch -- asked, never assumed.

    The strip's other numbers are measured on this machine; a workflow GitHub
    refuses to start fails with no jobs and no log, and nothing local can see
    that. So the board asks. Unreachable is reported as unknown, in words.
    """

    repository = _github_repository()
    if repository is None:
        return {"state": "unknown", "detail": "no GitHub remote"}
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs?branch={branch}&per_page=1",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "rich-board"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"state": "unknown", "detail": f"GitHub unreachable: {type(exc).__name__}"}
    runs = document.get("workflow_runs") or []
    if not runs:
        return {"state": "unknown", "detail": "no runs yet"}
    run = runs[0]
    sha = str(run.get("head_sha", ""))[:7]
    if run.get("status") != "completed":
        return {"state": "running", "detail": f"{run.get('status')} at {sha}", "sha": sha}
    conclusion = str(run.get("conclusion") or "unknown")
    return {
        "state": "green" if conclusion == "success" else "red",
        "detail": f"{conclusion} at {sha}",
        "sha": sha,
    }


def _ci_python_matrix() -> str:
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    if not workflow.exists():
        return "no CI"
    found = re.search(r"python-version:\s*\[([^\]]+)\]", workflow.read_text())
    if not found:
        return "no matrix"
    versions = [v.strip().strip('"') for v in found.group(1).split(",")]
    return f"{versions[0]}–{versions[-1]}" if len(versions) > 1 else versions[0]


# ── rendering ──────────────────────────────────────────────────────────────


def _ci_stat(ci: dict[str, str] | None) -> str:
    """The strip's CI cell: GitHub's word for main, or the admission nobody asked."""

    if ci is None:
        return '<div class="stat warn"><b>?</b><span>CI not asked</span></div>'
    classes = {"green": "ok", "red": "bad", "running": "warn", "unknown": "warn"}
    word = {"green": "green", "red": "RED", "running": "running", "unknown": "unknown"}[ci["state"]]
    return (
        f'<div class="stat {classes[ci["state"]]}"><b>{word}</b>'
        f'<span>CI on main · {html.escape(ci["detail"])}</span></div>'
    )


def _inline(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", out)
    return out


def _paragraphs(body: str) -> str:
    return "".join(f"<p>{_inline(' '.join(p.split()))}</p>" for p in body.split("\n\n") if p.strip())


def _milestone_progress(card: Card, cards: list[Card]) -> tuple[int, int]:
    work = [c for c in cards if c.kind == "work" and c.milestone == card.milestone]
    return sum(1 for c in work if c.status == "done"), len(work)


def _work_card(card: Card) -> str:
    meta = [f'<span class="tag {html.escape(card.track.lower())}">{html.escape(card.tag or card.track)}</span>']
    if card.milestone != "pre":
        meta.append(f'<span class="tag">{card.milestone}</span>')
    if card.sha:
        meta.append(f'<span class="sha">{html.escape(card.sha[:7])}</span>')
    if card.status == "blocked":
        meta.append(f'<span class="blocked">blocked: {_inline(card.blocked_by)}</span>')
    if card.status == "doing" and card.started:
        meta.append(f'<span class="date">since {html.escape(card.started)}</span>')
    if card.status == "done" and card.finished:
        meta.append(f'<span class="date">{html.escape(card.finished)}</span>')
    return (
        f'<article class="card {card.status}" id="{html.escape(card.id)}">'
        f"<h3>{_inline(card.title)}</h3>{_paragraphs(card.body)}"
        f'<div class="meta">{"".join(meta)}</div></article>'
    )


def _milestone_card(card: Card, cards: list[Card]) -> str:
    done, total = _milestone_progress(card, cards)
    progress = f"{done}/{total}" if total else "—"
    drive = f'<span class="drive">drive · {_inline(card.drive)}</span>' if card.drive else ""
    return (
        f'<article class="milestone {card.status}" id="{html.escape(card.id)}">'
        f'<div class="mhead"><span class="mid">{card.milestone}</span>'
        f'<span class="tag {html.escape(card.track.lower())}">{html.escape(card.track)}</span>'
        f'<span class="size">{html.escape(card.size)}</span>'
        f'<span class="progress">{progress}</span></div>'
        f"<h3>{_inline(card.title)}</h3>{_paragraphs(card.body)}{drive}</article>"
    )


def _column(css: str, title: str, cards: list[Card], *, group_by_milestone: bool = False) -> str:
    head = (
        f'<div class="col {css}"><div class="col-head"><span class="dot"></span>'
        f'<h2>{title}</h2><span class="n">{len(cards)}</span></div>'
    )
    if not cards:
        return head + '<p class="empty">Nothing here.</p></div>'
    if not group_by_milestone:
        return head + "".join(_work_card(c) for c in cards) + "</div>"
    out = [head]
    for milestone in ("pre", *MILESTONES):
        group = [c for c in cards if c.milestone == milestone]
        if not group:
            continue
        label = "Before the program" if milestone == "pre" else milestone
        out.append(f'<div class="group">{label}</div>')
        out.extend(_work_card(c) for c in group)
    out.append("</div>")
    return "".join(out)


def _sort_key(card: Card) -> tuple[Any, ...]:
    return (card.finished or "", card.order, card.title)


def render(
    cards: list[Card],
    *,
    health: dict[str, Any] | None,
    out: Path = PAGE,
    ci: dict[str, str] | None = None,
) -> str:
    problems = check(cards, verify_git=False)
    if problems:
        raise BoardError("\n".join(problems))
    head = _git("rev-parse", "--short", "HEAD") or "unknown"
    dirty = len([line for line in _git("status", "--porcelain").splitlines() if line.strip()])
    now = dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y")
    work = [c for c in cards if c.kind == "work"]
    milestones = sorted((c for c in cards if c.kind == "milestone"), key=lambda c: c.order)
    decisions = [c for c in cards if c.kind == "decision"]
    doing = sorted((c for c in work if c.status in {"doing", "blocked"}), key=_sort_key)
    nxt = sorted((c for c in work if c.status == "next"), key=_sort_key)
    backlog = sorted((c for c in work if c.status == "backlog"), key=_sort_key)
    done = sorted((c for c in work if c.status == "done"), key=_sort_key, reverse=True)
    program_done = [c for c in done if c.milestone != "pre"]
    earlier = [c for c in done if c.milestone == "pre"]

    if health:
        # Stale means the source differs from what was verified -- judged by
        # content, because the verifying run always precedes the commit that
        # records it, and a commit id would call its own tree stale.
        recorded = health.get("fingerprint")
        stale = recorded is None or recorded != _fingerprint(_working_tree())
        failed = int(health.get("failed", 0)) or (0 if health.get("pytest_ok") else 1)
        tests_class = "bad" if failed else ("warn" if stale else "ok")
        tests_label = (
            f"tests passed, {failed} FAILED" if failed else "tests passed"
        ) + f" at {health['commit']}{' · stale' if stale else ''}"
        gates_ok = health["ruff_ok"] and health["typecheck_ok"] and not failed
        tests = (
            f'<div class="stat {tests_class}"><b>{health["passed"]}</b><span>{tests_label}</span></div>'
            f'<div class="stat"><b>{health["skipped"]}</b><span>skipped (live)</span></div>'
            f'<div class="stat {"ok" if gates_ok else "bad"}"><b>{"green" if gates_ok else "RED"}</b>'
            f"<span>ruff · pytest · tsc, {html.escape(health['verified_at'])}</span></div>"
        )
    else:
        tests = '<div class="stat warn"><b>?</b><span>never verified — run tools/board.py verify</span></div>'
    strip = (
        f'<div class="health">{tests}'
        f'<div class="stat ok"><b>{_ci_python_matrix()}</b><span>python in CI</span></div>'
        + _ci_stat(ci)
        + f'<div class="stat"><b>{len(program_done)}/{len(work) - len(earlier)}</b><span>program cards shipped</span></div>'
        f'<div class="stat"><b>{len(doing)}</b><span>in progress</span></div>'
        f'<div class="stat {"ok" if dirty == 0 else "warn"}"><b>{dirty}</b><span>uncommitted files at render</span></div>'
        f'<div class="stat"><b>{head}</b><span>HEAD</span></div></div>'
    )

    lanes = []
    for release in RELEASES:
        lane = [c for c in milestones if c.release == release]
        shipped = sum(1 for c in lane if c.status == "done")
        lanes.append(
            f'<section class="lane"><div class="lane-head"><h2>Release {release}</h2>'
            f'<span class="n">{shipped}/{len(lane)} milestones</span></div>'
            f'<div class="lane-cards">{"".join(_milestone_card(c, cards) for c in lane)}</div></section>'
        )

    shipped_col = _column("done", "Shipped", program_done, group_by_milestone=True)
    if earlier:
        shipped_col = shipped_col[: -len("</div>")] + (
            f'<details class="earlier"><summary>Before the program · {len(earlier)} shipped</summary>'
            + "".join(_work_card(c) for c in earlier)
            + "</details></div>"
        )
    board = (
        '<div class="board">'
        + _column("doing", "In progress", doing)
        + _column("next", "Next", nxt, group_by_milestone=True)
        + _column("later", "Backlog", backlog, group_by_milestone=True)
        + shipped_col
        + "</div>"
    )
    decisions_html = "".join(
        f'<div class="decision" id="{html.escape(c.id)}"><span class="verdict">{html.escape(c.verdict)}</span>'
        f"<h3>{_inline(c.title)}</h3>{_paragraphs(c.body)}</div>"
        for c in decisions
    )
    page = TEMPLATE.format(
        now=now,
        head=head,
        strip=strip,
        lanes="".join(lanes),
        board=board,
        decisions=decisions_html,
        count=len(cards),
    )
    out.write_text(page, encoding="utf-8")
    return page


TEMPLATE = """<title>RICH Delivery Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap">
<!-- Rendered by tools/board.py from docs/board/cards/*.md — do not edit by hand. -->
<style>
  :root {{
    --bg: #07080c; --bg-glow: rgba(124,108,255,.09); --bg-glow-2: rgba(45,212,191,.045);
    --surface: #0e1017; --surface-2: #14171f;
    --rule: rgba(255,255,255,.085); --rule-strong: rgba(255,255,255,.16);
    --ink: #e7eaf1; --ink-dim: #98a1b2; --ink-faint: #5a6273;
    --accent: #7c6cff; --done: #2fd3a0; --doing: #f0b429; --next: #5b7cfa; --later: #8a93a3;
    --blocked: #f06a6a; --decided: #2dd4bf;
    --sans: 'Geist', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
    --mono: 'Geist Mono', ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  }}
  @media (prefers-color-scheme: light) {{ :root:not([data-theme="dark"]) {{
    --bg: #f5f6fa; --bg-glow: rgba(124,108,255,.07); --bg-glow-2: rgba(45,212,191,.05);
    --surface: #fff; --surface-2: #f0f1f6; --rule: rgba(9,11,18,.11); --rule-strong: rgba(9,11,18,.2);
    --ink: #12141b; --ink-dim: #4e5668; --ink-faint: #838c9e; --accent: #5847d8; --done: #0f9d72;
    --doing: #a9761a; --next: #3a56c9; --later: #6d7688; --blocked: #c23b3b; --decided: #0e9c92; }} }}
  :root[data-theme="light"] {{
    --bg: #f5f6fa; --bg-glow: rgba(124,108,255,.07); --bg-glow-2: rgba(45,212,191,.05);
    --surface: #fff; --surface-2: #f0f1f6; --rule: rgba(9,11,18,.11); --rule-strong: rgba(9,11,18,.2);
    --ink: #12141b; --ink-dim: #4e5668; --ink-faint: #838c9e; --accent: #5847d8; --done: #0f9d72;
    --doing: #a9761a; --next: #3a56c9; --later: #6d7688; --blocked: #c23b3b; --decided: #0e9c92; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; color: var(--ink); font-family: var(--sans); font-size: 15px; line-height: 1.55;
    -webkit-font-smoothing: antialiased;
    background: radial-gradient(1100px 620px at 50% -10%, var(--bg-glow), transparent 68%),
      radial-gradient(800px 560px at 100% 4%, var(--bg-glow-2), transparent 60%), var(--bg); }}
  .page {{ max-width: 1560px; margin: 0 auto; padding: 48px 28px 90px; }}
  header.top {{ display: flex; flex-direction: column; gap: 12px; margin-bottom: 26px; }}
  .eyebrow {{ font-family: var(--mono); font-size: 11px; font-weight: 500; letter-spacing: .14em;
    text-transform: uppercase; color: var(--ink-faint); display: flex; flex-wrap: wrap; gap: 8px 18px; }}
  .eyebrow b {{ color: var(--accent); font-weight: 500; }}
  h1 {{ margin: 0; font-size: clamp(28px, 4vw, 40px); font-weight: 600; letter-spacing: -.026em; }}
  .sub {{ margin: 0; max-width: 78ch; color: var(--ink-dim); font-size: 16px; }}
  .sub strong {{ color: var(--ink); font-weight: 500; }}
  .health {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0 4px; padding: 14px 16px;
    border: 1px solid var(--rule-strong); border-radius: 4px; background: var(--surface); }}
  .stat {{ display: flex; flex-direction: column; gap: 1px; padding-right: 26px; }}
  .stat b {{ font-family: var(--mono); font-size: 17px; font-variant-numeric: tabular-nums; }}
  .stat span {{ font-family: var(--mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint); }}
  .stat.ok b {{ color: var(--done); }} .stat.warn b {{ color: var(--doing); }} .stat.bad b {{ color: var(--blocked); }}
  .lane {{ margin: 26px 0 0; }}
  .lane-head {{ display: flex; align-items: baseline; gap: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--rule-strong); }}
  .lane-head h2 {{ margin: 0; font-size: 15px; font-weight: 600; }}
  .lane-head .n, .col-head .n {{ margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--ink-faint); font-variant-numeric: tabular-nums; }}
  .lane-cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; margin-top: 12px; }}
  .milestone {{ border: 1px solid var(--rule); border-top: 3px solid var(--later); border-radius: 3px;
    background: var(--surface); padding: 10px 12px; display: flex; flex-direction: column; gap: 5px; }}
  .milestone.done {{ border-top-color: var(--done); }} .milestone.doing {{ border-top-color: var(--doing); }}
  .milestone.next {{ border-top-color: var(--next); }} .milestone.blocked {{ border-top-color: var(--blocked); }}
  .mhead {{ display: flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: 10px; }}
  .mid {{ font-weight: 600; color: var(--ink); }} .size {{ color: var(--ink-faint); }}
  .progress {{ margin-left: auto; color: var(--ink-dim); font-variant-numeric: tabular-nums; }}
  .milestone h3 {{ margin: 0; font-size: 13px; font-weight: 550; line-height: 1.35; }}
  .milestone p {{ margin: 0; font-size: 12px; color: var(--ink-dim); line-height: 1.45; }}
  .drive {{ font-family: var(--mono); font-size: 10px; color: var(--decided); }}
  .board {{ display: grid; grid-template-columns: repeat(4, minmax(268px, 1fr)); gap: 18px; align-items: start; margin-top: 34px; }}
  .col {{ display: flex; flex-direction: column; gap: 10px; min-width: 268px; }}
  .col-head {{ display: flex; align-items: center; gap: 9px; padding-bottom: 10px; border-bottom: 2px solid var(--lane); }}
  .col-head h2 {{ margin: 0; font-size: 14px; font-weight: 600; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--lane); }}
  .col.done {{ --lane: var(--done); }} .col.doing {{ --lane: var(--doing); }} .col.next {{ --lane: var(--next); }} .col.later {{ --lane: var(--later); }}
  .group {{ font-family: var(--mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-faint); margin-top: 6px; }}
  .empty {{ margin: 0; font-size: 12.5px; color: var(--ink-faint); }}
  .card {{ border: 1px solid var(--rule); border-left: 2px solid var(--lane); border-radius: 3px; background: var(--surface);
    padding: 11px 13px; display: flex; flex-direction: column; gap: 6px; }}
  .card.blocked {{ border-left-color: var(--blocked); }}
  .card h3 {{ margin: 0; font-size: 13.5px; font-weight: 550; line-height: 1.4; }}
  .card p {{ margin: 0; font-size: 12.5px; color: var(--ink-dim); line-height: 1.5; }}
  .meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-family: var(--mono); font-size: 10px; }}
  .tag {{ letter-spacing: .07em; text-transform: uppercase; padding: 1.5px 6px; border-radius: 2px;
    border: 1px solid var(--rule-strong); color: var(--ink-faint); background: var(--surface-2); }}
  .tag.e {{ color: var(--accent); border-color: var(--accent); }} .tag.p {{ color: var(--next); border-color: var(--next); }}
  .tag.k {{ color: var(--decided); border-color: var(--decided); }} .tag.core, .tag.docs {{ color: var(--decided); border-color: var(--decided); }}
  .tag.release {{ color: var(--doing); border-color: var(--doing); }}
  .sha {{ color: var(--done); }} .date {{ color: var(--ink-faint); }} .blocked {{ color: var(--blocked); }}
  code {{ font-family: var(--mono); font-size: .87em; background: var(--surface-2); border: 1px solid var(--rule); border-radius: 2px; padding: .05em .3em; }}
  details.earlier {{ margin-top: 8px; }} details.earlier summary {{ cursor: pointer; font-family: var(--mono); font-size: 11px; color: var(--ink-faint); margin-bottom: 8px; }}
  details.earlier .card {{ margin-bottom: 10px; }}
  .decisions {{ margin-top: 46px; }}
  .decisions h2 {{ margin: 0 0 4px; font-size: 17px; font-weight: 600; padding-bottom: 11px; border-bottom: 1px solid var(--rule-strong); }}
  .dgrid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; margin-top: 18px; }}
  .decision {{ border: 1px solid var(--rule); border-radius: 3px; background: var(--surface); padding: 15px 17px; display: flex; flex-direction: column; gap: 6px; }}
  .decision h3 {{ margin: 0; font-size: 14px; font-weight: 600; }} .decision p {{ margin: 0; font-size: 12.75px; color: var(--ink-dim); }}
  .verdict {{ font-family: var(--mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--decided); }}
  footer {{ margin-top: 50px; padding-top: 18px; border-top: 1px solid var(--rule-strong); font-family: var(--mono); font-size: 11px; color: var(--ink-faint); display: flex; flex-wrap: wrap; gap: 6px 22px; }}
  @media (max-width: 980px) {{ .board {{ grid-template-columns: 1fr; }} .col {{ min-width: 0; }} }}
</style>
<div class="page">
  <header class="top">
    <div class="eyebrow"><span>Delivery board</span><span><b>RICH</b> — intent-to-verified-software compiler</span><span>rendered {now} · HEAD {head}</span></div>
    <h1>RICH Delivery Board</h1>
    <p class="sub">The tracker for <strong>one program, three releases</strong>: 2.0 builds real software, 2.1 proves more and ships to production, 2.2 compiles any software. Cards are files under <code>docs/board/cards</code>; this page is rendered from them and the numbers above are measured, not typed. The definition of done is one customer scenario, in <code>docs/program.md</code>.</p>
    {strip}
  </header>
  {lanes}
  {board}
  <section class="decisions"><h2>Decided, and why</h2><div class="dgrid">{decisions}</div></section>
  <footer><span>RICH delivery board</span><span>{count} cards</span><span>rendered {now}</span><span>HEAD {head}</span></footer>
</div>
"""


# ── editing ────────────────────────────────────────────────────────────────


def _today() -> str:
    return dt.date.today().isoformat()


def _write_front_matter(path: Path, updates: dict[str, Any]) -> None:
    meta, body = _split(path.read_text(encoding="utf-8"), path)
    for key, value in updates.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
    path.write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")


def move(card_id: str, status: str, *, sha: str = "", why: str = "") -> None:
    path = CARDS / f"{card_id}.md"
    if not path.exists():
        raise BoardError(f"no card {card_id!r}")
    if status not in STATUSES or status == "decision":
        raise BoardError(f"cannot move to {status!r}")
    updates: dict[str, Any] = {"status": status}
    if status == "doing":
        updates["started"] = _today()
        updates["blocked_by"] = None
    if status == "done":
        if sha:
            updates["sha"] = sha
        updates["finished"] = _today()
        updates["blocked_by"] = None
    if status == "blocked":
        if not why:
            raise BoardError("blocked needs --why")
        updates["blocked_by"] = why
    if status in {"backlog", "next"}:
        updates["blocked_by"] = None
    _write_front_matter(path, updates)


def new(card_id: str, *, title: str, milestone: str, track: str, status: str = "next", body: str = "", tag: str = "", sha: str = "") -> Path:
    path = CARDS / f"{card_id}.md"
    if path.exists():
        raise BoardError(f"card {card_id!r} already exists")
    meta: dict[str, Any] = {"id": card_id, "title": title, "status": status, "track": track, "milestone": milestone}
    if tag:
        meta["tag"] = tag
    if status == "doing":
        meta["started"] = _today()
    if status == "done":
        # A card born done -- work that shipped before its card was written --
        # still needs the commit that shipped it and the day it did.
        if not sha:
            raise BoardError("a card born done needs --sha")
        meta["sha"] = sha
        meta["finished"] = _today()
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
    path.write_text(f"---\n{front}\n---\n\n{body.strip()}\n", encoding="utf-8")
    return path


# ── cli ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("render")
    sub.add_parser("verify")
    mv = sub.add_parser("move")
    mv.add_argument("card_id")
    mv.add_argument("status", choices=[s for s in STATUSES if s != "decision"])
    mv.add_argument("--sha", default="")
    mv.add_argument("--why", default="")
    nw = sub.add_parser("new")
    nw.add_argument("card_id")
    nw.add_argument("--title", required=True)
    nw.add_argument("--milestone", required=True)
    nw.add_argument("--track", required=True, choices=TRACKS)
    nw.add_argument("--status", default="next", choices=[s for s in STATUSES if s != "decision"])
    nw.add_argument("--body", default="")
    nw.add_argument("--tag", default="")
    nw.add_argument("--sha", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            problems = check(load_cards())
            for problem in problems:
                print(problem, file=sys.stderr)
            print(f"{len(load_cards())} cards, {len(problems)} problems")
            return 1 if problems else 0
        if args.command == "move":
            move(args.card_id, args.status, sha=args.sha, why=args.why)
        if args.command == "new":
            new(args.card_id, title=args.title, milestone=args.milestone, track=args.track, status=args.status, body=args.body, tag=args.tag, sha=args.sha)
        health = None
        if args.command == "verify":
            health = verify()
            print(json.dumps(health, indent=2))
        elif HEALTH.exists():
            health = json.loads(HEALTH.read_text(encoding="utf-8"))
        render(load_cards(), health=health, ci=ci_status())
        print(f"rendered {PAGE.relative_to(ROOT)}")
        return 0
    except BoardError as exc:
        print(f"board: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
