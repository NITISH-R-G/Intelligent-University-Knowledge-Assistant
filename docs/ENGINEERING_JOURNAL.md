# Engineering Journal

**Purpose.** A dated record of what changed, why, what else was considered, what evidence existed at the
time, and — most importantly — whether the expected impact matched the measured impact.

The most valuable entries are the ones where **expected ≠ measured**. Those entries are where the model
of the system was falsified, and they are the reason this journal exists. An engineering history that
contains only successes is either a project that never measured anything or a project that deleted its
measurements.

**Entry fields:** Date · Change · Reason · Alternatives · Evidence · Tradeoff · Expected impact ·
Measured impact · Status · Linked ADR/requirements

---

## EJD-001 — 2026-10-04 — Architecture discovery completed; no code written

| Field | Value |
|---|---|
| **Change** | Produced the Phase 0 architecture package: 23 sections, 8 ADRs, 27 diagrams, 99 requirements, 24 threats, 38 risks, 20 open questions |
| **Reason** | Principal-engineer discovery: no significant implementation without specification, justified decisions, modelled threats and defined failure behaviour |
| **Alternatives** | (a) Start with code and document afterwards — rejected: architecture that has not survived review should not be encoded. (b) Write a lighter package — rejected: the brief's hard parts (multi-tenancy, versioning, security, evaluation) cannot be responsibly compressed |
| **Evidence** | Decision matrix with three sensitivity re-weightings; scale model with derivations; STRIDE + OWASP LLM mapping; cross-reference check that every MUST requirement appears in a user journey |
| **Tradeoff** | ~30 files of documentation before any code. This is a real time investment and is the largest single cost of the project's first phase |
| **Expected impact** | Reviewer can evaluate the engineering independently of the code; later phases have acceptance criteria from day one; design decisions are traceable |
| **Measured impact** | Pending — measure at the Phase 1 review whether the package prevented rework. **This is the test of whether Phase 0 was worth it** |
| **Status** | Complete |
| **Linked** | [ADR-001…008](04-adrs/), [§22 open questions](phase0/21-open-questions.md), [§23 checklist](phase0/22-architecture-review-checklist.md) |

---

## EJD-002 — 2026-10-04 — Rejected the "billions of chunks" target as inconsistent with "millions of documents"

