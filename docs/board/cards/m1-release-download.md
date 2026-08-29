---
id: m1-release-download
title: Download the release ZIP
status: next
track: P
milestone: M1
tag: api
---

`source:release-snapshot` already exists as a content-addressed artifact. `GET /v1/runs/{id}/release` streams it with a Content-Disposition — no base64, no 4 MiB cap — and a succeeded run gets a Download button.
