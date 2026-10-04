# Diagram Register

**Rule:** every diagram must explain a decision or a system behaviour. A diagram that only depicts is
decoration and is prohibited. This register records, for each diagram: what decision or behaviour it
exists to settle, and which phase owns its final form.

Diagrams are Mermaid inside Markdown, so they live in version control, diff cleanly, and render on
GitHub and in the docs site.

**Status legend.** ✅ exists in Phase 0 · 📋 scheduled with owning phase

---

## 23.1 Diagrams delivered in Phase 0

| # | Diagram | Document | What it exists to explain |
|---:|---|---|---|
| 1 | System context | [§6.1](05-system-boundaries.md) | Who uses the system, what it depends on, and that the model runtime and object store are **outside** the trust boundary |
| 2 | Trust boundaries (TB0–TB5) | [§6.2](05-system-boundaries.md) | Where trust begins and ends, with 9 numbered crossing rules that are each testable controls |
| 3 | Security boundary (secure / semi / hostile zones) | [§6.3](05-system-boundaries.md) | That uploaded documents, chunk text, metadata, citations and model output are all **hostile input** |
| 4 | Multi-tenancy isolation + escalation path | [§6.5](05-system-boundaries.md) | Shared-DB-with-RLS today, and the four specific triggers that escalate to per-tenant DBs |
| 5 | Option A — synchronous monolith | [§7.1](06-architecture-options.md) | Why one process cannot separate ingestion from serving |
| 6 | Option B — monolith + async workers | [§7.2](06-architecture-options.md) | **The chosen design**: independent scaling and failure isolation with one codebase and one database |
| 7 | Option C — service-oriented | [§7.3](06-architecture-options.md) | The topology we rejected, and what it would have cost in operational attention |
| 8 | Option D — event-driven | [§7.4](06-architecture-options.md) | Why eventual consistency contradicts the core correctness promise |
| 9 | Service tiers T-1/T-2/T-3 | [§9.1](08-recommended-architecture.md) | That "free" has three distinct meanings with different latency, availability and corpus ceilings |
| 10 | High-level architecture | [§9.2](08-recommended-architecture.md) | Ports as the dependency-inversion seam; one data plane |
| 11 | Container diagram | [§9.3](08-recommended-architecture.md) | Module responsibilities and the sandbox parser's isolation |
| 12 | Document ingestion sequence | [§9.4](08-recommended-architecture.md) | Why each ingestion step exists, and what breaks if it is removed |
| 13 | Query / retrieval / answer sequence | [§9.5](08-recommended-architecture.md) | That the ACL predicate is built once and pushed into both retrieval legs; the cache key includes `acl_set_hash` |
| 14 | Hybrid retrieval + ranker chain | [§9.6](08-recommended-architecture.md) | Why RRF (rank-only fusion, no score calibration) and where the rerank gate sits |
| 15 | Citation generation and verification | [§9.7](08-recommended-architecture.md) | That citation verification is a **deterministic span check**, not a model judgement |
| 16 | Cache flow | [§9.8](08-recommended-architecture.md) | Why index version and ACL set hash are cache-key components |
| 17 | Document lifecycle state machine | [§9.9](08-recommended-architecture.md) | The state transitions that make "retrievable" mean *in force right now* |
| 18 | Async retry / DLQ flow | [§9.9](08-recommended-architecture.md) | Retry classification, jitter, and the DLQ path |
| 19 | Hybrid retrieval & reranking policy | [§11.4](10-rag-architecture-options.md) | That reranking is gated on a measured ΔnDCG threshold, with structural ranking always on |
| 20 | Hallucination-control layers | [§11.6](10-rag-architecture-options.md) | Four layers, and why measurement (layer 4) is CI-only rather than in the request path |
| 21 | STRIDE threat model | [§13.1](12-threat-model.md) | The attack surface across entry points, processing, model runtime and data |
| 22 | Failure domains | [§14.5](13-reliability-model.md) | Per-component failure and recovery; the irreducible Postgres dependency |
| 23 | Degradation ladder L0–L5 | [§14.7](13-reliability-model.md) | The order the system degrades in, and the invariant that holds at every level |
| 24 | Observability architecture | [§15.1](14-observability-model.md) | The collector as the single redaction and cardinality-control point |
| 25 | Entity-relationship diagram | [§16.1](15-data-model.md) | The conceptual data model and cardinality |
| 26 | Embedding lifecycle | [§16.4](15-data-model.md) | Shadow-index build and atomic swap; no mixed-generation index |
| 27 | Evaluation change-measurement loop | [§12.8](11-rag-quality-strategy.md) | Before/after measurement with an explicit adopt-or-revert decision |
**Count: 27 Mermaid diagrams delivered** (verified by counting `mermaid` code blocks in the tree),
covering 12 of the 30 categories in the brief, with the remainder scheduled below. The error-budget
policy in [§14.4](13-reliability-model.md) is a table plus a consequence rule rather than a diagram,
and is deliberately not counted as one.

