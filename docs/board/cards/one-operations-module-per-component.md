---
id: one-operations-module-per-component
title: "One operations module per component"
status: done
track: E
tag: engine
milestone: pre
order: 11
sha: 82b96cd
---

Every component's operations lived in one shared module owned by domain — so domain
implemented `accept_req_a11y`, an operation belonging to app. Change locality was real
at the contract level and worth nothing at the implementation level.
