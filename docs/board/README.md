# The delivery board

`docs/board.html` is **rendered**, never edited. The tracker is this directory:
one Markdown file per card under `cards/`, YAML front matter plus a body. Two tracks
in two worktrees move different cards by editing different files, so the board never
becomes a merge conflict.

```bash
python tools/board.py check     # validate; the suite runs this too (tests/test_board.py)
python tools/board.py render    # write docs/board.html from the cards
python tools/board.py verify    # ruff + pytest + typecheck → health.json, then render
python tools/board.py move m1-project-state doing
python tools/board.py move m1-project-state done --sha 1a2b3c4
python tools/board.py move m7-spike blocked --why "PGlite exceeds RLIMIT_AS"
python tools/board.py new m1-release-download --title "Download the release ZIP" \
    --milestone M1 --track P --status next --body "One route, one button."
```

## A card

```yaml
---
id: m1-project-state            # == filename stem
title: "One call restores a project"
status: next                    # backlog · next · doing · blocked · done
track: P                        # P product · E engine · K packs · core · docs · release
milestone: M1                   # M0–M22, or pre for work shipped before the program
tag: canvas                     # optional label shown on the card
sha: 1a2b3c4                    # required once done — the commit that shipped it
started: 2026-08-30             # required while doing
finished: 2026-08-31            # required once done
blocked_by: "…"                 # required while blocked
---

What was wrong, what changed, and what it proves. One or two paragraphs.
```

Milestone cards (`kind: milestone`) carry `release`, `size` and `drive` (the step of the
customer scenario in `docs/program.md` they move); their progress is computed from
their work cards. Decision cards (`kind: decision`) carry a `verdict`.

## The rules the suite enforces

- every milestone M0–M22 has exactly one card;
- a done work card names a commit that exists in this repository;
- a card in progress has a start date; a blocked card says what blocks it;
- no unknown fields, no unknown statuses, no card without a milestone;
- the rendered page contains every card.

The health strip is measured: `verify` records the test count, ruff and typecheck
results **at a commit**, and the page says so — and says *stale* when HEAD has moved
since. Run `verify` before pushing a milestone; run `render` after any card edit.
