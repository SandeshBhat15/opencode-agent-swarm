---
description: Changelog of swarm policy and lesson changes.
mode: subagent
hidden: true
color: "#64748b"
---

Append one entry per applied change to swarm policy or recorded lesson.

Format each entry as:

```
- <date> | <kind> | <target> | <change> | evidence: <samples>, <metric> <before> -> <after>
```

Rules: one line per change, newest last, real numbers only. An unmeasurable change is
recorded as `evidence: unmeasurable`. Never rewrite history; never delete an entry.
