# ADR-007: Authorisation Enforced at the Retrieval Layer

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Requirements** | SEC-002, SEC-003, FR-021, FR-022, FR-029, THR-001, THR-002, THR-003, THR-007 |
| **Related** | ADR-002, ADR-003, ADR-005 |

## 1. Decision

Authorisation is **pushed down into the index scan**. The ACL predicate is constructed once, at
authorisation time, from the resolved principal — and is applied to **every** retrieval leg **before**
any ranking, assembly or generation. Content the principal may not read is never a candidate, never in
a prompt, never in a diagnostic payload, and never in a cache entry.

Additionally:

- Every chunk row carries a denormalised `acl_principals` array computed from the authoritative
  `document_acl` table, with a **reconciliation job and alert** to detect drift.
- The **cache key includes an `acl_set_hash`**, so a permission change isolates the principal's
  namespace immediately.
- **Postgres row-level security** is a second, independent control, so an application-layer bug is not
  sufficient to leak data.

## 2. Context

This is the highest-severity failure mode in the system (THR-001/002/003): a user retrieves a
document they are not permitted to read. In an LLM system it is worse than in a conventional one,
because the leaked content is not returned as a file — it is **paraphrased into a fluent, confident
answer with citations**, which makes the leak both more useful to an attacker and harder for a
reviewer to notice.

The natural implementation shortcut is: retrieve broadly, generate, then filter citations or verify
permissions afterwards. That is wrong, and the reasons are worth stating precisely.

## 3. Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Filter citations after generation** | The unauthorised content has already been in the prompt, has influenced the answer, and may already be in the model's output. Also, content that shaped an answer but is not cited is invisible to the filter. **This is a data-leak vulnerability, not a UX detail** |
| **Filter after ranking, before assembly** | Better, but the content still reached the retrieval engine and the ranking pipeline. Defence is one accidental refactor away from gone |
| **Separate indexes per role/ACL** | Strongest isolation, but combinatorial: ACL groups × tenants × index versions. Unmaintainable at any real ACL cardinality |
| **Application-layer filtering only, no RLS** | One control. A single missed `WHERE tenant_id = $1` is a breach. **Relying on application discipline for a catastrophic failure class is not acceptable** |
| **Postgres RLS only, no application filtering** | Single control at the storage layer, but filtering cannot be expressed in index-scan predicates as precisely, and it cannot enforce the denormalised ACL projection |
| **Authorisation as a middleware before the service** | Correct as a *step*, insufficient as the *mechanism*: the service must still construct the predicate. Accepted as part of the design (the predicate is built once, at step 2), rejected as the whole answer |

## 4. Why this option

1. **It makes leakage structurally unrepresentable.** Unauthorised content is never a candidate, so it
   cannot be ranked, assembled, generated from, or logged. The property is enforced by the data flow,
   not by a check someone might forget.
2. **It is the only design under which caching is safe.** If retrieval returned unfiltered results, a
   cache would have to store *unfiltered* content keyed by query — which is exactly the leak. Pushing
   authorisation into retrieval makes the correct cache key constructible (ADR-003 §7).
3. **It works with one engine.** Both retrieval legs are inside Postgres (ADR-002), so the identical
   predicate applies to both without a cross-store consistency argument.
4. **It supports hybrid fusion safely.** Fusion operates on already-authorised candidates, so a strong
   lexical match on an unauthorised document cannot promote it.
5. **Two independent controls** (data-access layer + RLS) mean a single defect is insufficient for a
   breach.
6. **It keeps diagnostics safe.** Candidates and scores can be exposed to operators without content,
   because candidates are already authorised.

## 5. Advantages

- Leakage is prevented by construction rather than by diligence.
- Caching is safe and correct.
- Denial-of-information is also prevented: an unauthorised document's *existence* is not inferable from
  the answer, because it never influenced it.
- Multi-tenant isolation, document ACLs and temporal filtering all use the same mechanism — one
  predicate, one place to audit.
- Operationally inspectable: every retrieval trace shows which filters were applied and how many
  candidates each removed (the `retrieval.filter` span).

