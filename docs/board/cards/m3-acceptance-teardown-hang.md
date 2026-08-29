---
id: m3-acceptance-teardown-hang
title: Acceptance teardown can hang in the sandbox until the attempt timeout
status: next
track: E
milestone: M3
---

First M4 live run (run 7b4961c8, 2026-08-30 18:33Z): both scenarios failed within 60 s, then Playwright's runner sat in epoll for nine minutes with No projects matched the filters in "/home/zaphod/work/rich" and next-server (v16.2.12) alive until the 600 s attempt timeout. On the host the same workspace exits in 39 s, so the hang is sandbox-specific (bwrap --new-session/--unshare-pid and Playwright's process-group kill of the web server are the suspects). A host-side reproduction inside the sandbox failed to launch Chromium (pthread_create EAGAIN) while another build was running, so it is unproven. Cost: ten minutes per failed acceptance attempt. Candidates: webServer.gracefulShutdown in the scaffold's Playwright config, starting next directly instead of through pnpm, a per-gate timeout below the task timeout.
