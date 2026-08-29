---
id: m11-serve-beyond-loopback
title: serve binds 0.0.0.0 only when told the port is published to loopback; checks enforced
status: done
track: E
milestone: M11
sha: 96d694d
finished: '2026-08-29'
---

--published-on-loopback allows --host 0.0.0.0; beyond loopback the Host check and the Origin check for mutations are enforced rather than optional. On loopback nothing changes.
