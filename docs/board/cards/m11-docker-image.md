---
id: m11-docker-image
title: A container image with Bubblewrap and the pinned toolchain
status: doing
track: E
milestone: M11
started: '2026-08-29'
---

Dockerfile written (toolchain / builder / runtime), README run line, CI image job that runs the container and reads rich doctor under the default seccomp profile and unconfined. No docker on this host: the proof is CI's, pending its first run.
