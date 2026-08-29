---
id: m3-owner-retry
title: An acceptance failure reopens the task that owns the page, not the root
status: done
track: E
milestone: M3
sha: 17fb718
finished: '2026-08-29'
---

Finding from the first live build: three root attempts (~$0.35, 7 min) regenerated a composition shim while the failing page belonged to web. The pack names the pages a scenario opens, evidence names their owners, the scheduler reopens the owner and supersedes everything downstream; exhausted owners withhold the retry. Live proof pending: the next M3 drive after the subscription limit resets.
