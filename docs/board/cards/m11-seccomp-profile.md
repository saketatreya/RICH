---
id: m11-seccomp-profile
title: 'docker/seccomp.json: Docker''s default profile plus only the namespace syscalls Bubblewrap needs'
status: done
track: E
milestone: M11
sha: decfbb7
finished: '2026-08-29'
---

Derived from moby/profiles seccomp/default.json (clone, clone3, unshare, setns, mount, umount2, pivot_root, mount_setattr, open_tree, move_mount allowed unconditionally; nothing else changed). CI runs the container with it and requires rich doctor green; the default profile is recorded as refusing the user namespace.
