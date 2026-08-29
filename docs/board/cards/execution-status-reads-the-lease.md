---
id: execution-status-reads-the-lease
title: "Execution status reads the lease"
status: done
track: E
tag: engine
milestone: pre
order: 40
sha: 6dbd6a1
---

It reported an in-memory set while the guarantee lived in SQLite, so a run executing
elsewhere read as idle.
