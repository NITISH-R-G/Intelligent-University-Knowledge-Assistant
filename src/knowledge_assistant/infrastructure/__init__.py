"""Infrastructure layer: adapters for everything outside the process.

**Invariants enforced by CI:** may import ``domain``, ``application``, ``config`` and
``observability``. Never imported *by* ``domain`` or ``application``.

Adapters are where the three "replaceable" decisions of Phase 0 actually live: the database
driver, the migration mechanism, and the telemetry backend. Each is hidden behind a port that
the application layer declared, so replacing one is an adapter swap plus a composition-root
change and nothing else.
"""