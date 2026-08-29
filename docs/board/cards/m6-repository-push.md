---
id: m6-repository-push
title: Push the verified snapshot to a repository
status: done
track: E
milestone: M6
tag: engine
sha: 53a9195
finished: '2026-08-29'
---

`POST /v1/runs/{id}/repository-pushes`: extract the immutable release snapshot, commit with the run id and digest, push to a new or named GitHub repository through the same closed secret-handle map as the preview. Digest-bound: refuses if the live tree drifted.
