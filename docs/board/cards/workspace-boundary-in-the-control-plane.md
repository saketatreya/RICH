---
id: workspace-boundary-in-the-control-plane
title: "Workspace boundary in the control plane"
status: done
track: E
tag: engine
milestone: pre
order: 39
sha: 2ae0d95
---

It guarded only the HTTP entry point. Now every caller crosses it — and the CLI opts out
explicitly, with a reason.
