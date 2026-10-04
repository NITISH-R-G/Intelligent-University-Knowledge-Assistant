"""Application layer: use cases and the ports they depend on.

**Invariants enforced by CI:**

1. Imports ``domain`` freely. Never imports ``infrastructure``.
2. Declares ports (Protocols) for every collaborator it does not own.
3. Contains no framework types: no FastAPI, no pydantic models on the request path, no
   database driver. Use cases raise ``domain.errors.ApplicationError`` subclasses.

The dependency direction here is the one that matters: use cases know *that* they need to
store a job, not *how* jobs are stored. That is what lets the entire job lifecycle be tested
against an in-memory double, and what lets the Phase 0 storage decision (PostgreSQL) be
revisited without touching a single use case.
"""