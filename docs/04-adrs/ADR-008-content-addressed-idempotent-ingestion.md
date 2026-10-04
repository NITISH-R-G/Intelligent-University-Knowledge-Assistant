# ADR-008: Content-Addressed, Idempotent, Versioned Ingestion Pipeline

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Requirements** | FR-006, FR-007, FR-008, FR-011, FR-013, FR-027, NFR-004, OPS-004, OPS-005 |
| **Related** | ADR-001, ADR-002, ADR-007 |

## 1. Decision

Ingestion is a **content-addressed, idempotent, versioned, transactional** pipeline:

1. **Identity** comes from content, not filename. A content hash identifies a document's substance;
   the same bytes under two names resolve to one document with two sources.
2. **Versions are immutable.** Every content change creates a new version. A published version is never
   updated in place.
3. **Idempotency at every boundary.** Re-running any stage, job or batch is a no-op if the work is
   already committed.
4. **Incremental embedding.** Only chunks whose `content_hash` changed are re-embedded.
5. **One transaction** for the chunk + FTS + vector write, so a partially indexed document is
   unrepresentable.
6. **Provenance per chunk**: `parser_version`, `chunker_version`, `embedding_model_id`, `index_version_id`.
7. **DRAFT → publish gate**: parsed content becomes retrievable only after an administrator publishes it
   with explicit effective dates.
8. **Quarantine, never silent deletion**: validation/injection/limit failures move the document to
   `QUARANTINED` with a machine-readable reason and the original is retained for review.

## 2. Context

Three properties are non-negotiable for a policy system:

- **Repeated ingestion must not create duplicate knowledge.** At-least-once delivery is the default
  semantics of any durable queue, so retries are certain, and a crash between "vector written" and
  "job marked done" guarantees a redelivery. Without idempotency, a retry storm silently duplicates
  chunks, and duplicated boilerplate degrades every subsequent retrieval.
- **The index must reflect the current policy.** Partial writes, non-atomic version switches, and
  half-migrated index generations are all ways to answer with an old or incomplete document.
- **Reindexing must be possible.** Changing the parser, the chunker, or the embedding model is
  inevitable; without provenance it is impossible to know what needs rebuilding.

The pipeline also faces hostile input (malicious uploads, zip bombs, parser RCE) and metadata errors
that are only dangerous once a document is live.

## 3. Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Upsert by filename/URL** | Files get renamed; URLs change; a renamed file becomes a duplicate document |
| **Mutable documents (update in place)** | Destroys auditability; makes "what was live on 3 March?" unanswerable; breaks citation resolution for historical answers |
| **Update-in-place chunk rows** | Same problem one level down; also breaks deterministic chunk IDs and incremental re-embedding |
| **Advisory-lock-only idempotency** (a lock table, no content hash) | Does not cover crash windows, replays, or manual re-runs. Lock-based idempotency is exactly the part that is unreliable |
| **Event-sourced document state** | Powerful audit, eventual consistency, and a large operational cost — for a workload where a version table + audit log gives 95 % of the benefit at 5 % of the complexity |
| **Auto-publish on successful parse** | Removes the human gate that catches metadata errors and poisoned content. Metadata errors are only dangerous once a document is live |
| **At-least-once with best-effort dedupe in retrieval** | Hides the duplication instead of preventing it; wastes context and misleads the ranker |

## 4. Why this option

1. **Idempotency becomes a property of the data, not of the code path.** A content hash is checked in a
   unique constraint; correctness does not depend on which code path runs or how many times.
2. **Crash-window safety is automatic.** If a job dies after committing but before acknowledging, the
   retry sees the content hash already present and does nothing.
3. **Batch replay is byte-identical**, which makes the whole pipeline testable: replay a 1,000-document
   batch and compare index state.
4. **Cost is bounded by change, not by upload.** A 5 %-changed document re-embeds ≤ 8 % of chunks, so
   daily churn of 440 k chunks at stress scale is the re-embedding cost — not the whole corpus.
5. **Immutable versions make publication atomic** (`current_version_id` is one pointer in one
   transaction) and rollback a pointer flip rather than a data migration.
6. **Provenance makes reindexing a computed decision** rather than a guess: "which chunks were produced
   by chunker v1?" is a query, and a mixed-generation index can be detected and refused.
7. **The publish gate converts a dangerous error class into a detected one.** A wrong effective date
   caught at review is a data-quality issue; caught after publication it is a correctness incident.

## 5. Advantages

- Duplicate knowledge is structurally impossible.
- Re-uploading an unchanged file is free and instant.
- Every answer is explainable to a specific version with its provenance.
- Rollback is a pointer flip, < 5 s, itself audited.
- Model/chunker/parser upgrades are a background reindex with a shadow index and atomic swap.
- One bad document cannot stop a batch (FR-011) — quarantine with a reason, pipeline continues.
- The pipeline is replayable, therefore testable, therefore trustworthy.

