# ADR-002: PostgreSQL + pgvector as the Single Data Store

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Requirements** | FR-007, FR-009, FR-012, FR-020, PERF-001…003, SEC-003, OPS-004 |
| **Related** | ADR-001, ADR-003, ADR-007 |

## 1. Decision

Use **PostgreSQL + pgvector** as the single data store for documents, versions, chunks, vectors, the
lexical index, the job queue and the audit log. No second data store in the default architecture.

## 2. Context

The system's central correctness requirement is FR-009: **a superseded version must never be
retrievable by a default query, and a publish must be atomic.** That requirement is fundamentally a
transactional-consistency requirement over the document, its version pointer, its chunks and its
vectors.

The storage layer must additionally support: dense-vector search with metadata filters, lexical search,
full-text ranking, durable job queue semantics with at-least-once delivery, multi-tenant isolation,
audit-only append, and operation on a free-tier machine.

At the modelled stress scale (22 M chunks, ~90–110 GB) the entire corpus fits in one instance.

## 3. Alternatives considered

| Alternative | Advantages | Why rejected |
|---|---|---|
| **Specialised vector DB** (Qdrant/Milvus/Weaviate) | Better ANN recall/latency at 10⁸+; purpose-built filtering | **Publish becomes a non-atomic two-store operation.** A failure between stores leaves the corpus internally inconsistent — the exact failure the project exists to prevent. Second backup, second failure mode. High lock-in |
| **Elasticsearch/OpenSearch** | Excellent BM25, analyzers, highlighting | Second store; heavy memory footprint unfriendly to a 1–2 GB free VM; no transactional tie to the document row |
| **MySQL** | Familiar | No mature vector support; weaker FTS |
| **SQLite** | Zero ops | Single writer; no concurrent scale; no multi-tenant RLS story |
| **Split: Postgres + one vector store** | Best of each | Same atomicity problem as above. Can be adopted later behind `VectorStore` |

## 4. Why this option

1. **Atomic publication.** `UPDATE documents SET current_version_id = …` and the version/chunk/vector
   writes are one transaction. A reader can never observe a half-published document. This is the
   property that makes Journey E safe and FR-012 rollback a pointer flip.
2. **One ACL filter for both retrieval legs.** The lexical and dense legs run inside the same engine, so
   the identical tenant/ACL/temporal predicate applies to both without a cross-store consistency
   argument. This is what makes ADR-007 implementable.
3. **One backup, one restore, one transaction log.** Operational simplicity is worth real money for a
   single-maintainer system.
4. **Adequate at the modelled scale.** 22 M chunks × 384-d fp16 = 16.9 GB of vectors, ~21 GB with ANN
   overhead; total database ~90–110 GB.
5. **Multi-tenancy enforcement in the database.** Row-level security is a second, independent
   authorisation control that no separate store provides for free.
6. **The queue lives where the state changes**, so claiming a job and writing its result is one
   transaction — exactly-once *effect* from at-least-once delivery (OPS-004).

## 5. Advantages

- ACID across the whole domain, including publication.
- Lexical + dense + metadata filtering in one query plan, one connection, one latency budget.
- Mature, extremely well understood, abundant tooling; trivially self-hostable.
- `tsvector` + GIN gives credible lexical search without a second system.
- Partitioning by `tenant_id` gives tenant-co-located storage: export, deletion and backup become
  bounded operations.
- Free/self-hostable with no licensing constraints.

