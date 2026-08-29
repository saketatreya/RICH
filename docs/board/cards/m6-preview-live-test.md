---
id: m6-preview-live-test
title: 'Opt-in live preview test: deploy, answer, tear down'
status: blocked
blocked_by: "no NEON_API_TOKEN, VERCEL_TOKEN or RICH_NEON_PROJECT_ID on this host; only the owner can supply them"
track: P
milestone: M6
---

tests/test_preview_live.py is written and self-skips. It has not run: this host has no NEON_API_TOKEN, VERCEL_TOKEN or RICH_NEON_PROJECT_ID. The M6 drive's preview step skips for the same reason. Unblocks the moment the credentials exist where the tests run.
