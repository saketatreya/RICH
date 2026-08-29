---
id: m0-packaging-find
title: "A wheel can import richbuild"
status: doing
track: E
tag: release
milestone: M0
order: 1
started: 2026-08-29
---

`pyproject.toml` listed packages by hand and omitted `richbuild.models`; a built wheel
could not import the package. Switch to `packages.find`, and make CI build, install and
import the wheel so it cannot regress.
