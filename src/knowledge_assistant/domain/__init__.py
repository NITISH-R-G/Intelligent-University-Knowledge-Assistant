"""Domain layer.

**Invariants enforced by CI** (``tests/architecture/test_dependency_rules.py``):

1. This package imports **only** the Python standard library.
2. It performs **no I/O**: no network, no filesystem, no database, no clock read.
3. It contains no framework types (no FastAPI, no pydantic, no structlog, no ORM).

Why: the domain encodes the rules that must stay correct as the system grows. Keeping it
free of I/O makes every rule deterministically testable, which is what allows the
retry/backoff/idempotency logic to be property-tested rather than only example-tested.

The single I/O-shaped exception is :mod:`knowledge_assistant.domain.clock`, which
*declares* time as a collaborator. That is deliberate: time is an input, not an ambient
fact, and Phase 1 needs deterministic tests of lease expiry and retention.
"""
