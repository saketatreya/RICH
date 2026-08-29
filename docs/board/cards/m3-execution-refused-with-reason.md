---
id: m3-execution-refused-with-reason
title: A build the host cannot run is refused, with the reason
status: done
track: E
milestone: M3
tag: engine
sha: ec667a7
finished: '2026-08-29'
---

Found by the M3 drive: the executor died on SandboxUnavailable in a background thread and the run stayed `ready` forever. The executor is now asked whether this host can run before a run is accepted (503 with the reason); a backgrounded death records why; the monitor surfaces it; the drive fails fast.
