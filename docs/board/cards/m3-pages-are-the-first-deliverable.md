---
id: m3-pages-are-the-first-deliverable
title: The scaffolded page says who replaces it; the pages are deliverable one; the prompt budget is 48 KB
status: done
track: E
milestone: M3
sha: 495a4c3
finished: '2026-08-30'
---

Second live run: the reopen worked and both new attempts died on PromptLimitError (22 KB base prompt for the component owning apps/web, 29 KB with the failure; ceiling 24 KB). Raised to 48 KB, guard at four bytes per token. The worker, told the page, still wrote only operations.ts: the placeholder now says in its first lines that the owning component replaces it, and the prompt opens with the pages to rewrite as deliverable one.
