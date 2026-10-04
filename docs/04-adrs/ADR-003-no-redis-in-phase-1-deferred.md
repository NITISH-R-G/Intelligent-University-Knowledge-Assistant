# ADR-003: No Redis in Phase 1 — Deferred Behind a CachePort

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Requirements** | FR-033, PERF-013, NFR-007 |
| **Related** | ADR-001, ADR-002 |

## 1. Decision

Use an **in-process TTL cache** in the `api` role for answer caching. Introduce **no Redis, no
Memcached, and no external cache** in Phases 1–11. All cache access goes through a `CachePort`
interface so Redis can be added later as an adapter without touching call sites.

## 2. Context

Caching answers is valuable: institutional queries are highly repetitive (the same handful of
questions dominate — attendance, resit eligibility, deadlines, library rules), so a high hit rate is
plausible. But caching in a permissioned knowledge system carries a **specific security hazard**: a
cache keyed on the query alone will serve one principal's answer to another. The cache key must include
the resolved ACL set.

The decision also has to survive the project's free-tier and single-maintainer constraints.

## 3. Alternatives considered

| Alternative | Advantages | Why not now |
|---|---|---|
| **No cache at all** | Simplest; no correctness risk | Leaves an entire latency and cost lever unused; recomputing a popular query every time is wasteful |
| **Redis** | Shared across replicas; survives restarts; higher hit rate with many API replicas | New component = new failure mode + new secret + new thing to patch; at 133 QPS sustained the win is small; **a cache outage must not be an outage** |
| **CDN / HTTP cache** | Free, fast | Cannot cache per-principal answers; static assets only |
| **Postgres table as a cache** | No new component | Cache-in-database is a well-known anti-pattern: it pollutes the transactional store and evicts pages the corpus needs |
| **In-process LRU/TTL cache** | Zero infrastructure; per-replica; trivially correct with a proper key | Lower hit rate with many replicas; lost on restart (acceptable) |

## 4. Why this option

1. **The requirement is latency and cost, not cross-replica coherence.** At the modelled load, an
   in-process cache is sufficient.
2. **Fewer components is the correct default for a single maintainer.** Every additional component is a
   runbook, an upgrade, a failure mode and a credential.
3. **The security-correctness argument is simpler.** A per-replica cache cannot serve a stale
   cross-replica entry; correctness rests on the key, not on a distributed invalidation protocol.
4. **The interface removes the reversal cost.** `CachePort` means the decision is deferred, not locked.

## 5. Advantages

- Zero infrastructure, zero credentials, zero operational surface.
- No cache-related outage mode: the cache cannot be unavailable, only absent.
- Cache failures degrade to the full pipeline, which is the correct behaviour.
- Instant to implement and to reason about; trivially local in development.

## 6. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation / trigger |
|---|---|
| Hit rate divided across N replicas | Target ≥ 30 % with a few replicas; trigger is a measured miss rate |
| Cache lost on restart | Acceptable — it is a cache |
| No cross-replica invalidation of answer entries | **Solved by keying on `index_version`**: a publish bumps the index version, so all stale entries become unreachable without any invalidation traffic |
| Duplicate work during a thundering herd | Single-flight per key with jittered delays |

## 7. The cache key (the security-critical design)

```
key = hash(
    tenant_id,                 // isolation
    normalised_question,       // the query
    index_version,             // any publish invalidates every cached answer
    acl_set_hash,              // a permission change isolates the principal
    tenant_config_hash         // models/flags/depth changes invalidate
)
```

Three properties follow, and each is a test:

1. **A permission change cannot leak.** The principal's effective ACL set is part of the key.
2. **A publication cannot serve a stale answer.** The index version is part of the key, and publishing
   bumps it.
3. **A negative cache is safe** because abstentions are keyed the same way — and caching abstentions
   matters, because "not in the corpus" is a common and cheap-to-recompute-expensive query.

**This is the second reason ADR-007 exists.** If authorisation is a filter applied after the fact, the
cache cannot be keyed correctly at all — the cache would have to hold the *unfiltered* result. Pushing
authorisation into the retrieval layer is what makes safe caching possible.

## 8. Tradeoffs

We traded **peak hit rate** for **zero operational surface**. If the hit-rate target turns out to be
missed by a wide margin, adding Redis behind `CachePort` is a configuration-plus-adapter change, not a
redesign.

## 9. Operational consequences

- **No cache dashboard initially.** Metrics instead: `cache_hits_total` / `cache_requests_total` by
  namespace type, and hit rate as an SLO-linked metric.
- **No cache runbook** — because the cache cannot fail independently.
- **Alerting**: none for the cache; the hit rate is a cost/latency metric, not an availability one.

## 10. Scaling consequences

At > 200 QPS sustained, or with > 3 API replicas and a measured hit rate below 30 %, add Redis behind
`CachePort`. At that point the key structure above carries over unchanged, so the migration is
additive.

## 11. Security consequences

- **Positive**: cache correctness rests on the key rather than on distributed invalidation. No
  cache-fill, cache-stampede or cache-poisoning protocol to get wrong.
- **Residual**: a stale *entry within one process* after an ACL change is prevented by `acl_set_hash`
  being computed from the current ACL state at request time — the key is recomputed per request, so a
  change is effective immediately for the next request on that replica.
- **Negative**: an unbounded cache is a memory-exhaustion vector. Mitigation: a **bounded size with LRU
  eviction** plus a TTL. Bounded memory is a requirement (PERF-014) because it is what makes horizontal
  scaling safe.

## 12. Cost consequences

$0. The avoided cost is the LLM line item on repeated queries — the largest single cost lever in
[§18.4](../phase0/17-cost-model.md). A 30 % hit rate on generated answers removes ~30 % of
generation cost, which is why this "simple" decision is also a **financial** one.

## 13. Migration / replacement strategy

Implement `CachePort` with `get`, `set(key, value, ttl)`, `delete_namespace`. Phase 1: in-process
implementation. Later: a Redis adapter with the same semantics. No call-site changes; no key changes.

## 14. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| Hit rate is much lower than expected | Measured hit rate < 15 % over a week | Check normalisation and index-version churn first — an index version bump per publish would make the hit rate structurally zero. Only then add Redis |
| Hit rate is much higher than expected | > 80 % | Reduce cache TTL to limit staleness exposure; no action needed |
| Many replicas needed | > 3 API replicas | Add Redis; the key design is unchanged |