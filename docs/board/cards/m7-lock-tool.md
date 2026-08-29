---
id: m7-lock-tool
title: Lock regenerator, domain reaches db, pglite as direct deps
status: done
track: E
milestone: M7
sha: a234a08
finished: '2026-08-29'
---

On engine/m7 (not merged): tools/refresh_nextjs_lock.py regenerates _nextjs_lock.py under the pinned pnpm; domain → db edge; @electric-sql/pglite 0.5.8 and postgres as direct apps/web deps; a lock-validity test over all rendered variants. Finding: five scaffold variants exist, not eight.
