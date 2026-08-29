"""The delivery board is part of the tree, so its consistency is part of the suite.

A card that says "done" without the commit that shipped it, a milestone with no card,
or a card in progress with no start date is a board that misreports progress -- which
is worse than no board. These tests hold the checked-in cards to the rules in
``tools/board.py`` and prove the renderer shows every card.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _board():
    spec = importlib.util.spec_from_file_location("board", ROOT / "tools" / "board.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before execution: a dataclass under ``from __future__ import
    # annotations`` resolves its field types through ``sys.modules``.
    sys.modules["board"] = module
    spec.loader.exec_module(module)
    return module


board = _board()


def _write(directory: pathlib.Path, name: str, front: str, body: str = "Body.") -> None:
    (directory / f"{name}.md").write_text(f"---\n{front.strip()}\n---\n\n{body}\n", encoding="utf-8")


def _milestones(directory: pathlib.Path) -> None:
    for index, name in enumerate(board.MILESTONES):
        _write(
            directory,
            f"{name.lower()}-milestone",
            f'id: {name.lower()}-milestone\ntitle: "{name}"\nkind: milestone\nstatus: backlog\n'
            f'track: E\nmilestone: {name}\nrelease: "2.0"\nsize: M\norder: {index}',
        )


def test_the_checked_in_board_is_consistent():
    problems = board.check(board.load_cards(), verify_git=True)
    assert problems == [], "\n".join(problems)


def test_the_page_is_rendered_from_the_cards_and_never_edited_by_hand():
    page = (ROOT / "docs" / "board.html").read_text(encoding="utf-8")
    assert "Rendered by tools/board.py" in page
    for card in board.load_cards():
        assert card.id in page, f"card {card.id} is missing from the rendered page"


def test_render_shows_every_card(tmp_path: pathlib.Path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _milestones(cards_dir)
    _write(cards_dir, "shipped", 'id: shipped\ntitle: "Shipped thing"\nstatus: done\ntrack: E\nmilestone: M1\nsha: a4732c6\nfinished: 2026-08-29')
    _write(cards_dir, "moving", 'id: moving\ntitle: "Moving thing"\nstatus: doing\ntrack: P\nmilestone: M1\nstarted: 2026-08-29')
    _write(cards_dir, "stuck", 'id: stuck\ntitle: "Stuck thing"\nstatus: blocked\ntrack: P\nmilestone: M2\nblocked_by: "a quota"')
    cards = board.load_cards(cards_dir)
    page = board.render(cards, health=None, out=tmp_path / "board.html")
    for title in ("Shipped thing", "Moving thing", "Stuck thing"):
        assert title in page
    assert "blocked: a quota" in page
    assert "never verified" in page


def test_a_done_card_needs_the_commit_that_shipped_it(tmp_path: pathlib.Path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _milestones(cards_dir)
    _write(cards_dir, "claimed", 'id: claimed\ntitle: "Claimed"\nstatus: done\ntrack: E\nmilestone: M1\nfinished: 2026-08-29')
    problems = board.check(board.load_cards(cards_dir), verify_git=False)
    assert any("needs the commit" in p for p in problems)


def test_a_card_in_progress_needs_a_start_and_a_blocked_card_a_reason(tmp_path: pathlib.Path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _milestones(cards_dir)
    _write(cards_dir, "a", 'id: a\ntitle: "A"\nstatus: doing\ntrack: E\nmilestone: M1')
    _write(cards_dir, "b", 'id: b\ntitle: "B"\nstatus: blocked\ntrack: E\nmilestone: M1')
    problems = board.check(board.load_cards(cards_dir), verify_git=False)
    assert any("started date" in p for p in problems)
    assert any("what blocks it" in p for p in problems)


def test_every_milestone_has_exactly_one_card(tmp_path: pathlib.Path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _milestones(cards_dir)
    (cards_dir / "m7-milestone.md").unlink()
    problems = board.check(board.load_cards(cards_dir), verify_git=False)
    assert any("M7 has 0 cards" in p for p in problems)


def test_milestone_progress_is_computed_from_its_work_cards(tmp_path: pathlib.Path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _milestones(cards_dir)
    _write(cards_dir, "one", 'id: one\ntitle: "One"\nstatus: done\ntrack: E\nmilestone: M3\nsha: a4732c6\nfinished: 2026-08-29')
    _write(cards_dir, "two", 'id: two\ntitle: "Two"\nstatus: next\ntrack: E\nmilestone: M3')
    cards = board.load_cards(cards_dir)
    milestone = next(c for c in cards if c.kind == "milestone" and c.milestone == "M3")
    assert board._milestone_progress(milestone, cards) == (1, 2)


def test_move_records_dates_and_the_commit(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    monkeypatch.setattr(board, "CARDS", cards_dir)
    board.new("thing", title="Thing", milestone="M1", track="P", status="next")
    board.move("thing", "doing")
    card = board.load_cards(cards_dir)[0]
    assert card.status == "doing" and card.started
    board.move("thing", "done", sha="a4732c6")
    card = board.load_cards(cards_dir)[0]
    assert card.status == "done" and card.sha == "a4732c6" and card.finished
    with pytest.raises(board.BoardError):
        board.move("thing", "blocked")
