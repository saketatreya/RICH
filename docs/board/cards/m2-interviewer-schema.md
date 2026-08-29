---
id: m2-interviewer-schema
title: An invalid oracle step is unrepresentable
status: done
track: P
milestone: M2
tag: engine
started: '2026-08-29'
sha: 506e572
finished: '2026-08-29'
---

`interviewer.py`, modelled on `architect.py`: a response schema whose oracle items are one branch per action with exactly that action's required fields, locator kinds and ARIA roles as enums, requirement references as keys the same answer declares, and `answers_from_keys` deriving the ids a human never sees. `AdaptiveInterview.compile` stays the validator.
