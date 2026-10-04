# §20 Implementation Roadmap

**Sequencing principle.** Each phase is only useful if its output makes the *next* phase's decision
possible. The order below is driven by "what do we need to know before we build the next thing", not by
what is easiest or most impressive to build. The most counter-intuitive consequence: **Phase 2 begins
with measurement, not code**, and the evaluation harness arrives at Phase 7, before generation tuning.

---

## 20.1 Phase overview

| Phase | Name | Goal | Primary dependency | Exit gate |
|---|---|---|---|---|
| **0** ✅ | Architecture discovery | Make decisions reviewable | — | Review checklist passes |
| **1** | Repository + foundations | A running, testable, typed skeleton | 0 | `make up` yields a health-checked service |
| **2** | Corpus profiling + ingestion | **Replace assumptions with measurements** | 1 | 500-doc corpus ingested end to end |
| **3** | Knowledge representation | Versioned, chunked, indexed, searchable | 2 | Chunk/vector quality measured; `search` returns correct chunks |
| **4** | Retrieval | Hybrid search with filters and reranking | 3 | Recall@20 ≥ 0.95 on the dev slice |
| **5** | Answering + citations | Extractive answers, abstention, citations | 3 | Citation correctness ≥ 0.97 |
| **6** | Generation (flag-gated) | Generative answers with the output contract | 5 | Groundedness ≥ 0.95; unsupported ≤ 0.02 |
| **7** | Evaluation | The instrument everything else needs | 3,4,5,6 | Full benchmark run with a committed baseline |
| **8** | Security hardening | Prove the controls, not assert them | any | All security suites green |
| **9** | Observability | Make every SLO measurable | any | SLO dashboards live; alerts → runbooks |
| **10** | Performance + load | Validate the capacity model | 4–7 | PERF targets met; measurements replace ASM-014/015 |
| **11** | Production readiness | Operability, backups, DR, incident practice | 8–10 | Restore drill within RTO; runbooks complete |
| **12** | Deployment | Ship the free demonstration tier | 11 | Public demo runs the full journey set |
| **13** | T-3 enablement | The production-scale path | 12 | Scalability + SLO design reviewed |
| **14** | Continuous improvement | Evidence-driven evolution | 13 | Journal shows measured impact for every change |

---

## 20.2 Phase 1 — Repository and development foundations

**Goal.** A running, typed, tested, lint-enforced service skeleton with health checks, structured
logging, migrations, tracing and CI — before any RAG logic exists.

**Depends on.** Phase 0 only.

**Deliverables**
1. Repository layout enforcing module boundaries (domain / application / infrastructure / adapters) with a CI import-boundary lint rule (NFR-002).
2. Typed settings object, validated at startup, failing fast (OPS-014).
3. Postgres + pgvector in Docker Compose, with a connection pool and `statement_timeout` defaults.
4. Health/readiness endpoints with **different semantics** (OPS-001) — a test proves liveness survives a dependency outage and readiness does not.
5. Structured JSON logging with schema v1 and a redaction test (NFR-010).
6. OTel tracing wired end to end with a synthetic span; request-ID propagation (NFR-008).
7. Migrations framework with one applied migration and a documented forward/backward policy (OPS-008).
8. OpenAPI generation from types + a CI contract-diff step.
9. Test harness: unit + property-based scaffolding (Hypothesis), integration with a **real database**.
10. CI: typecheck, lint, unit, integration, secret scan, dependency audit, SBOM.
11. `make up` bringing up a working system in < 5 minutes (NFR-012).
12. Port definitions for all providers, with **at least one fake adapter** and one real adapter each for storage and parser.
13. ADR-009 (module boundary enforcement), ADR-010 (CI gates), ADR-011 (test strategy), ADR-012 (configuration).

**Exit criteria**
- `make up` → healthy service; `/readyz` green; smoke test passes.
- A deliberately introduced dependency-inversion violation **fails CI**.
- Zero `any` in domain code; `mypy` clean.
- Logging redaction test passes (PII-shaped input absent from output).
- ≥ 80 % line coverage on domain logic (coverage of glue is not valuable; coverage of authorisation and versioning logic is).

**Tests.** Health semantics · config validation failure · log redaction · migration up/down · RLS
tenant isolation (a stub schema) · port contract tests for every adapter.

**Docs.** `06-data/migrations`, `10-observability/logging-policy`, ADR-009…012.

**Risks.** Boundary erosion under time pressure (mitigated by the CI lint rule, which is the only real
control); over-engineering the skeleton before requirements are tested in practice.

