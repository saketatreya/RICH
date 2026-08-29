---
id: m3-scenario-pages-in-prompt
title: A scenario names the pages its steps run on; prior-failure feedback is bounded
status: done
track: E
milestone: M3
sha: 355de5b
finished: '2026-08-30'
---

The second live build failed like the first: the web worker wrote operations.ts and never touched the page the scenarios open, because nothing named it. Every scenario now carries the page files the pack says it opens, for the task that owns them. The reopen then died on PromptLimitError: prior failures are cut to 3000 bytes each, named steps first.
