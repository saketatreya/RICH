---
id: m1-release-download
title: Download the release ZIP
status: doing
track: P
milestone: M1
tag: api
started: '2026-08-29'
---

`source:release-snapshot` already exists as a content-addressed artifact. `GET /v1/runs/{id}/release` streams it with a Content-Disposition — no base64, no 4 MiB cap — and a succeeded run gets a Download button.
