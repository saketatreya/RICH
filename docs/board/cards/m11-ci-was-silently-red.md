---
id: m11-ci-was-silently-red
title: 'CI had failed every run with no jobs: a job-level env named the runner context'
status: done
track: E
milestone: M11
sha: 3f2c4b7
finished: '2026-08-29'
---

Since the package job landed, GitHub rejected the workflow before creating a job; the API showed only 'failure'. Found with actionlint (runner context not allowed in job env). The board's locally measured strip said green throughout: it now needs CI's verdict for HEAD.
