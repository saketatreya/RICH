---
id: m3-legible-timeline
title: The run reads in plain language
status: done
track: P
milestone: M3
tag: api
sha: 60f8a38
finished: '2026-08-29'
---

`GET /v1/runs/{id}/timeline` serves the same lines `rich logs --follow` prints, from `runlog.format_event`; the canvas shows them instead of raw JSON payloads, which move behind a disclosure.
