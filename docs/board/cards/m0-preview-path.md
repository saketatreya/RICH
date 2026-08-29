---
id: m0-preview-path
title: The canvas preview request sends the scaffold destination
status: done
track: P
tag: canvas
milestone: M0
order: 12
sha: 56117ce
finished: '2026-08-29'
---

It sent the workspace-relative form string; the API resolved it against the server's cwd
and refused every request. Also deploy the preview just approved, not `latest.id`.
