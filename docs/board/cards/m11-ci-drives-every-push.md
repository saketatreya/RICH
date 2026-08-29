---
id: m11-ci-drives-every-push
title: CI runs the canvas test and drives M1 and M6 in a real browser on every push
status: done
track: E
milestone: M11
sha: 25e7f5b
finished: '2026-08-29'
---

vitest holds describeStep to the shared sentence fixture; a drive job runs web/drive/m1-return.mjs and m6-release.mjs in Chromium against rich serve --route none (40 s), uploading screenshots on failure. Whole workflow green in under a minute per job.
