---
id: only-remember-what-the-gates-accepted
title: "Only remember what the gates accepted"
status: done
track: E
tag: engine
milestone: pre
order: 17
sha: 5cd675a
---

The memo was written as soon as source applied, so a generation whose gates then failed
stayed replayable. The worker now stages it and cannot commit it — whatever ran the
gates does.
