---
id: m1-project-state
title: One call restores a project
status: done
track: P
milestone: M1
tag: api
started: '2026-08-29'
sha: 01f4711
finished: '2026-08-29'
---

Loading an existing project nulled its spec, architecture and run and stranded it. `GET /v1/projects/{id}/state` returns the latest product-spec revision and approval, the latest architecture revision and approval, the runs and the previews in one answer, built from store methods that already exist; the canvas restores from it.
