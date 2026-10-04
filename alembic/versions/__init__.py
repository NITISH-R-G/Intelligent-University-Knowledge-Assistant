"""Alembic migration revisions.

Every module in this package is one migration revision. Files are named
``<revision>_<slug>.py`` and are executed in filename order by Alembic's version table.

The Phase 1 revision deliberately creates only the two tables the foundation needs -
``jobs`` and ``idempotency_keys`` - plus the ``vector`` extension. No business tables exist
yet: a speculative schema is expensive to remove once it has been written to.
"""