## 6. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation / trigger |
|---|---|
| **HNSW recall and build time degrade at scale** | Measured recall/latency curve in Phase 10; tune `ef_search`/`m`; consider IVFFlat or quantisation; **escalate to a dedicated `VectorStore` past ~10⁸ chunks** |
| **ANN build competes with serving I/O** | Shadow-index build + atomic swap; never in-place `REINDEX` on a serving table |
| **FTS is not true BM25** | Evaluate the `pg_search` (ParadeDB) extension behind `LexicalSearchPort`; adopt only on measured Recall@20 evidence |
| **A second store is needed eventually at 10⁹ chunks** | Accepted and planned; the port exists so it is an adapter, not a redesign |
| **No distributed transactions across shards** | Not needed until the sharding trigger; single-shard-per-tenant queries avoid scatter-gather |
| **Queue throughput ceiling ~1–5 k jobs/s** | Need is 10/s — a 100× margin. `JobQueuePort` isolates the replacement |

## 7. Tradeoffs

We traded **best-in-class vector performance and maximum horizontal scale** for **transactional
integrity and operational simplicity**. For a policy-knowledge system where *correctness under
versioning* is the product, that is the correct side of the trade. If the product were instead
"search a billion web pages", the trade would reverse.

## 8. Operational consequences

- **Backups**: one logical backup strategy (WAL/PITR + object-store snapshots); restore drill in Phase 11.
- **Monitoring**: connection pool, query latency percentiles, slow queries, deadlocks, disk, replica lag,
  index-build duration.
- **Single point of failure**: named explicitly; managed failover at T-3, restart + restore at T-2.
- **No cache-only fallback**: if Postgres is down, the system says so. Serving stale policy answers
  would be equivalent to the supersession bug we are preventing.

## 9. Scaling consequences

| Range | Approach |
|---|---|
| ≤ 10⁷ chunks | Single instance, everything in one database |
| 10⁷–10⁸ | Tune ANN params; consider partitioning; read replica for search |
| > 10⁸ | Tenant-hash sharding across instances; **every query is tenant-scoped, so it stays single-shard** |
| > 10⁹ | Dedicated vector service behind `VectorStore`; probabilistic store for exact text |

The key enabler: **tenant-scoped queries make horizontal sharding cheap**, because there is no
cross-tenant scatter-gather. This is a property of the multi-tenancy requirement, not a coincidence.

## 10. Security consequences

- **Positive**: RLS provides an authorisation control **independent of application code** — a single
  application-layer bug is not sufficient to leak data across tenants. Least-privilege roles mean the
  application cannot `DROP`/`TRUNCATE`. Audit tables are append-only (no UPDATE/DELETE grant).
- **Negative**: one engine = one blast radius. A Postgres compromise exposes everything. Mitigated by
  encryption at rest, least privilege, and the fact that this is the same exposure profile as any
  single-store system.
- `pg_stat_statements` may capture query text — **must be reviewed for PII** before enabling in a
  multi-tenant deployment.

## 11. Cost consequences

T-1/T-2: **$0** (self-hosted). T-3: $700–1,500/month for a managed HA instance plus a replica — a
modest share of total cost, most of which is LLM inference. **The storage decision is not the cost
decision.**

## 12. Migration / replacement strategy

| Escape hatch | Cost |
|---|---|
| **→ dedicated vector DB** | Implement `VectorStore`; backfill embeddings; dual-write during cutover; **publish atomicity is the hard part** — use a dual-write + verification window, or keep Postgres authoritative for version state and treat the vector store as a rebuildable projection |
| **→ separate search engine** | Implement `LexicalSearchPort`; reindex FTS data |
| **→ different relational DB** | High. The schema is deliberately conventional; the queue (`SKIP LOCKED`) and RLS would be the hard parts |
| **→ hosted managed Postgres** | Low. Standard SQL; the intended and recommended T-3 path |

## 13. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| Corpus outgrows one instance sooner than 10⁸ chunks | Recall/latency degrade; build times unmanageable | Tenant-hash sharding (R7), then `VectorStore` extraction |
| FTS recall is inadequate | Lexical Recall@20 < 0.85 | Adopt `pg_search` BM25 inside Postgres; escalate to OpenSearch only if that also fails |
| Queue ceiling reached | Sustained > 1 k jobs/s | Replace via `JobQueuePort` — the state transition contract is unchanged |