| Field | Value |
|---|---|
| **Change** | Set the architectural stress target to **20 institutions / 2 M documents / 22 M chunks**, and documented that 10⁹ chunks requires ~91 M documents under the modelled profile (45× the corpus) |
| **Reason** | The brief asked for both "millions of documents" and "billions of chunks". Under a realistic university document profile (11 chunks/document) those two figures are mutually inconsistent by ~45× |
| **Alternatives** | (a) Repeat the brief's numbers and quietly pick parameters to fit — rejected as fabrication. (b) Assume tiny documents to make both fit — rejected as unrepresentative. (c) State the inconsistency, model one regime honestly, and treat the other as an extrapolation — chosen |
| **Evidence** | Derivation in [§4.0](phase0/03-scale-model.md#40-reconciling-the-briefs-scale-target-with-reality): 10⁹ ÷ 11 chunks/doc = 90.9 M documents |
| **Tradeoff** | The headline scale number is lower than the brief requested. The architectural response (tenant-partitioned, shardable index) still satisfies the larger regime |
| **Expected impact** | The design will not embed an assumption that the index is indivisible — partition keys are chosen now, at 22 M rows, where they are cheap |
| **Measured impact** | Pending Phase 3 (partitioning) and Phase 13 (sharding design) |
| **Status** | Decided; verify when corpus measurements land in Phase 2 |
| **Linked** | [§4.0](phase0/03-scale-model.md), [ADR-002](04-adrs/ADR-002-postgresql-pgvector-as-the-single-data-store.md) |

---

## EJD-003 — 2026-10-04 — Discovered that a conventional cross-encoder reranker is unusable on target hardware

| Field | Value |
|---|---|
| **Change** | Cross-encoder reranking **off by default**; admitted only on GPU tiers behind a feature flag, gated on a measured ΔnDCG@10 ≥ 0.02. Added an explainable structural/lexical reranker as the default ranker |
| **Reason** | FLOPs analysis: `bge-reranker-base` (278 M) at 15 pairs × 400 tokens = 3.34 TFLOP/query → **~33 s on 4 ARM cores, ~5.6 s on a developer laptop**, ~83 ms on an A10G |
| **Alternatives** | (a) Include the reranker as the industry-standard practice — rejected: it would breach the latency SLO by an order of magnitude on the tier the system is designed for. (b) Use a MiniLM-scale cross-encoder (~450 ms on a laptop, 2.3 s on free CPU) — rejected as the default for the same reason, retained as an option. (c) No reranking at all — rejected: ordering quality matters, and a free structural reranker costs microseconds |
| **Evidence** | [§4.4](phase0/03-scale-model.md#44-reranking--infeasible-on-cpu-so-it-is-not-a-default); the admission test in [§11.1](phase0/10-rag-architecture-options.md) |
| **Tradeoff** | We forgo some ranking quality to stay within a usable latency budget. If RRF + structural ranking already reaches Recall@20 ≈ 0.95, the forgone quality may be negligible — **which is exactly what Phase 7 must measure** |
| **Expected impact** | p95 stays near 200 ms for the extractive path on all tiers; ranking quality is close to the reranked variant |
| **Measured impact** | Pending Phase 7 (nDCG@10 with and without reranking, per slice). **If the gap is < 0.02, the cross-encoder should be removed from the design entirely** |
| **Status** | Decided, reversible on measurement |
| **Linked** | [ADR-005](04-adrs/ADR-005-hybrid-retrieval-with-rrf-and-gated-reranking.md), [§11.4](phase0/10-rag-architecture-options.md#114-reranking-policy) |

---

## EJD-004 — 2026-10-04 — Made extractive answering a first-class mode rather than a fallback

| Field | Value |
|---|---|
| **Change** | The retrieval + verbatim-extract path is a **product mode** (FR-024), the default on T-1/T-2, and a first-class evaluation target — not a degradation artefact |
| **Reason** | The extractive path is ~195 ms versus ~5.7 s for generation (**29× faster**). For regulations, timetables and FAQs, quoting the correct clause is frequently the *correct* answer, not a compromise. Free-tier hardware cannot serve generation at interactive latency anyway |
| **Alternatives** | (a) Generation-first with search as an error fallback — rejected: it makes the free tier unusable and treats the cheaper path as degraded when it is often better. (b) Generate first, extract on failure — rejected: it confuses a failure mode with a product mode and produces worse answers on the most common query shapes. (c) Extractive-only forever — rejected: generation adds synthesis value for multi-source questions |
| **Evidence** | Latency budget [§4.6](phase0/03-scale-model.md#46-latency-budget-t-2-gpu-backed-tier); generation feasibility [§4.7](phase0/03-scale-model.md#47-generation-feasibility-on-the-free-tier); Journey J shows the degradation path is an *improvement* |
| **Tradeoff** | Users get excerpts rather than synthesis on free tiers; multi-source questions are less fluent. The UI must be explicit about which mode answered — transparency rather than hiding the difference |
| **Expected impact** | p95 < 900 ms on T-2; correctness unchanged (citation correctness is identical in both paths); cost near zero |
| **Measured impact** | Pending Phase 7: does an extractive answer score as highly as a generated one on the benchmark? If yes, this is the most important finding in the project |
| **Status** | Decided |
| **Linked** | [FR-023, FR-024](phase0/02-requirements.md), [ADR-005](04-adrs/ADR-005-hybrid-retrieval-with-rrf-and-gated-reranking.md), [ADR-006](04-adrs/ADR-006-free-first-deployment-target.md) |

---

## EJD-005 — 2026-10-04 — Tied chunk size to the embedding model's context window

| Field | Value |
|---|---|
| **Change** | Chunk size fixed at **400 tokens with 50 overlap**, bound to and versioned with the embedding model; the index refuses to serve chunks whose provenance does not match the active index generation |
| **Reason** | Most 384-dimension embedding models cap at **512 tokens**. An 800-token chunk would be silently truncated — its tail never embedded, therefore never retrievable, and the loss invisible to every metric except recall |
| **Alternatives** | (a) 800–1,000 token chunks for better context coherence — rejected: silently truncated by 512-token encoders. (b) Adopt a long-context model (`bge-m3`, 8192) to permit large chunks — deferred: 0.22 chunks/s on CPU makes it GPU-only. (c) Chunk per model, i.e. duplicate chunks — rejected: it doubles the corpus for no conceptual gain |
| **Evidence** | Model context windows; the coupling is recorded in [§11.2](phase0/10-rag-architecture-options.md) as constraints C1–C6 |
| **Tradeoff** | Smaller chunks reduce context coherence and increase chunk count (11 chunks/document rather than 6). Accepted because invisible truncation is strictly worse |
| **Expected impact** | Chunk count rises ~1.8× versus 800-token chunks; embedding cost rises proportionally. Recall on the structure-heavy slice should improve because no chunk tail is lost |
| **Measured impact** | Pending Phase 3 (chunk quality) and Phase 7 (segment recall). **Test: compare Recall@10 at 400 vs 800 tokens where the model permits it** |
| **Status** | Decided |
| **Linked** | [FR-013](phase0/02-requirements.md), [§11.2](phase0/10-rag-architecture-options.md) |

---

## EJD-006 — 2026-10-04 — Accepted PostgreSQL's HNSW index build as a serving-risk mitigation problem

| Field | Value |
|---|---|
| **Change** | Index creation **only** via shadow-table build and atomic swap (FR-014); never `CREATE INDEX` on a live large table; index-generation provenance stored per chunk |
| **Reason** | HNSW build on 22 M chunks is a multi-hour, IO-heavy operation. Run against a serving database it degrades query latency — a self-inflicted outage caused by a routine operation |
| **Alternatives** | (a) Build in place during a maintenance window — rejected: needs downtime proportional to corpus size, and any overrun is an outage. (b) Build on a replica and swap — rejected: replication of a partially built index is more complex than a shadow table. (c) Build in a shadow table and swap atomically — chosen |
| **Evidence** | [§4.2](phase0/03-scale-model.md#42-corpus-derivation) storage estimates; [§14.5](phase0/13-reliability-model.md) failure analysis; [§16.4](phase0/15-data-model.md) embedding lifecycle |
| **Tradeoff** | Doubles temporary storage during a build; adds a swap step that must be verified before cutover |
| **Expected impact** | Zero measurable query impact during reindex; reindex duration unchanged; disk headroom needed |
| **Measured impact** | Pending Phase 10 (build under load) |
| **Status** | Decided |
| **Linked** | [ADR-008](04-adrs/ADR-008-content-addressed-idempotent-ingestion.md), [RISK-002](phase0/20-risk-register.md) |

---

## EJD-007 — 2026-10-04 — Chose "no agency" as an architectural security control

| Field | Value |
|---|---|
| **Change** | The system has **no tools, no function calling, no write actions, no network egress from the model runtime**, and no plan to add them |
| **Reason** | Most of the OWASP LLM Top 10 attack surface (indirect prompt injection → data exfiltration → lateral action) requires the model to have a **capability** to act. Removing capability converts these threats from "mitigated" to "not exploitable" |
| **Alternatives** | (a) Agents with retrieval tools — rejected: no requirement demands autonomy, and the value is in trustworthy answers. (b) Tool calling for structured output — rejected: a typed output contract achieves the same without giving the model a capability. (c) Policy/prompt-based injection defence only — rejected: prompts are not a security boundary |
| **Evidence** | [Journey H](phase0/04-user-journeys.md#journey-h--malicious-document-attempts-prompt-injection) defence-layer table; [§13.3](phase0/12-threat-model.md#133-owasp-llm-top-10-coverage); [ADR-004](04-adrs/ADR-004-provider-ports-and-adapters.md) — the no-agency property is encoded in the port signature |
| **Tradeoff** | No autonomous workflows, no multi-step tool use. If a future requirement needs them, **the security model must be re-derived, not extended** |
| **Expected impact** | Injection attacks become survivable by design; residual risk reduces to "wrong but cited" answers, which retrieval-layer controls address |
| **Measured impact** | Pending Phase 8: the injection corpus must show **zero** behaviour change, including multilingual attempts |
| **Status** | Decided. Revisit only with a security review and a genuine requirement |
| **Linked** | [BND-1](phase0/05-system-boundaries.md), [SEC-004](phase0/02-requirements.md), [THR-006/007](phase0/12-threat-model.md) |

---

## EJD-008 — 2026-10-04 — Rejected LangChain / LlamaIndex

| Field | Value |
|---|---|
| **Change** | No RAG framework. The retrieval pipeline is implemented directly, ~5 well-defined stages, with explicit per-stage timing, scoring and evaluation |
| **Reason** | The project's core deliverable is **measured** retrieval quality and **observable** stage-by-stage latency. Both are exactly what a framework abstracts away. The pipeline is small enough that the framework saves a few hundred lines in exchange for making the system depend on someone else's roadmap |
| **Alternatives** | (a) Use a framework for assembly speed — rejected as above. (b) Use a framework for provider abstraction — rejected: [ADR-004](04-adrs/ADR-004-provider-ports-and-adapters.md) ports are ~11 small interfaces and are more predictable than a framework's callback model. (c) Reconsider if the pipeline's complexity grows — the trigger is written down |
| **Evidence** | [§10.17](phase0/09-technology-evaluation.md#1017-decision-summary--what-is-not-in-the-stack-and-why) |
| **Tradeoff** | We own more code, including provider adapters and chunking. In exchange, every behaviour is inspectable and every optimisation is measurable |
| **Expected impact** | More implementation effort in Phase 3–6; full control over measurement and tuning |
| **Measured impact** | Pending. **Revisit trigger: if the pipeline grows stateful complexity that these libraries demonstrably reduce** |
| **Status** | Decided, with a named revisit trigger |

---

## EJD-009 — 2026-10-04 — Declined a model requiring `trust_remote_code`

| Field | Value |
|---|---|
| **Change** | Rejected `jina-embeddings-v3`-class models despite attractive quality/dimension trade-offs; accepted the constraint that every model is a pinned revision converted in a sandbox to ONNX/GGUF |
| **Reason** | `trust_remote_code` executes arbitrary code from the model repository at load time. A model file is untrusted code from the internet, and treating it as trusted inverts the threat model |
| **Alternatives** | (a) Accept the risk with pinning — rejected: a pinned revision still executes unreviewed code. (b) Sandbox model loading — chosen: conversion happens in an isolated step and only the converted artefact is loaded in the serving path |
| **Evidence** | [§10.8](phase0/09-technology-evaluation.md), [THR-022](phase0/12-threat-model.md) |
| **Tradeoff** | Restricted to models that can be converted without remote code; some high-quality models are excluded |
| **Expected impact** | Small measurable quality gap on some benchmarks; a meaningfully smaller AI supply-chain attack surface |
| **Measured impact** | Pending Phase 7 model comparison |
| **Status** | Decided |
| **Linked** | [SEC-012](phase0/02-requirements.md), [ADR-006](04-adrs/ADR-006-free-first-deployment-target.md) |

---

## EJD-010 — 2026-10-04 — Declined a database-level cached-answers design

| Field | Value |
|---|---|
| **Change** | Answer cache is **in-process, keyed including `index_version` and `acl_set_hash`**; explicitly rejected both a durable answer table and a Redis deployment in Phase 1 |
| **Reason** | A durable answer cache creates a staleness-correctness problem in a policy system (serving a superseded answer) for a latency benefit that an in-process cache already provides at the modelled load. Making the cache *safe* requires pushing authorisation into retrieval ([ADR-007](04-adrs/ADR-007-authorisation-enforced-at-the-retrieval-layer.md)) and including the index version in the key |
| **Alternatives** | (a) No cache — rejected: leaves a large latency and cost lever unused. (b) Redis — rejected for Phase 1 on operational-surface grounds, deferred behind `CachePort`. (c) Cache-in-Postgres — rejected as an anti-pattern that pollutes the transactional store |
| **Evidence** | [ADR-003](04-adrs/ADR-003-no-redis-in-phase-1-deferred.md), [§9.8](phase0/08-recommended-architecture.md#98-cache-flow) |
| **Tradeoff** | Lower hit rate with many replicas; cache lost on restart. Acceptable for a cache |
| **Expected impact** | ≥ 30 % hit rate on repetitive policy queries; ~30 % reduction in generation cost at scale; zero added infrastructure |
| **Measured impact** | Pending Phase 5. **Watch for a structural cause of low hit rate: an index version bump on every publish would make the hit rate approximately zero** |
| **Status** | Decided |

---

## EJD-011 — 2026-10-04 — Ordered Phase 7 (evaluation) before generation tuning

| Field | Value |
|---|---|
| **Change** | The full evaluation harness is a **hard gate before production readiness**, and minimal retrieval/citation gates exist in CI from Phase 4 onward |
| **Reason** | Every subsequent decision — model choice, rerank admission, query rewriting, chunk size — has a numeric admission test. Without the instrument, those decisions become opinions, and "it seems to work" is the only available statement |
| **Alternatives** | (a) Evaluation at the end — rejected: by then every decision has been made without evidence and there is nothing to compare against. (b) Evaluation before ingestion — impossible: the corpus and questions must exist. (c) Evaluation early, gating CI from the first retrieval implementation — chosen, with the full harness at Phase 7 |
| **Evidence** | [§12](phase0/11-rag-quality-strategy.md), [roadmap Phase 7](phase0/19-implementation-roadmap.md#208-phase-7--evaluation) |
| **Tradeoff** | The most intellectually demanding phase is deliberately mid-project, delaying visible end-to-end polish. Accepted: a polished system with unmeasured quality is the failure mode this project is built to avoid |
| **Expected impact** | Later optimisation decisions are evidence-based; regressions are caught before merge; every technique addition is justified by a number |
| **Measured impact** | Pending Phase 7. **EJD-004, EJD-003, EJD-005 all resolve here** |
| **Status** | Decided |
| **Linked** | [RISK-031](phase0/20-risk-register.md) |

---

## EJD-012 — 2026-10-04 — Wrote down the decisions taken on the owner's behalf

| Field | Value |
|---|---|
| **Change** | Recorded 12 defaults decided without stakeholder input (monolith topology, no Redis, no Kubernetes, no cross-encoder by default, extractive-first, 400-token chunks, one database, no RAG framework, no agents, URL ingestion deferred, synthetic-corpus acceptable, OTel) with the rationale and the override path for each |
| **Reason** | These questions are resolvable with a defensible default. Leaving them open would have stalled Phase 1; deciding them silently would have hidden them from the owner |
| **Alternatives** | (a) Leave all open — rejected: blocks Phase 1 on decisions nobody needs to deliberate. (b) Decide and document — chosen |
| **Evidence** | [§22.5](phase0/21-open-questions.md#225-the-decisions-i-made-on-the-owners-behalf-flagged-for-confirmation) |
| **Tradeoff** | A default chosen under time pressure may not match owner preference. Each entry lists the one-word override |
| **Expected impact** | Phase 1 can start immediately; the owner can override any default cheaply |
| **Measured impact** | Pending Phase 1 review — did any default need overturning? |
| **Status** | Complete |

---

## Journal conventions (for future entries)

1. **Write the "Expected impact" before the change, not after.** Expected-after-the-fact is a rationalisation, not a prediction.
2. **"Measured impact" is never estimated.** If it has not been measured, write "pending Phase N".
3. **Record negative results.** A technique adopted, measured, and then removed is one of the most valuable entries in the file.
4. **Record decisions reversed.** EJD-003 and EJD-005 are explicitly reversible on measurement; the reversal is a success of the process, not a failure of the decision.
5. **One entry per decision.** Grouped changes hide the individual tradeoffs.
6. **Link the ADR and the requirements.** An entry with no traceability is an anecdote.