---

## 23.2 Diagrams scheduled, with owning phase

| Category from the brief | Status | Owning phase | What it will explain |
|---|---|---|---|
| Database schema / physical ER | 📋 | Phase 3 | The physical schema, partitioning and invariants as constraints |
| Document **update** flow (version supersession, atomic switch) | 📋 | Phase 3 | The atomic version switch and concurrent-publish handling (narrative exists in Journey E) |
| Document **deletion** flow (purge, retention, legal hold) | 📋 | Phase 3 | Retention matrix and provable deletion |
| Authentication flow (OIDC) | 📋 | Phase 5 | Token acquisition, refresh rotation, fail-closed behaviour |
| Authorization flow (RBAC+ABAC → ACL predicate) | 📋 | Phase 5 | Policy evaluation → the denormalised ACL projection → the filter predicate |
| Embedding pipeline | 📋 | Phase 2 | Stage-level flow with throughput and the changed-chunk-only rule |
| Indexing pipeline | 📋 | Phase 3 | Chunk → embed → vector + FTS write in one transaction |
| RAG pipeline (end-to-end) | 📋 | Phase 5 | The default path from question to cited answer, with the optional stages marked |
| Query rewriting / decomposition flow | 📋 | Phase 7 | Only if the benchmark admits them |
| Context assembly / token budget flow | 📋 | Phase 5 | Budget, dedupe, parent-section enrichment, truncation disclosure |
| **Deployment architecture** (T-1/T-2/T-3) | 📋 | Phase 12 | The actual deployment topology with volumes, health checks and egress |
| **CI/CD pipeline** | 📋 | Phase 1 | Stages, gates, artifacts, and which gate blocks which |
| Security boundary — per-request flow | 📋 | Phase 8 | Every hop a request makes, annotated with the control that governs it |
| **Disaster recovery architecture** | 📋 | Phase 11 | Backup topology, restore order, and the integrity verification after restore |
| **Scaling architecture** (10⁸ chunks) | 📋 | Phase 13 | Tenant-hash sharding, per-shard capacity, read fan-out |
| **Future multi-region architecture** | 📋 | Phase 13 | Consistency trade-offs for active-active, and what breaks |
| Data-flow diagram (logical, all flows) | 📋 | Phase 5 | Every data flow on one page, for reviewers |
| Request lifecycle | 📋 | Phase 5 | Cross-cutting view with all control points |
| Threat model — STRIDE per component detail | 📋 | Phase 8 | Component-level STRIDE tables as the design is built |
| Load-test / capacity diagram | 📋 | Phase 10 | Test topology and what it measures |

---

## 23.3 Diagram conventions

| Convention | Reason |
|---|---|
| Mermaid, never a binary image | Diffable, reviewable, renderable in-repo |
| Every diagram has a caption stating the decision it explains | Enforces the no-decoration rule |
| Trust boundaries drawn as explicit subgraphs with a crossing table | Boundary mistakes are the top LLM security failure |
| Degraded paths shown with the **same** visual weight as the happy path | The degraded path is the one that ships |
| No diagram without a linked requirement or ADR | Traceability |
| Data flows annotated with the *reason* for the step, not just the step | Principle E: the "why" |
| State machines for every lifecycle | Half the bugs in ingestion systems are state bugs |
| Sequence diagrams for the two critical flows (ingest, query) | Concurrency reasoning is the hard part of both |

## 23.4 Reviewing the diagrams

A useful review technique, and the reason captions are mandatory: **cover the captions and ask an
engineer what each diagram is for.** If they cannot say, the diagram is decoration and should be
deleted rather than defended. Applied to this register, all 27 diagrams should be answerable —
and the ones that fail that test are the ones to cut.