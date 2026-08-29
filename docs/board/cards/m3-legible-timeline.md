---
id: m3-legible-timeline
title: The run reads in plain language
status: next
track: P
milestone: M3
tag: api
---

`GET /v1/runs/{id}/timeline` serves the same lines `rich logs --follow` prints, from `runlog.format_event`; the canvas shows them instead of raw JSON payloads, which move behind a disclosure.
