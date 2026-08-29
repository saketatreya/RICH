---
id: every-claimed-python-version-tested
title: "Every claimed Python version tested"
status: done
track: release
tag: release
milestone: pre
order: 44
sha: 3c968be
---

`requires-python` said 3.10 while CI ran two versions. All five now run, each verified
locally first — which caught a dependency I had wrongly dropped.
