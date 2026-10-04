"""Worker process: asynchronous job execution.

**Invariants enforced by CI:** may import ``application``, ``domain``, ``config``,
``observability`` and ``infrastructure``. It is a *process*, not a layer: it contains the run
loop and the handler registry, and no business rules.

Why a separate process rather than background tasks in the API: ingestion and serving have
opposite resource and failure profiles, and Phase 0's Option B was chosen precisely to keep
them separable. A worker crash must not take the API with it, and a slow job must not consume
an API request slot.

The worker is the **only** component that executes handlers, and handlers are registered
explicitly. A job type with no registered handler dead-letters immediately rather than retrying
forever - a missing handler is a deployment defect, not a transient fault.
"""