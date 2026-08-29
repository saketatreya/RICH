---
id: m1-session-is-a-pointer
title: The session is a pointer, not a copy
status: done
track: P
milestone: M1
tag: canvas
started: '2026-08-29'
sha: 34916b6
finished: '2026-08-29'
---

localStorage held full copies of server objects that went stale the moment anything else touched the project. It keeps the project id and UI preferences; everything else comes from the state call. Actor identity becomes a header chip instead of a field under the Inspector.
