---
id: m3-shared-dependency-cache
title: A second build must not re-download the world
status: done
track: E
milestone: M3
tag: engine
sha: 8b884bc
finished: '2026-08-29'
---

The pnpm store and Playwright browsers live inside each workspace, so every run pays ~11 minutes and ~2 GiB. A shared, integrity-verified cache under the state dir, mounted read-only into gates after `--verify-store-integrity`; fail closed on any mismatch. Also what per-task workspaces need in M14.
