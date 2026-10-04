# PHASE 0 OUTPUT SUMMARY

**Project:** Intelligent University Knowledge Assistant
**Phase:** 0 — Architecture Discovery
**Date:** 2026-10-04
**Status:** Architecture discovery complete. **No application code exists.**
**Purpose of this document:** to let a senior/AI engineering reviewer evaluate the architecture
independently, without access to the conversation that produced it.

---

## 1. What was decided

| # | Decision | Summary | Record |
|---|---|---|---|
| D-1 | **System topology** | Modular monolith with asynchronous workers: one codebase, one PostgreSQL database, two process roles (`api` stateless; `worker` scaled on queue depth) | [ADR-001](docs/04-adrs/ADR-001-modular-monolith-with-asynchronous-workers.md) |
| D-2 | **Single data store** | PostgreSQL + pgvector holds documents, versions, chunks, vectors, lexical index, job queue and audit log. One ACID transaction spans publication | [ADR-002](docs/04-adrs/ADR-002-postgresql-pgvector-as-the-single-data-store.md) |
| D-3 | **Provider isolation** | 11 ports defined in the domain layer (LLM, embedding, reranker, vector store, lexical search, parser, object store, auth, queue, cache, clock/id) with adapters selected by configuration; CI-enforced import boundaries | [ADR-004](docs/04-adrs/ADR-004-provider-ports-and-adapters.md) |
| D-4 | **Hybrid retrieval, RRF fusion** | PostgreSQL FTS + pgvector ANN in parallel, fused with Reciprocal Rank Fusion (k=60), under one identical ACL/temporal predicate | [ADR-005](docs/04-adrs/ADR-005-hybrid-retrieval-with-rrf-and-gated-reranking.md) |
| D-5 | **Reranking gated on measurement** | Cross-encoder reranking off by default. FLOPs analysis: a 278 M cross-encoder costs ~33 s/query on 4 ARM cores. Structural reranker (microseconds, explainable) is the default. Cross-encoder admitted only if ΔnDCG@10 ≥ 0.02 and only with a GPU | [ADR-005](docs/04-adrs/ADR-005-hybrid-retrieval-with-rrf-and-gated-reranking.md), [EJD-003](docs/ENGINEERING_JOURNAL.md) |
| D-6 | **Extractive answering is a first-class mode** | Search-only and extractive cited answers are a product mode (FR-024), the default on free tiers, and 29× faster than generation (~195 ms vs ~5,740 ms). For regulations, quoting the correct clause is often the *correct* answer | [EJD-004](docs/ENGINEERING_JOURNAL.md) |
| D-7 | **Authorisation at the retrieval layer** | The ACL predicate is built once at authorisation time and pushed into both index scans. Post-generation filtering is documented as a prohibited pattern. Cache keys include `acl_set_hash`; Postgres RLS is an independent second control | [ADR-007](docs/04-adrs/ADR-007-authorisation-enforced-at-the-retrieval-layer.md) |
| D-8 | **Content-addressed idempotent ingestion** | Identity from content hash, immutable versions, idempotent at every boundary, only changed chunks re-embedded, one transaction for chunk+FTS+vector write, DRAFT→publish gate, quarantine with reason | [ADR-008](docs/04-adrs/ADR-008-content-addressed-idempotent-ingestion.md) |
| D-9 | **Free-first with three honest service tiers** | T-1 laptop / T-2 free cloud VM / T-3 production-scale, each with its own SLOs. Free-tier limits (≈99 % availability, 24 h RPO, ~60 s generated answers, 1.3–4.5 chunks/s embedding) stated as limits | [ADR-006](docs/04-adrs/ADR-006-free-first-deployment-target.md) |
| D-10 | **Three service tiers + 20 assumptions registered** | Every load-bearing number is DERIVED, ASSUMED or MEASURED. Nothing is asserted without a category | [§4](docs/phase0/03-scale-model.md) |
| D-11 | **Temporal/supersession correctness as the core promise** | A default query never retrieves a superseded version; `as_of` queries do; publish is a verified atomic pointer flip | [Journey G](docs/phase0/04-user-journeys.md#journey-g--user-asks-about-an-outdated-policy) |
| D-12 | **No agents, no tools, no LLM agency** | Architecture decision and security control. Removes most of the OWASP LLM attack surface rather than mitigating it; encoded in the `LLMProvider` port signature | [EJD-007](docs/ENGINEERING_JOURNAL.md), [BND-1](docs/phase0/05-system-boundaries.md) |
| D-13 | **Numeric admission tests for RAG techniques** | Every advanced technique (20 assessed) has a measurement that must be met before entering the default path. 9 of 20 are in the default path | [§11.1](docs/phase0/10-rag-architecture-options.md) |
| D-14 | **Security-aware chunk sizing** | 400 tokens / 50 overlap, bound to the embedding model's context window, because a 512-token encoder silently truncates an 800-token chunk | [EJD-005](docs/ENGINEERING_JOURNAL.md) |

## 2. What was rejected, and why

| Rejected | Reason | Revisit trigger |
|---|---|---|
| **Synchronous modular monolith** (Option A) | Fails PERF-005/006 (a 25 MB PDF takes minutes in-request), NFR-007 (no backpressure), OPS-005 (no DLQ), SEC-007 (parser in-process with DB credentials) | Read-only small corpus |
| **Service-oriented architecture** (Option C) | Operational *attention* cost for 8 deployables with one maintainer; network latency consumes the retrieval budget; no benefit at 133 QPS sustained | > 100 QPS sustained, or a second team, or GPU/CPU hardware split |
| **Event-driven services** (Option D) | Eventual consistency contradicts FR-009 (never answer with a superseded rule); highest operational weight; exactly-once is unachievable and idempotency is already had | Cross-service auditability; many event consumers |
| **Redis / any external cache** | New failure mode and credential for a 133 QPS workload; a cache outage must not be an outage | Hit rate < 30 % across ≥ 3 replicas, or > 200 QPS |
| **Kubernetes, Kafka/Redpanda, service mesh** | No requirement demands them. Resume theatre | §7.7 triggers |
| **Dedicated vector database** | Breaks ACID across publication; second backup; second failure mode | Corpus > ~10⁸ chunks, or measured recall degradation |
| **Elasticsearch/OpenSearch** | Second store; memory profile unfriendly to a free tier | Lexical Recall@20 < 0.85 (a BM25-in-Postgres extension is the cheaper first attempt) |
| **Cross-encoder reranking by default** | ~33 s/query on the target hardware | ΔnDCG@10 ≥ 0.02 **and** GPU available |
| **LangChain / LlamaIndex** | Abstract away the stage-level behaviour we must measure; couple the domain to a fast-moving dependency | Pipeline gains stateful complexity these libraries demonstrably reduce |
| **Agents / tool use** | No requirement; and the absence of agency is a security control | A genuine requirement + security review |
| **Semantic chunking, context compression, ColBERT, knowledge graph** | Each adds cost or a second index without a demonstrated gain | The numeric admission test in §11.1 |
| **Fine-tuning** | No requirement; retrieval is not yet shown to be the bottleneck | Measured evidence that generation quality, not retrieval, is the limit |
| **Models requiring `trust_remote_code`** | Executes unreviewed remote code at load time — a supply-chain inversion | Never, for serving-path models |
| **Auto-publish on successful parse** | Removes the human gate that catches metadata errors and poisoned content | Trusted-source classes, with a mandatory post-publication audit sweep |
| **URL ingestion in the MVP** | Highest SSRF risk, lowest value | OQ-007 |
| **Cache-only answers when the database is down** | Serving stale policy answers is equivalent to the supersession bug. Honest 503 is the correct failure | Never |
| **Hosted LLM/embedding by default** | Data egress; cost at scale; lock-in | Opt-in per tenant, with a data-classification check |

## 3. Why these decisions (the short form)

1. **The problem's hard part is not retrieval — it is authority and time.** Policy documents are
   versioned, permissioned and temporally scoped. A system that retrieves the lexically best chunk
   regardless of whether it has been superseded is wrong exactly when it matters. Every major decision
   serves versioned, permissioned, time-scoped knowledge.
2. **Correctness above capability.** Atomic publication, immutable versions, authorisation before
   generation, and abstention are all worth more than a better answer, because a confidently wrong
   answer about university policy causes real harm.
3. **The free-tier hardware constrains the architecture, so we measured it rather than guessed.** Two
   findings changed the design materially: a standard cross-encoder reranker is infeasible on CPU
   (~33 s/query), and a free 4-core VM cannot serve interactive generated answers (~60 s). The response
   was to make the cheap, often-better path first-class — not to hide the limitation behind a paid API.
4. **Complexity is admitted only with evidence.** Twenty RAG techniques assessed, nine admitted, each
   with a numeric admission test. Every rejected technology has a named revisit trigger.
5. **The seams are kept so migration is mechanical.** Extraction order is pre-agreed (embedding worker →
   indexer → retrieval → query/answer), ports exist before any commercial evaluation, and the same
   container image runs all three tiers.
6. **Measurement is the deliverable.** The evaluation harness gates CI from Phase 4; the engineering
   journal records expected-vs-measured impact for every change.

## 4. Biggest assumptions

| ID | Assumption | If wrong | Sensitivity |
|---|---|---|---|
| **ASM-008** | Text extraction yield = 6 % of raw bytes | At 15 %, chunks rise 2.5×; at 30 % (DOCX-dominant) ~5× → crosses the sharding threshold | **Highest.** Measured in the first task of Phase 2 |
| **ASM-004** | Peak concurrency = 2 % of DAU | At 10 % (exam period), peak QPS rises 5× to ~833 | High |
| **ASM-009** | Document churn = 2 %/day | At 10 %, daily re-embedding becomes 2.2 M chunks — infeasible on CPU | High |
| **ASM-014/015/016** | Hardware effective FLOPS (120 / 600 / 40,000 GOPS) | All throughput numbers shift; the free-tier embedding budget is the sensitive one | High; measured in Phase 10 |
| **A-01** | Corpus is machine-generated text, not scans | OCR pipeline required; ingestion 10–50× slower; changes Phase 2 | High (OQ-004) |
| **A-07** | One active version per document + supersession chain | Parallel in-force variants need a variant dimension | Medium (OQ/assumption A-07) |
| **A-05** | Answers must cite a document *version*, not a document | Audit model (UC-10) becomes impossible | Medium |
| **ASM-001/002** | 20 institutions × 50,000 users | Corpus and load scale linearly | Low–Medium |

**Corpus is modelled at 2 M documents / 22 M chunks.** The brief's "billions of chunks" is
**arithmetically inconsistent** with "millions of documents" under a realistic university document
profile: 10⁹ chunks ÷ 11 chunks/document ≈ **91 M documents** (45× the corpus). The larger regime is
treated as an order-of-magnitude extrapolation whose only architectural consequence is that the index
must not be indivisible — so the chunk table is partitioned from day one, when partitioning is cheap
[§4.0](docs/phase0/03-scale-model.md#40-reconciling-the-briefs-scale-target-with-reality).

## 5. Biggest risks

| # | Risk | Why it is the biggest | Handling |
|---|---|---|---|
| 1 | **Residual hallucination** — a fluent, uncited, unsupported claim | Inherent to the technique; no amount of prompting makes it impossible. An answer about policy that is wrong is worse than no answer | Five defence layers; deterministic citation verification (free, exact); abstention gate; UI separates cited from uncited claims; continuous measurement with an alert on shift. **Stated honestly as a residual risk rather than claimed eliminated** |
| 2 | **Cross-tenant data leakage** (THR-001/002) | Highest-severity failure. In an LLM system the leak is *paraphrased into a fluent answer with citations*, so it is both more useful to an attacker and harder to notice | ACL pushed into both index scans; RLS as an independent control; cache keyed on ACL set; exhaustive negative test suite; **alerting at any non-zero count** |
| 3 | **Evaluation validity** (RISK-021/022/016) | Author-written questions, a possibly-synthetic corpus and no real traffic mean quality claims are weaker than they look | 13 adversarial slices; per-slice reporting; no-regression guard metrics; benchmark changes reviewed like code; weaknesses documented |
| 4 | **Free-tier generative latency** | ~60 s per generated answer on 4 ARM cores — 7× over PERF-004 | Extractive-first by design; measured; labelled "slow mode"; T-1 laptop as the good-UX floor |
| 5 | **Silent retrieval-quality regression** | Nothing crashes; rankings just get worse | Canary recall probe; nightly per-slice eval; CI retrieval gate; provenance reconciliation |
| 6 | **Scope creep** ("production-grade" expands indefinitely) | The most likely way this project fails is by never finishing | Phased gates with exit criteria; "reject with a trigger" discipline; definition of done = a reviewer reaches a cited answer in 15 minutes |

## 6. Most important unresolved questions

**Blocking Phase 1** (defaults are documented and defensible if unanswered):

| ID | Question | Default if unanswered |
|---|---|---|
| **OQ-001** | Licence for the repo and model weights | MIT for code; model weights keep upstream licences; corpora excluded unless redistributable |
| **OQ-004** | **Corpus profile: scans, formats, languages, text yield** | English, machine-generated text, 6 % yield. *Highest-leverage unknown* |
| **OQ-005** | Is a hosted LLM API acceptable (prompts leave our infrastructure)? | No. Local-only; extractive is the default answer mode |
| **OQ-006** | Is free-tier GPU notebook compute acceptable for bulk embedding? | No. Embed locally; size the demo corpus to fit |
| **OQ-002/003** | Scale target of record; peak concurrency profile | Modelled values stand, labelled as assumptions |

**Non-blocking, with defaults:** URL ingestion (deferred), ACL granularity (institution-readable),
benchmark corpus licensing (synthetic with documented bias), answer retention (90 days), multilingual
(English-first, multilingual-capable model), frontend commitment (React), policy engine (in-house),
OCR (out of scope), structured-data depth (relational + semantic), feedback UX (one click),
T-2 provider (always-free ARM VM with a durable volume).

**For the reviewer to answer directly** ([§22.3](docs/phase0/21-open-questions.md)): is the modular
monolith the right call; are the decision-matrix weights defensible (Cost+Operability = 0.26 combined);
is an extractive default the right product call; is a no-LLM default path acceptable; is refusing
cross-encoder reranking correct; is evaluation-at-Phase-7 correctly ordered; is 99 % availability on T-2
an acceptable published claim.

## 7. Recommended architecture

```
Web client → Reverse proxy (TLS, rate limit, size limit)
                    │
        ┌───────────┴────────────┐
        │   api  (N replicas)    │  stateless; scales on read traffic
        │  authn · authz → ACL   │
        │  query · retrieval     │  hybrid FTS+ANN, RRF, structural rerank,
        │  answer · citations     │  dedupe, extractive | generative, abstain
        │  admin · upload · jobs  │
        └───────────┬────────────┘
                    │  (all provider access behind ports)
        ┌───────────┴────────────┐
        │  worker (M replicas)   │  scales on queue depth
        │  validate ▸ sandbox-parse ▸ structure ▸ normalise                     │
        │  ▸ chunk ▸ embed(changed only) ▸ index(1 txn) ▸ publish              │
        └───────────┬────────────┘
                    │
    PostgreSQL + pgvector  ·  Object store (private, signed URLs)
    (docs · versions · chunks · vectors · FTS · queue · audit)
```

- **Query path**: authenticate → build ACL predicate → cache lookup (key includes index version + ACL
  set hash) → embed query → **both retrieval legs under the identical predicate** → RRF → hard filters
  (tenant ∧ ACL ∧ PUBLISHED ∧ effective window ∧ not superseded) → structural rerank → dedupe →
  context assembly → grounding gate → extractive (default) or generative → **deterministic citation
  verification** → persist answer record + retrieval trace → stream.
- **Extraction order if scale arrives**: embedding worker → indexer → retrieval → query/answer. Admin
  stays in the monolith. Already supported by rules R1–R8.
- **Degradation ladder**: L0 full → L1 no rerank → L2 extractive (default on free) → L3 lexical only →
  L4 cache only → L5 honest 503. Invariant at every level: **no answer without retrieved, authorised,
  temporally-valid, citation-resolvable evidence.**
- **Escalation triggers (pre-agreed)**: > 100 QPS; > 10⁸ chunks; GPU needed; multi-region;
  regulatory isolation; second team.

## 8. Recommended technology direction

| Area | Choice | One-line reason |
|---|---|---|
| Backend | Python 3.12 + FastAPI, multi-process | The domain *is* AI; parsers and model tooling are Python-native. GIL is off the critical path |
| Frontend | React + TypeScript + Vite | Streaming answer UI with citation anchors is a reactive-client problem. API contract stays UI-independent |
| Database | **PostgreSQL 16 + pgvector** | One engine ⇒ ACID across publication ⇒ the correctness story holds |
| Lexical search | PostgreSQL FTS (`tsvector`); evaluate `pg_search` BM25 | Same predicate as the dense leg; no second store |
| Vector | pgvector HNSW, fp16, shadow-build + atomic swap | Sufficient to ~10⁸ chunks; `VectorStore` port for later |
| Object store | S3-compatible (MinIO local), signed URLs | Standard protocol; provider swappable |
| Queue | PostgreSQL job table (`FOR UPDATE SKIP LOCKED`) + DLQ | Claim + result in one transaction ⇒ exactly-once *effect* |
| Cache | In-process TTL, keyed on tenant + query + index version + ACL hash | Zero infrastructure; correctness by key design |
| Embedding | Bulk: `multilingual-e5-small` (384-d, 512 ctx) on CPU; `bge-m3` (1024-d) on GPU. Query-time may use a larger model (18 ms is affordable; 57 days is not) | Throughput is the binding constraint |
| Chunking | Structural, 400 tokens / 50 overlap, versioned, bound to the model | 512-token encoders silently truncate 800-token chunks |
| LLM | Local `llama.cpp` (Qwen2.5 1.5B/3B Q4), opt-in slow mode on free tiers | Zero data egress; no cost; local quality limits stated |
| Reranker | Structural (default, explainable); cross-encoder flag-gated on GPU | 33 s/query on CPU |
| Parsers | poppler / mammoth / readability / native, all sandboxed | Libraries parse attacker-controlled binaries |
| Auth | OIDC 2.1 (self-hosted Keycloak/Authentik); in-house RBAC+ABAC in the domain | Never build password storage; policy as a pure function |
| Observability | OpenTelemetry → Prometheus + Grafana + Loki + Tempo; self-hosted | Vendor-neutral; the collector is the single redaction point |
| Testing | pytest + Hypothesis + real Postgres; Locust/k6; Playwright | A mock Postgres cannot tell us whether the index returns the right documents |
| Deployment | Docker Compose (all tiers) + GitHub Actions | Same image, three configurations |

**Explicitly not in the stack:** Kubernetes, Kafka/Redpanda, Redis, Elasticsearch, a dedicated vector
DB, LangChain/LlamaIndex, an agent framework, any hosted LLM by default, any model needing
`trust_remote_code`. Each with a named revisit trigger in
[§10.17](docs/phase0/09-technology-evaluation.md#1017-decision-summary--what-is-not-in-the-stack-and-why).

## 9. Next phase

**Phase 1 — Repository and development foundations.** No RAG logic yet.

Deliverables: module-boundary-enforced layout with a CI import lint rule; typed validated config;
Postgres + pgvector in Compose; liveness vs readiness with *different* semantics; versioned structured
logging with a PII-redaction test; OTel tracing end to end; migration framework; OpenAPI generation
with a CI contract diff; test harness with property-based tests and a real database; CI gates
(typecheck, lint, unit, integration, secret scan, dependency audit, SBOM); `make up` → working system
in < 5 minutes; 11 port definitions with fake and real adapters; ADR-009…012.

Exit criteria: `make up` healthy; a deliberate boundary violation fails CI; zero `any` in domain code;
redaction test passes; migration up/down succeeds; RLS tenant isolation proven on a stub schema.

Then: **Phase 2 begins with corpus measurement, not pipeline code** — a 100+ document profiler whose
output replaces ASM-004/007/008, and the benchmark corpus selection (OQ-009).

## 10. Exact prerequisites for beginning implementation

**Blocking (must be answered or defaulted explicitly before the first commit):**

1. **OQ-001 licence decision** — repository licence; model-weights licensing; whether corpora may be
   redistributed.
2. **OQ-004 corpus access** — a 100+ document real sample for profiling. *If unavailable, the entire
   scale model rests on ASM-008 with 2.5–5× uncertainty.*
3. **OQ-005 hosted-API policy** — whether any third-party model API may ever receive document text.
4. **OQ-006 free GPU acceptability** — for burst bulk embedding.
5. **Confirmation or override of the 12 defaults** in
   [§22.5](docs/phase0/21-open-questions.md#225-the-decisions-i-made-on-the-owners-behalf-flagged-for-confirmation).
6. **Reviewer sign-off** on the [§23 checklist](docs/phase0/22-architecture-review-checklist.md) §23.1–23.13.
7. **Runtime decisions for the implementer**: target language version (3.12), Postgres 16 + pgvector,
   Python dependency manager, and whether the T-2 provider is selected now or at Phase 12 (OQ-020).

**Available and sufficient to start immediately:**

- Requirements: 99 with acceptance criteria and verification methods; every MUST traced to a journey.
- 8 ADRs with alternatives, consequences, tradeoffs and migration paths.
- Conceptual data model + 15 testable integrity invariants (DI-01…DI-15).
- 10 user journeys including injection, retrieval outage and LLM outage.
- Security: trust boundaries, 24 threats with residual risk and tests, 10 ADRs of controls.
- Reliability: 10 SLIs, tier-specific SLOs, error budget policy, RTO/RPO, 6-level degradation ladder.
- Observability: metric/span/alert/dashboard catalogues mapped to SLOs.
- Evaluation: 13 dataset slices, metric definitions with targets, CI gates with tolerances.
- Scale model: derivations, 20 assumptions with sensitivity, and a named measurement plan.
- 27 diagrams + a register mapping each to the decision it explains; 38-risk register; 14-phase roadmap.

**Not available (and must not be assumed):** a benchmark corpus; measured hardware throughput; any
measured quality number; a working system. Every figure that would imply otherwise is labelled `ESTIMATE`
with the phase that must measure it.

---

## 11. What a reviewer should challenge first

Ranked by leverage, not by section order:

1. **[§4 scale model](docs/phase0/03-scale-model.md)** — attack ASM-004, ASM-008, ASM-009 and the
   FLOPs-based throughput estimates. These invalidate the storage decision, the freshness SLO and the
   reranking decision if wrong.
2. **[§8 decision matrix](docs/phase0/07-decision-matrix.md)** — the weights. Note that §8.6 adds a
   non-compensatory **veto test**, because a weighted average can mask a fatal flaw: Option A is
   eliminated by requirement failure, not by points.
3. **[§11.1 technique admissions](docs/phase0/10-rag-architecture-options.md)** — is refusing semantic
   chunking, context compression and cross-encoder reranking by default engineering judgement or
   timidity? The FLOPs arithmetic is given so this is checkable rather than a matter of taste.
4. **[§6.5 tenant isolation](docs/phase0/05-system-boundaries.md)** — is shared-database-with-RLS the
   right choice, and are the escalation triggers the right ones?
5. **[§13 threat model](docs/phase0/12-threat-model.md)** — is "no agency" doing too much work? If a
   reviewer believes the threat model over-relies on that single decision, [§13.5](docs/phase0/12-threat-model.md)
   identifies the three accepted residual risks that follow from it.
6. **[§22.5 defaults](docs/phase0/21-open-questions.md)** — any of the 12 could be wrong, and each lists
   its override.