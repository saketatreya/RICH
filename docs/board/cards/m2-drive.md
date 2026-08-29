---
id: m2-drive
title: 'Drive: describe it in prose, approve a readable spec'
status: done
track: P
milestone: M2
tag: drive
sha: 5bfc052
finished: '2026-08-29'
---

Live: one paragraph on the claude-code route → a spec that compiles and whose oracle assembles, no human edits. Browser: type prose, answer a question, see scenarios as sentences, edit a step with dropdowns, approve.

Held both ways. Live: one paragraph on the claude-code route became a specification that compiles, 75 s, no human edits (`tests/test_interviewer_live.py`). Browser, against `rich serve --route none`: seven steps, every one held (`npm --prefix web run drive:m2`).
