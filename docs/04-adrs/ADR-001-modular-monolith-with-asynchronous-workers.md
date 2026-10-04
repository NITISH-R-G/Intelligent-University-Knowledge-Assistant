# ADR-001: Modular Monolith with Asynchronous Workers

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Deciders** | Project owner, staff-engineer review |
| **Requirements** | PERF-005, PERF-006, NFR-004…007, OPS-004…006, SEC-007 |
| **Supersedes** | — |

## 1. Decision

Adopt **Option B**: one codebase, one PostgreSQL database, deployed as two process roles —
`api` (stateless, horizontally scaled) and `worker` (asynchronously processing ingestion, scaled on
queue depth). Ingestion never runs inside a request.

## 2. Context

The workload has three conflicting resource profiles: read-serving (I/O-bound on Postgres), document
parsing (bursty, memory-heavy, hostile-input-facing), and model inference (CPU/GPU-heavy). The system
must also satisfy an ingestion freshness SLO of < 120 s while serving queries, isolate the parser from
untrusted input, absorb at-least-once delivery without duplicating knowledge, and remain operable by
one maintainer on free infrastructure.

At the modelled scale (133 QPS sustained, ~10 jobs/s ingestion), the load is small. The difficulty is
not throughput; it is **failure isolation** and **resource asymmetry**.

## 3. Alternatives considered

| Alternative | Why considered | Why not |
|---|---|---|
| Synchronous modular monolith (Option A) | Simplest possible; cheapest | Fails PERF-005/006 (a 25 MB PDF takes minutes in-request), NFR-007 (no backpressure), OPS-005 (no DLQ), SEC-007 (parser in-process with DB credentials) |
| Service-oriented (Option C) | True independent scaling | Operational attention cost unacceptable for one maintainer; network latency consumes the retrieval budget; no benefit at 133 QPS |
| Event-driven services (Option D) | Replay, audit, backpressure | **Eventual consistency contradicts FR-009** (never answer with a superseded rule); highest operational weight; exactly-once is not achievable — idempotency is, and Option B has it |
| Serverless functions | Packaging variant of B | Comparable topology; revisit only for GPU burst embedding (`OQ-006`) |
| Monolith + outbox/CDC from D | Best of both | **Adopted as an element inside B** — the outbox pattern for publishing change events is in scope for Phase 6+ |

## 4. Why this option

1. It is the **cheapest architecture that satisfies every MUST requirement**. Option A fails six.
2. It separates **failure domains** that must not be coupled: a parser crash cannot take down serving.
3. It provides **independent scaling policies** for read traffic and burst ingestion, from the same image.
4. It preserves **ACID across publication**, because documents, versions, chunks and vectors live in one
   transaction in one database — the property the whole correctness story depends on.
5. It keeps **extraction seams**: an independently deployable worker already exists, and queue payloads
   are versioned (`job_schema_version`), so version skew is tolerated. See §9.10 of the review.
6. It was **robust under weight sensitivity**: B wins under three different weightings of the decision
   matrix, and survives the non-compensatory veto test.

## 5. Advantages

- Cheap: one small host; workers scale to zero when the queue drains.
- Simple to reason about: no network hops, no distributed transactions, no service-to-service auth.
- Backpressure, retries and a DLQ become expressible in one data model.
- The parser can be isolated in a sandboxed subprocess with no DB credentials — a real control, not a
  nominal one.

## 6. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation |
|---|---|
| **Postgres is a single point of failure** | Named as the irreducible dependency; mitigation is failover + PITR + honest RTO/RPO. No cache-only fallback, because serving stale policy answers is worse than an outage |
| **Ingestion I/O can degrade query latency** | Separate connection pools, `statement_timeout`, autovacuum tuning, reads from a replica under load, per-tenant ingestion concurrency limits |
| **ANN index build competes with serving** | Shadow-index build + atomic swap, never in-place `REINDEX`; build at reduced priority |
| **Two roles means two failure modes** | Explicitly better than one coupled failure mode; both are modelled in the FMEA |
| **Postgres-as-queue has a throughput ceiling** | ~1–5 k jobs/s vs 10/s needed — a 100× margin. `JobQueuePort` isolates the choice |
| **Shared database can erode module boundaries** | CI import-boundary lint rule (NFR-002) and rules R2/R5 |

## 7. Tradeoffs

We traded **theoretical horizontal scalability of each component** for **drastically lower operational
cost and preserved transactional integrity**. At 100× the modelled scale this trade would need
revisiting — which is exactly what the pre-agreed triggers in §7.7 of the review specify.

## 8. Operational consequences

- **Two things to deploy** instead of one; one stack, one database, one set of runbooks.
- **Two capacity signals**: API CPU/latency, and queue depth/age.
- **More incident paths**: worker-stall runbook required (not just service-down).
- **Onboarding**: two roles from one image; simpler than two services.

## 9. Scaling consequences

| Scale | Action required |
|---|---|
| Current model (22 M chunks) | None — one instance |
| ~10⁸ chunks | Tenant-hash partitioning across instances; route per tenant so retrieval stays single-shard |
| > 100 QPS sustained | Add API replicas (already stateless); then read replicas |
| Serving and embedding need different hardware | Extract the embedding worker (R3/R4 already in place) |
| A second team owns ingestion | Extract the ingestion service |

## 10. Security consequences

- **Positive**: parser isolation (no network, no credentials, hard resource limits) is a genuine control.
  The API role never holds file bytes. Worker privileges are narrower than admin privileges.
- **Negative**: one database means one trust domain. Mitigated by separate roles with least privilege,
  RLS as an independent control, and no `DROP`/`TRUNCATE` grant to the application role.
- The queue payload crosses a trust boundary, so it is **versioned and validated**, and payloads never
  contain secrets or document content.

## 11. Cost consequences

T-1 and T-2 at **$0**. T-3 cost is dominated by the embedding worker and LLM inference, not by the
topology ([§18.4](../phase0/17-cost-model.md)) — so this decision has **almost no effect on the
budget**, which is itself a reason to prefer it.

## 12. Migration / replacement strategy

Pre-agreed extraction order, chosen so extracted services never call back into the monolith:

1. **Embedding worker** — pure producer/consumer; R3 + R4 already in place.
2. **Indexer** — owns the chunk/vector write path.
3. **Retrieval** — read-only; the strongest isolation boundary and the biggest latency win.
4. **Query/Answer** — last; most coupled to authorisation and context assembly.
5. **Admin** — stays in the monolith.

Reverting is equally cheap: merge the worker back into the API image; the queue remains valid.

## 13. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| Query load far exceeds the model | Sustained > 100 QPS, autoscaling pressure | Extract retrieval/query (steps 3–4) |
| Tenants require physical isolation | Regulatory or contractual requirement | Jump to per-tenant databases behind the same `VectorStore`/`ObjectStore` ports |
| Ingestion volume dwarfs querying | Queue age chronically > 5 min | Extract the ingestion service; add a broker behind `JobQueuePort` |

None of these invalidate the decision; all are the consequences of **keeping the seams rather than
removing them**.