## 6. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation |
|---|---|
| **Storage grows with versions** — superseded versions are retained | Version-aware partitioning; retention policy per document class; version pruning is explicit and audited |
| **Re-embedding cost on every genuine change** | Bounded by changed-chunk diffing (FR-008); embed only what changed |
| Content hashing requires reading the whole object | Hash at upload time and store with the object; re-verify only on integrity checks |
| **Per-chunk provenance increases row width** | Measured in Phase 3; negligible against the vector column |
| Deterministic chunk IDs require a stable chunker | `chunker_version` in the ID derivation; a version change forces a reindex |
| Quarantined documents consume storage | Retention policy for quarantine; admin review queue |

## 7. Tradeoffs

We traded **storage growth and pipeline complexity** for **provable idempotency, auditable versions and
safe reindexing**. Given that institutional policy changes constantly and that an answer must be
defensible months later, retained version history is a feature, not overhead.

## 8. Operational consequences

- **Job states are a first-class operational surface**: queue depth, age, retry counts, DLQ, quarantine
  reasons by category (all metrics + alerts).
- **DLQ replay** is a documented procedure with a runbook; a permanently failing job is an operational
  item, not a silent loss.
- **Reindex runs are planned operations**: shadow build, atomic swap, verify, retain the old generation
  for rollback, then drop.
- **Quarantine is a workflow**, not an error: an administrator reviews the reason and either fixes the
  source document or rejects it.
- **Reconciliation jobs** verify that chunk ACL projections and provenance distributions match their
  sources (DI-08, DI-13).

## 9. Scaling consequences

| Change | Effect |
|---|---|
| Larger corpus | No change to the model; partitioning by tenant + time makes pruning and export bounded |
| Higher churn | Re-embedding is proportional to change; GPU burst capacity becomes the scaling lever |
| Embedding model change | Full re-embed as a background job with shadow index; **the version model is what makes this safe** |
| Multiple institutions | Content addressing is tenant-scoped; a hash collision across tenants is a non-issue |
| Beyond 10⁸ chunks | Partition pruning by time/tenant; shard by tenant hash |

## 10. Security consequences

| Property | Mechanism |
|---|---|
| Hostile content never becomes live without review | DRAFT → publish gate |
| Poisoned/injected content is retained for investigation, not silently deleted | Quarantine state with reason; admin review |
| Parser compromise is contained | Sandboxed subprocess, no network, no credentials, hard limits |
| ACLs are attached at ingest from the authoritative source | Never from the uploaded document |
| Privileged lifecycle actions are non-repudiable | Append-only audit log with actor, reason, before/after |
| Type-confusion / mislabelled files rejected | Content sniffing, not MIME trust |
| **Publication is a controlled act** | Effective dates required; supersession requires an explicit close date; cycle detection |

**The upload path is the system's largest attack surface**, because it is the only place attacker-
controlled bytes enter a persistent store. Every control in the table above exists because of that fact.

## 11. Cost consequences

- **Storage**: 0.51 TB of originals at stress scale + ~31 GB of text; superseded versions add growth
  that retention policy controls.
- **Compute**: embedding cost is proportional to *changed* chunks, not to corpus size — the single most
  important cost property of the design.
- **Verification**: replay-based tests are cheap because the pipeline is idempotent; a
  non-idempotent pipeline would require much more expensive integration testing.

## 12. Migration / replacement strategy

| Change | Procedure |
|---|---|
| Parser upgrade | Re-parse all versions; new `parser_version`; shadow chunks; atomic swap |
| Chunker change | `chunker_version` bump; re-chunk; new shadow index; swap; retain old generation N days |
| Embedding model change | New `IndexVersion`; re-embed all chunks; shadow index; verify recall on the benchmark; swap |
| Adding a document field | Nullable column, backfill in background; forward-compatible reads throughout |
| Version retention change | Policy change; pruning runs as an audited job with a dry-run mode |

**Every migration is the same shape**, because the version model was designed to make migration a normal
operation rather than an emergency.

## 13. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| Storage growth from versions is unacceptable | Storage pressure with low churn | Add version pruning by policy; retain a summary version rather than all versions |
| Content hash collisions | Essentially impossible with SHA-256; a collision would surface as an incorrect dedupe | Add `(hash, size)` and a byte-level verification on suspected collision |
| Re-embedding on every change is too slow | Freshness SLO missed under high churn | GPU burst capacity; per-tenant embedding concurrency; `OQ-006` |
| The publish gate is a bottleneck | Admin complaint about publish latency | Measure and publish the actual queue wait; consider auto-publish for trusted sources with a mandatory post-publication audit sweep |
| Deterministic chunk IDs are insufficient for diffing | Re-embedding more than the changed-chunk threshold | Per-chunk content hash (already implemented) is the real diff key; IDs need not encode content |