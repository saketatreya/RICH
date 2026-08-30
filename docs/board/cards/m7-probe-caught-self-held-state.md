---
id: m7-probe-caught-self-held-state
title: State kept in the server process is not persistence, and the probe says whose fault it is
status: done
track: E
milestone: M7
sha: 1f5ea9a
finished: '2026-08-30'
---

M7's first live proof failed and the failure was the invariant working. Told only 'never import a database driver', the web worker kept the todos in a `globalThis` array; `next start` is one long-lived process, so it survived `page.reload()`, every browser step passed, and the trusted probe read a database whose tables were all empty. Three defects, all fixed: the prompt forbade the mechanism and not the behaviour; a probe failure named no owner, so the root was regenerated three times while the component holding the array was never reopened; and the reopened owner would have been shown 'acceptance failed on pages you own' beside a browser log of ticks, so it now reads the probe's verdict and table counts instead.
