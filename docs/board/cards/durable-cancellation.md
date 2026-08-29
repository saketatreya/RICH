---
id: durable-cancellation
title: "Durable cancellation"
status: done
track: E
tag: engine
milestone: pre
order: 32
sha: cc793de
---

The engine always checked a token; nothing could set one. Now reachable from any
surface, because the process that starts a run is rarely the one asked to stop it.