---

## 20.3 Phase 2 — Corpus profiling and document ingestion

**Goal.** Ingest real documents, and **replace the load-bearing assumptions with measurements**.

**Depends on.** Phase 1.

**Why measurement first.** ASM-004/ASM-008 (document size and text yield) dominate the entire capacity
model. Building the pipeline on an unmeasured assumption and discovering a 3× error at the end would
invalidate [§4](03-scale-model.md) and possibly the storage decision. Therefore the **first deliverable
of Phase 2 is a profiler**, not a pipeline.

**Deliverables**

*Measurement (before code):*
1. **Corpus profiler**: over a 100+ document real sample — size distribution, format mix, page counts, text yield, language distribution, section-structure prevalence, duplicate rate. Output: a report that **replaces ASM-004, ASM-007, ASM-008 with measurements**.
2. **Benchmark corpus selection** under an appropriate licence (`OQ-009`), with the corpus manifest committed.
3. **User-journey question set drafted** (S1–S13 slices) — authored *now*, so the pipeline is built to serve real questions rather than imagined ones.

*Pipeline:*
4. Signed-URL upload flow (FR-046), with content sniffing and quota enforcement (SEC-006, SEC-011).
5. Sandboxed parser for PDF/DOCX/HTML/TXT/MD/CSV (SEC-007).
6. Structural extraction: sections, tables, page map (FR-003).
7. Metadata extraction: title, author, department, dates, doc type, supersedes (FR-004), with a measured F1 on the sample.
8. Normalisation: Unicode NFC, de-hyphenation, boilerplate removal.
9. Job queue with claims, retries, backoff, DLQ, quarantine (OPS-005, FR-011).
10. Idempotency by content hash; duplicate detection (FR-007).
11. State machine + audit records + admin status view.
12. Pipeline stage metrics and tracing ([§15](14-observability-model.md)).

**Exit criteria**
- 500-document corpus ingested; **95 % published, 5 % quarantined with reasons**, zero stuck jobs.
- Measured yield and size distribution reported; [§4](03-scale-model.md) updated with measurements and the deltas from assumptions documented.
- Malformed-input suite green: zip bomb, ELF-as-PDF, traversal filename, oversized file, MIME mismatch.
- Sandbox cannot reach network, credentials or host filesystem (test).
- Duplicate upload creates zero new versions and zero embeddings (test).
- Replaying the entire batch produces a byte-identical index state (test).
- FRESHNESS measured and published per document size.

**Tests.** Parser unit tests per format against fixtures · yield threshold test · quarantine paths ·
idempotency · DLQ and replay · sandbox escape regression · batch replay determinism · quota enforcement.

**Docs.** `06-data/lifecycle`, `05-rag/chunking` (draft), profiling report, updated scale model.

**Risks.** No suitable public corpus available (`OQ-009`) — contingency: synthetic corpus with the
bias documented. Scanned PDFs discovered (`OQ-004`) — contingency: flag for OCR, which is out of scope;
document the limitation rather than silently producing empty chunks.

---

## 20.4 Phase 3 — Knowledge representation

**Goal.** Versioned documents, deterministic chunking, embeddings, and a searchable index with
transactional guarantees.

**Depends on.** Phase 2.

**Deliverables**

