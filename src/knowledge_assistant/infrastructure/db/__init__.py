"""Database adapters.

**Why psycopg 3 in async mode.** The async API is native to psycopg 3 rather than bolted onto
a sync driver with a thread pool, which removes a whole class of "coroutine blocked on a
thread" bugs. Connection pooling uses ``psycopg_pool.AsyncConnectionPool``.

**Statement timeouts are set per connection, not per statement.** Relying on the PostgreSQL
default (infinite) means one pathological query holds a pooled connection forever and the
service dies of pool exhaustion rather than of a timeout. The timeout is applied in the pool
configuration so it cannot be forgotten at a call site.

**Transaction discipline** is documented in ``docs/03-architecture/TRANSACTIONS.md`` and
enforced by the repository methods: each public method opens at most one transaction, and
the atomic job claim is a *single statement* so that "claim then act" cannot interleave.
"""