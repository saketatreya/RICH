---
id: m1-interview-draft-server-side
title: The interview draft lives on the server
status: next
track: P
milestone: M1
tag: store
---

A reload discarded every word typed into the interview. One mutable draft per project with its own optimistic counter (`interview_drafts`, store migration 12), `GET/PUT /v1/projects/{id}/interview` — not a revision kind, because a revision would race the spec submission's expected revision.