1. Physical schema from the [§16](15-data-model.md) conceptual model, with migrations and all **DI-01…DI-15 invariants** implemented as constraints where possible.
2. Document versioning + supersession + as-of semantics (FR-006, FR-009, FR-027).
3. Structural chunker with deterministic chunk IDs (FR-013, C1–C6 in [§11.2](10-rag-architecture-options.md#112-chunking--the-decision-with-the-most-downstream-consequence)); **bound to the embedding model's context window**.
4. `EmbeddingProvider` with a local ONNX adapter and batching; **only changed chunks re-embedded** (FR-008).
5. `chunk` table with pgvector, FTS, and ACL projection.
6. Search-only endpoint with the hard filter stack: tenant ∧ ACL ∧ PUBLISHED ∧ effective window ∧ not-superseded.
7. Index-version machinery with shadow build + atomic swap (FR-014).
8. Delete/withdraw/purge path with the bounded window (FR-010, NFR-011).
9. Admin API + UI for lifecycle operations (FR-047).

**Exit criteria**
- Determinism test: same document + versions → identical chunk IDs.
- Incremental re-embed: a 5 %-changed document re-embeds ≤ 8 % of chunks.
- Chunk-quality report: segment-level recall on the golden set; metadata extraction F1 ≥ 0.85.
- Search returns only PUBLISHED, in-force, authorised chunks (negative tests).
- A superseded version never appears for a default query; appears correctly for an as-of query.
- Purge removes chunks + vectors + object within the window; audit retained.
- Index-version swap completes with **zero failed requests** (load test).

**Tests.** DI-01…DI-15 invariant tests · determinism/property tests (Hypothesis on chunking) ·
supersession cycle rejection · ACL projection reconciliation · rollback atomicity · concurrent publish conflict.

**Docs.** `06-data/schema`, `06-data/partitioning`, `05-rag/chunking`, ADR-008, ADR-009.

**Risks.** Chunking quality lower than assumed → Phase 7 will quantify it; contingency is semantic
chunking (T8) evaluated against the benchmark rather than by intuition.

---

## 20.5 Phase 4 — Retrieval

**Goal.** Hybrid retrieval with fusion, structural reranking, and measured quality.

**Depends on.** Phase 3.

**Deliverables**

1. Lexical leg (FTS; evaluate `pg_search` BM25 as a drop-in) and dense leg.
2. RRF fusion with configurable `k` (FR-020).
3. Structural reranker with recorded reasons (§11.4).
4. Deduplication and boilerplate removal (T12).
5. `RetrievalDiagnostics` exposing candidates, scores, filters, index version (FR-029).
6. Optional lexical-only and vector-only modes for A/B measurement.
7. Per-stage latency instrumentation.

**Exit criteria**
- Recall@10 ≥ 0.90 and Recall@20 ≥ 0.95 on the dev slice.
- Hybrid ≥ max(lexical, dense) on nDCG@10 — **or** a documented finding explaining why not, with the fusion alternative that does win.
- Post-filter candidate drop-off measured and understood.
- p95 hybrid search < 250 ms on the dev corpus.
- Every retrieval decision traceable in the diagnostics output.

**Tests.** FTS-only vs ANN-only vs hybrid comparison · RRF unit tests (including monotonicity properties) ·
filter correctness · per-stage latency assertions.

**Docs.** `05-rag/retrieval-design`, `12-performance/benchmarks`, ADR-005.

**Risks.** FTS recall insufficient for BM25-critical queries → contingency: `pg_search` extension behind
`LexicalSearchPort`, adopted only on measured evidence.

---

## 20.6 Phase 5 — Answering, citations and abstention

**Goal.** The correct, fast, honest product: extractive answers with verified citations and
well-calibrated abstention — **before** adding generation.

**Depends on.** Phase 3 (retrieval from Phase 4 improves quality but is not required for the mode).

**Deliverables**

1. Extractive answer path: passage selection + composition with inline citations (PERF-002 target).
2. Citation construction with document, version, section, page, effective date.
3. **Citation verification** (FR-026): deterministic span check; strip and flag violations.
4. Abstention gate with a **calibrated threshold** (FR-023) — calibrated on the S7 slice, not guessed.
5. Grounding disclosure in responses: `mode`, `degraded`, `citations_verified`, `truncated`.
6. Search-only mode (FR-024).
7. Answer persistence + retrieval trace (FR-029, UC-10).
8. SSE streaming of the extractive answer (FR-048).
9. Answer cache keyed including `acl_set_hash` (FR-033).

**Exit criteria**
- Citation correctness ≥ 0.97.
- Correct abstention ≥ 0.90 **and** refusal precision ≥ 0.80 on the absent slice (both directions matter).
- Over-abstention rate within the target band on answerable questions.
- p95 extractive answer < 400 ms (dev hardware).
- Cache: ≥ 30 % hit rate on replay; **negative cross-ACL test passes**.
- UI renders a verified answer with clickable citations that resolve to the exact clause.

**Tests.** Citation-span unit tests (positive, negative, Unicode/whitespace normalisation) ·
abstention threshold calibration test · cache ACL-isolation negative test · answer record completeness ·
truncation disclosure test.

**Docs.** `05-rag/citation-grounding`, `07-api/`, updated eval harness slices.

**Risks.** Abstention threshold mis-calibrated → measured on both directions; a threshold tuned only for
"abstain more" is a failure, not a safety feature.

---

## 20.7 Phase 6 — Generation (feature-flagged)

**Goal.** Generative answers with a typed output contract, grounded and verifiable — introduced **after**
the honest path works and only where measured.

**Depends on.** Phase 5, and Phase 7's harness for gating.

**Deliverables**

1. `LLMProvider` adapters (local llama.cpp; optional hosted, disabled by default).
2. Prompt assembly with the **delimited untrusted-content block** and the typed output contract (§11.6).
3. Streaming generation with output-contract validation.
4. Citation verification applied to generated citations.
5. Abstention applied when zero citations verify.
6. Token budgets, per-tenant token quotas, circuit breaker, timeouts (SEC-011, OPS-007).
7. Per-tier configuration: enabled on T-1/T-3, opt-in slow mode on T-2.
8. Feature flags for generation, reranking, rewriting, decomposition (OPS-011).

**Exit criteria**
- Groundedness ≥ 0.95 and unsupported-claim rate ≤ 0.02 on the in-corpus slice.
- Abstention preserved on S7 (a generative model must not make the honest path dishonest).
- p95 TTFT < 1.2 s on T-1; T-3 p95 complete < 8 s.
- Malformed/hostile outputs never reach the renderer (contract test suite).
- **Prompts are version-controlled** with rationale, and a prompt change cannot merge without an eval result.

**Tests.** Output-contract fuzzing · citation verification on generated output · prompt-injection
corpus (must show zero behaviour change) · breaker/timeout behaviour · quota enforcement · token
accounting accuracy.

**Docs.** `05-rag/prompt-library`, `09-reliability/degradation`, ADR-005.

**Risks.** Local-model quality insufficient → **keep the extractive path as the default** and document
that honestly; this is a designed outcome, not a failure.

---

## 20.8 Phase 7 — Evaluation

**Goal.** The instrument that everything else depends on. Without it, Phases 4–6 are guesswork.

**Depends on.** Phases 3–6.

**Deliverables**

1. Benchmark corpus manifest with content hashes.
2. Full question set S1–S13 (≈470 items) with gold answers for the gold core.
3. Chunk-level relevance judgements for the 200–400 item gold core.
4. Evaluation harness: deterministic metrics + pinned LLM judge with human calibration.
5. Baseline committed to the repository.
6. Per-slice reporting with before/after diffs.
7. CI gates (blocking: retrieval, citation, abstention, security suites).
8. Nightly full eval + trending.
9. **Measured quality at each degradation level** — the L3 lexical-only number that
   [OQ-015](21-open-questions.md) leaves open.

**Exit criteria**
- Every target in [§11.8](10-rag-architecture-options.md#118-what-good-enough-means-numerically) measured — **including misses**.
- Two runs on the same commit produce identical aggregates within tolerance (NFR-014).
- Judge-human agreement reported with κ; if κ < 0.6, the judge is demoted from gating.
- The lexical-only vs hybrid gap is quantified.
- Every slice is reported individually; no aggregate-only reporting.

**Tests.** Harness self-tests (a known-good and a known-bad system) · determinism test · per-slice
regression gate · benchmark-integrity check (no item may be added without review).

**Docs.** `16-evaluation/*`, updated §11.8 with measured values, EJD entries.

**Risks.** Author-bias in the question set (documented, partially mitigated by adversarial slices) ·
synthetic-corpus optimism (documented as [RISK-011](20-risk-register.md)).

---

## 20.9 Phase 8 — Security hardening

**Goal.** Turn every control in [§13](12-threat-model.md) from a claim into a passing test.

**Depends on.** Any implemented feature.

**Deliverables**

1. Complete authorisation policy table + matrix test (12 roles × 8 doc types × 20 fixtures).
2. Tenant isolation suite across 100 % of endpoints, including RLS-bypass attempts.
3. RLS policies applied independently of the application layer.
4. ACL projection reconciliation job with alerting (DI-13).
5. Prompt-injection corpus (S11), multilingual, in CI and nightly — **blocking at 1.00**.
6. Upload hardening suite (zip bomb, type spoofing, traversal, oversize, MIME mismatch).
7. SSRF defence + tests, **if URL ingestion is enabled** (`OQ-007`).
8. Sandbox escape regression suite.
9. Secret management + canary-secret test + image scan.
10. Security headers, CSP, TLS configuration.
11. Supply chain: SBOM, pinned revisions, model checksum verification, no `trust_remote_code`.
12. Quota and rate-limit enforcement tests.
13. Threat-model re-walk for every new data flow.

**Exit criteria**
- All blocking security suites green; zero known high/critical findings open.
- Cross-tenant violation count is zero in tests **and** zero in production telemetry.
- Injection suite at 1.00; no behaviour change caused by adversarial content.
- Secrets absent from repo, images and logs (automated proof).
- A reviewer can read the threat model and the tests and see they correspond.

**Tests.** As per the §13.6 catalogue.

**Docs.** `08-security/*`, threat-model updates, EJD entries for accepted residual risk.

**Risks.** Security tests written by the same person as the feature ([§13.6](12-threat-model.md)
mitigation: data-driven suites from the policy table, so a missing case is a data gap, not an oversight).

---

## 20.10 Phase 9 — Observability

**Goal.** Every SLO measurable, every paging alert actionable, every answer debuggable.

**Depends on.** Phases 1–6 implemented.

**Deliverables**

1. Full metric catalogue ([§15.2](14-observability-model.md)) with cardinality guards enforced in CI.
2. Trace span model per the stage list, with sampling policy.
3. Log schema v1 + redaction at the collector + retention.
4. Error taxonomy implemented end to end.
5. Dashboards 1–8.
6. Alerts with severity, symptom, runbook link and verification step.
7. **Runbook per paging alert**, written before the alert is enabled.
8. Alert-fatigue control: two weeks of baseline before thresholds are tightened.
9. Trace-to-answer debugging walkthrough as a documented procedure.

**Exit criteria**
- Every SLI in [§14.2](13-reliability-model.md) computable and displayed.
- Every paging alert links to a runbook with a verification step.
- A synthetic incident is detected within the alert threshold and triaged using only dashboards and runbooks.
- `internal_error` < 5 % of all errors.
- No metric has an unbounded label set (CI-enforced).

**Tests.** Metric presence assertions · alert-routing tests · a deliberate degradation is detected ·
collector redaction test.

**Docs.** `10-observability/*`, runbooks in `14-operations/runbooks`.

**Risks.** Alert fatigue → mitigated by symptom-based paging and a two-week baseline; undersized
dashboards → mitigated by the per-tenant and per-stage views, which are the ones that get used.

---

## 20.11 Phase 10 — Performance and load

**Goal.** Replace the analytical estimates with measurements, and validate the capacity model.

**Depends on.** Phases 4–9.

**Deliverables**

1. Load tests for every PERF requirement, at 1×, 3× and soak.
2. **Measured embedding throughput** on the actual hardware → replaces ASM-014/015/016.
3. **Measured rerank cost** → validates the §4.4 conclusion on real hardware.
4. **Measured generation latency** at 0.5B/1.5B/3B/7B → validates the §4.7 conclusion.
5. Database tuning: connection pool, `statement_timeout`, autovacuum, HNSW parameters, `maintenance_work_mem`.
6. HNSW recall/latency trade-off curve (`ef_search`, `m`) with measured numbers.
7. Index build timing; shadow-build validation.
8. Capacity model updated with measurements; a revised [§4](03-scale-model.md).
9. Bottleneck analysis documenting where the system actually breaks first.

**Exit criteria**
- All PERF targets met on the target tier, or the target is revised with justification.
- Measured throughput/recall/latency figures replace every ESTIMATE in [§4](03-scale-model.md).
- Index build completes within the maintenance window with **no serving impact**.
- 3× burst for 10 min with no error-rate increase (PERF-009).
- Memory stays within PERF-014 under load.

**Tests.** Locust/k6 scenarios · soak test · shadow-swap-under-load test · HNSW parameter sweep.

**Docs.** `12-performance/*`, updated scale model, EJD entries with before/after.

**Risks.** Measured results contradicting the model (a **good** outcome — it means the model was testable)
→ update the model, the ADRs, and the roadmap, and record the delta in the journal.

---

## 20.12 Phase 11 — Production readiness

**Goal.** Operable by a human under pressure.

**Depends on.** Phases 8–10.

**Deliverables**

1. Restore drill: full backup → restore → **integrity verification** (document count, vector integrity, ACL integrity, answer continuity) within RTO.
2. Disaster-recovery documentation and a decision tree for each failure domain.
3. Incident-response runbooks; a simulated Sev-1 exercise.
4. Postmortem template + the first real postmortem.
5. Graceful-shutdown verification (NFR-006) under real deploy conditions.
6. Blue/green deploy with automatic rollback on SLO burn (OPS-009).
7. Feature-flag operational procedure.
8. Configuration and secrets runbook; rotation procedure.
9. Capacity thresholds and scaling triggers documented.
10. Accessibility verification (NFR-009).

**Exit criteria**
- Restore drill completes within the stated RTO and passes integrity checks.
- A simulated Sev-1 is detected, triaged and mitigated **using only runbooks**.
- A deliberately bad release is auto-rolled-back within 5 minutes.
- Graceful shutdown under load loses no committed work.
- Every operational task has a documented procedure.

**Tests.** Restore drill · rollback drill · shutdown-under-load · failover drill.

**Docs.** `09-reliability/dr-backups`, `14-operations/*`, first postmortem in `15-incidents/`.

**Risks.** Restore never tested until needed — the classic failure. **The drill is an exit criterion,
not an aspiration.**

---

## 20.13 Phase 12 — Deployment (free demonstration tier)

**Goal.** A publicly reachable, free, working demonstration that a reviewer can evaluate in minutes.

**Depends on.** Phase 11.

**Deliverables**

1. T-2 deployment on a free VM, containerised, one-command bring-up.
2. Seeded corpus + reproducible embedding build (`make seed`).
3. HTTPS with automated certificate renewal.
4. Domain configuration with zero hard-coded assumptions.
5. Monitoring running on the same host with **disk/host-down alerts routed off-host** (a host that is down cannot report that it is down — this is a genuine subtlety).
6. Runbook for the free-tier host, including "what to check if the provider changes its free tier".
7. Honest tier documentation published with the demo: what is free, what is slow, and why.
8. Smoke tests post-deploy covering journeys A, C, D, G.

**Exit criteria**
- A reviewer reaches a cited answer in < 15 minutes from clone.
- All smoke journeys pass in the deployed environment.
- Monitoring and alerting verified from outside the host.
- The published tier documentation states the T-2 limitations without hedging.

**Tests.** Post-deploy smoke suite · certificate renewal dry-run · backup verification from off-host.

**Docs.** `13-deployment/*`, `17-cost/*`, updated operations runbooks.

**Risks.** Free-tier capacity unavailability → contingency: the stack is containerised for a provider
swap (RISK-017); a laptop-tier demo is always available as the floor.

---

## 20.14 Phase 13 — T-3 enablement (production-scale path)

**Goal.** Make the production-scale architecture *reviewable and ready*, without necessarily deploying it.

**Depends on.** Phase 12.

**Deliverables**

1. Scalability design at 10⁸+ chunks: partitioning, sharding by tenant, read replicas, per-shard capacity.
2. GPU embedding/rerank worker design with batch scheduling and backpressure.
3. Multi-region design and the consistency trade-offs it forces (ADR).
4. Managed-service migration plan (Postgres, object storage, backends) with cost deltas.
5. Load test at target scale, or the largest scale attainable, with extrapolated results clearly marked as extrapolated.
6. SLOs restated for T-3 with measured evidence.
7. `18-future/scale-path`, `multi-region`, `multi-institution`.

**Exit criteria**
- A reviewer can trace the path from T-2 to T-3 with no design change that invalidates a prior ADR.
- Every T-3 claim is either measured or explicitly labelled as an extrapolation.
- The scaling triggers in [§7.7](06-architecture-options.md#77-what-would-change-the-answer) are
  operationalised as monitored signals, not just prose.

---

## 20.15 Phase 14 — Continuous improvement

**Goal.** Evidence-driven evolution, evidenced in the engineering journal.

**Ongoing**
1. Nightly eval with trending and regression alerts.
2. Monthly quality/cost/latency review; re-derive SLOs from real usage (replacing ASM-003…006).
3. Quarterly threat-model review and dependency audit.
4. ADR reviews when a trigger from §7.7 or a §11 justification test fires.
5. Journal entries for every change with expected vs measured impact.
6. At least one **honest negative result** documented (a technique that was adopted, measured, and then
   removed). A project that only reports successes is either lucky or not measuring.

---

## 20.6 Requirement traceability (summary)

| Requirement group | Primary phase | Verified by |
|---|---|---|
| FR-001…014 (ingestion/lifecycle) | 2, 3 | Integration + property tests |
| FR-020…034 (retrieval/answering) | 4, 5, 6 | Benchmark |
| FR-040…050 (API/admin) | 1, 5 | API + E2E tests |
| NFR-001…014 | 1, all | CI gates |
| SEC-001…016 | 1, 3, 8 | Security suites |
| OPS-001…015 | 1, 2, 9, 11 | Ops drills |
| PERF-001…014 | 10 | Load tests |

Full requirement→design→test mapping is maintained as
`docs/02-requirements/traceability-matrix.md` from Phase 1 (created when it has content).