---
id: m11-wheel-bundles-canvas
title: The wheel carries the canvas
status: done
track: E
milestone: M11
sha: 5cd1a3c
finished: '2026-08-29'
---

tools/build_wheel.py: canvas built, packaged as richbuild/canvas data, checkout left clean. canvas_origin() serves the checkout's build when present, else the bundled copy; doctor and the serve banner say which. Proven locally and in CI: an empty venv installs the wheel and serves the canvas.
