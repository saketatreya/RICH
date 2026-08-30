---
id: m3-scheduler-waits-for-its-own-clock
title: A task waiting out its retry backoff is waited for, not stranded
status: done
track: P
milestone: M3
sha: eb91e7a
finished: '2026-08-30'
---

Found while designing the quota fix and a prerequisite for it. `_ready_tasks` filters out a task whose `retry_not_before` has not arrived; the loop then found nothing ready and nothing running, called that a corrupted DAG — the comment said so — and failed the run milliseconds after scheduling the very retry it refused to wait for. Dormant only because every configured `retry_backoff_seconds` was 0.0. One function now answers both what can start and when the earliest waiting task may start, so the two predicates cannot drift apart again; a restored deadline is clamped by the backoff that set it, because it is persisted as a wall-clock epoch and wall clocks move.