## 6. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation |
|---|---|
| **Denormalised ACLs can drift** from the authoritative source — the classic silent privilege-loss bug | Reconciliation job with alerting (DI-13); ACLs refreshed on change; never accepted from an upload request |
| **Filter complexity can reduce recall** — over-filtering looks like "not in the corpus" | Measure pre/post-filter candidate counts as a metric; alert on drop-off; denormalisation keeps the predicate cheap |
| Denormalisation must be refreshed on every ACL change | ACL change triggers a targeted refresh; correctness beats avoiding a cheap write |
| Requires discipline in *every* query path | Enforced by the data-access layer (callers cannot construct their own queries) + RLS + a test that attempts a direct unscoped query and must fail |
| Cardinality cost on the chunk row | Bounded by ACL group count; measured in Phase 3; acceptable at expected institutional ACL sizes |

## 7. Tradeoffs

We traded **a modest amount of query complexity and a denormalisation-consistency burden** for
**the elimination of the most severe failure class**. In a system whose credibility rests on "the
citation is trustworthy", leaking the content behind a citation is not a bug to be triaged — it
invalidates the product's central promise.

## 8. Operational consequences

- **The `retrieval.filter` span is the most important trace in the system.** When a user reports
  "it gave me the old regulation", it distinguishes a filter bug (never retrieved) from a ranking bug
  (retrieved, lost) from a generation bug (retrieved and answered) — three different incidents.
- **Per-filter drop-off counters** are SLO-linked: a sudden drop in post-filter candidates indicates a
  broken ACL or temporal predicate.
- **ACL reconciliation is a scheduled, alerted job**, not a background nicety.
- **Privilege changes are audited** and trigger cache namespace isolation.

## 9. Scaling consequences

- The denormalised `acl_principals` array makes the predicate an index-friendly containment check rather
  than a join, which keeps latency flat as ACL cardinality grows.
- At very large ACL cardinality (> 1000 groups per tenant), escalate to **materialised per-group
  indexes** — the predicate shape is unchanged, only its physical encoding changes.
- Under tenant sharding (§4 of ADR-002), every query is already tenant-scoped, so authorisation
  predicates never become cross-shard operations.

## 10. Security consequences

This ADR *is* the security architecture for access control. The resulting properties:

| Property | Mechanism |
|---|---|
| No unauthorised content in prompts | Predicate applied in the index scan |
| No unauthorised content in the model | Same — the model cannot see what the principal cannot |
| No unauthorised content in logs | Candidates are authorised and content-free by design (`NFR-010`) |
| No unauthorised content in cache | `acl_set_hash` in the key |
| No unauthorised content in diagnostics | Candidates carry IDs and scores, not text |
| Tenant isolation independent of app code | RLS |
| Privilege changes take effect immediately | Key recomputed per request from current ACL state |
| Audit of every privileged action | Append-only audit log (SEC-010) |

**Residual risks** (from [§13.5](../phase0/12-threat-model.md)): a bug in the reconciliation job could
leave a stale ACL projection (mitigated by alerting + immediate rebuild path); a compromised IdP could
assert roles that map to over-permissive access (mitigated by failing closed on unknown role claims and
by the separate audit trail).

## 11. Cost consequences

Effectively zero. The denormalised array makes the filter a cheap index predicate; the alternative
(post-filtering) costs nothing to implement and everything to be safe.

## 12. Migration / replacement strategy

If the ACL model changes (e.g. institution-wide read access, `OQ-008`):

- The **predicate shape stays the same** — only how `acl_principals` is computed changes.
- A backfill recomputes the projection for every chunk, running as a background job with the same
  reconciliation loop.
- If ACL groups exceed useful cardinality, migrate to materialised per-group indexes — again, a change
  of physical encoding only.

## 13. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| ACL cardinality explodes | Predicate latency grows with group count | Materialise per-group indexes; consider coarser ACL units (department rather than course) with an audit trail |
| Filter over-restricts | Post-filter candidate counts near zero; users report missing documents | Reduce granularity; investigate whether the drop-off is a bug or genuinely correct |
| RLS and application filters disagree | Reconciliation detects drift | Application filter wins for correctness; RLS policies are updated. **They must never disagree silently** — that is why both exist and why drift is alerted |
| Institutions need document-level rather than tenant-level ACL | OQ-008 resolves that way | No architectural change: `acl_principals` is already per-document |