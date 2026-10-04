"""Observability: structured logging, metrics and trace context.

**Invariants enforced by CI:** importable by ``infrastructure``, ``interfaces``, ``workers``
and the composition root; never by ``domain`` or ``application``. The domain raises typed errors
and knows nothing about how they are reported.

Two rules are enforced here rather than left to reviewer diligence:

**Telemetry carries no content and no secrets.** A redaction processor masks denylisted keys
and truncates long values before serialisation, so a caller who forgets cannot leak. The
allow-list/deny-list choice is deliberate: an allow-list would break every new call site that
adds a field, and a denylist fails open on an unanticipated key name - so the denylist is
deliberately broad and the structured schema requires known keys anyway.

**Metric labels are low-cardinality by construction.** The metrics module exposes counters and
histograms whose label sets are fixed at construction. There is no API for attaching an
arbitrary label, which removes the most common way a Prometheus deployment is destroyed.
"""
