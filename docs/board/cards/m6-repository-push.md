---
id: m6-repository-push
title: Push the verified snapshot to a repository
status: next
track: E
milestone: M6
tag: engine
---

`POST /v1/runs/{id}/repository-pushes`: extract the immutable release snapshot, commit with the run id and digest, push to a new or named GitHub repository through the same closed secret-handle map as the preview. Digest-bound: refuses if the live tree drifted.
