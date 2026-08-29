---
id: m0-duplicate-helpers
title: "Duplicated helpers get one definition"
status: doing
track: E
tag: codebase
milestone: M0
order: 3
started: 2026-08-29
---

`_all_events`, `_fsync_directory`, vestigial `_is_owned`, the two providers' identical
HTTP transport, and `_optional_string` with opposite contracts under one name. Rename
the store's `Artifact` and `SCHEMA_VERSION` collisions.
