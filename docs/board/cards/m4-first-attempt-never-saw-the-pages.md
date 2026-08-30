---
id: m4-first-attempt-never-saw-the-pages
title: The first attempt was never told which pages the browser opens
status: done
track: P
milestone: M4
sha: 04d7755
finished: '2026-08-30'
---

Found by a sixty-agent adversarial review of the day's work, and it reframes several earlier observations. `CodingWorker` passed `scenario_pages` only to its retry branch, so the prompt actually sent on every first attempt carried `pages_to_write: []` and no deliverables guidance — while the system prompt still said "a scenario that names pages runs its browser steps against those files", referring to nothing. A worker learned which page a browser step runs against only after failing a gate and spending an attempt. M7's second live proof shows it: `web` attempt 1 wrote its operations module and left both pages as the scaffold rendered them, which had been put down to model variance. The only coverage called `build_task_prompt` directly and never exercised the worker, so the suite stayed green; the new test is at the worker level and fails without the fix.
