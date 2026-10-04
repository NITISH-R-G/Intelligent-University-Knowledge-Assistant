# §21 Risk Register

**Scoring.** Probability (P) and Impact (I) 1–5. Severity = P × I: Low ≤ 4, Medium 5–9, High 10–15,
Critical ≥ 16. Every risk has a **contingency** — the response if the mitigation fails — because a
mitigation without a fallback is a hope.

**Scope note.** This register includes risks to *the project* (will this be finished and credible?)
as well as risks to *the system* (will it fail in production?). Both matter; the second is what a
reviewer will probe hardest.

---

## 21.1 Technical risks

| ID | Risk | P | I | Sev | Mitigation | Contingency |
|---|---|:-:|:-:|:-:|---|---|
| **RISK-001** | **Parser fragility on real institutional PDFs** — multi-column layouts, scanned inserts and broken encodings produce poor text | 4 | 4 | **High** | Sandbox every parser; measure yield per document at ingest and quarantine low-yield files; several parser backends; corpus profiling in Phase 2 before committing to the pipeline | OCR fallback for flagged documents (scope increase); or publish only the documents that parse correctly, and say so explicitly |
| **RISK-002** | **HNSW index build degrades serving** — a 22 M-chunk build saturates IO on the serving database | 3 | 4 | **High** | Shadow build + atomic swap (FR-014); build on a worker at reduced priority; quiet hours; measure build time in Phase 10 | Build on a separate instance and swap the search target; fall back to IVF+quantisation; temporarily serve lexical-only |
| **RISK-003** | **Free hardware cannot meet the generative latency SLO** — ~60 s per answer on 4 ARM cores | 5 | 3 | **High** | Already designed for: extractive path is the default on T-2; generation is opt-in "slow mode"; §4.7 documents the measurement | Accept and document honestly; offer a hosted-LLM adapter behind opt-in; keep T-1 laptop as the good-UX demo |
| **RISK-004** | **pgvector recall degrades before the modelled scale** — HNSW recall/latency falls off at ~10⁷–10⁸ chunks | 3 | 4 | **High** | Measure the recall/latency curve in Phase 10; tune `ef_search`; IVFFlat as an alternative index type | `VectorStore` port → a dedicated vector service; shard by tenant; quantisation |
| **RISK-005** | **Queue/deadlock contention** — ingestion competes with queries for the same engine | 3 | 3 | **Medium** | Separate pools; `statement_timeout`; autovacuum tuning; per-tenant ingestion concurrency limits | Serve reads from a replica; shed generation first; pause ingestion |
| **RISK-006** | **Module boundary erosion** — the modular monolith degrades into a ball of mud | 4 | 3 | **High** | CI import-boundary lint rule (NFR-002); port definitions; explicit R2/R5 rules in §9.10 | Extract the offending module to a service — the seams make this tractable |
| **RISK-007** | **Schema/migration breaks a running deployment** | 2 | 5 | **High** | Forward-only, backwards-compatible migrations; expand/contract pattern; migration tested in CI while serving traffic | Roll forward with a fix; restore from backup in the worst case |
| **RISK-008** | **Silent retrieval-quality regression from an index or chunking change** | 3 | 4 | **High** | Canary recall probe; nightly full eval with per-slice comparison; CI retrieval gate; `chunker_version` reconciliation | Rebuild the index from the previous known-good configuration; the shadow-build design makes this cheap |

## 21.2 Security risks

| ID | Risk | P | I | Sev | Mitigation | Contingency |
|---|---|:-:|:-:|:-:|---|---|
| **RISK-009** | **Cross-tenant data leakage** | 2 | 5 | **High** | Mandatory tenant scope in the data layer + independent RLS; cross-tenant test suite over 100 % of endpoints; ACL-keyed cache; alerting at any non-zero | Immediate tenant isolation (route affected tenants to a dedicated instance); incident response; audit-based scope determination |
| **RISK-010** | **Authorisation bypass via a denormalised ACL projection** | 3 | 5 | **High** | ACLs computed centrally; reconciliation job with alerting (DI-13); never accepted from an upload request | Disable retrieval; rebuild all ACL projections from `DOCUMENT_ACL`; treat as Sev-1 |
| **RISK-011** | **Prompt injection succeeds against the extraction path** | 3 | 4 | **High** | No tools/agency/network (the primary control); delimited untrusted content; typed output contract; detection as defence-in-depth; adversarial CI slice at 1.00 | Disable generation (extractive path is immune by construction); tighten the output contract; document the residual risk honestly |
| **RISK-012** | **SSRF if URL ingestion is enabled** | 3 | 5 | **High** | **Deferred to Phase 4**; when enabled: allowlist, DNS-rebinding defence, per-hop validation, egress proxy | Keep URL ingestion disabled — it is explicitly the lowest-value, highest-risk feature |
| **RISK-013** | **Parser RCE or sandbox escape** | 2 | 5 | **High** | Sandbox with no network/credentials, hard limits, pinned patched parsers; escape regression tests | Disable the affected format; block uploads of that type; upstream patch |
| **RISK-014** | **Supply-chain compromise (package or model)** | 2 | 5 | **High** | Lockfile, `pip-audit`, SBOM, no `trust_remote_code`, pinned model revisions with checksums, minimal dependencies | Pin the last known-good version; rebuild; rotate secrets if build-time |
| **RISK-015** | **Secret leakage in repo/image/logs** | 2 | 5 | **High** | Secret store only; gitleaks; canary-secret test; secrets never passed to parsers or the model runtime | Rotate all secrets; audit log access; treat as Sev-1 |

## 21.3 Data risks

| ID | Risk | P | I | Sev | Mitigation | Contingency |
|---|---|:-:|:-:|:-:|---|---|
| **RISK-016** | **No suitable public benchmark corpus available** (licensing) | 3 | 4 | **High** | Search early in Phase 2; commit the corpus manifest; prefer public institutional documents | Synthetic corpus with documented bias; reduce evaluation ambitions and say so |
| **RISK-017** | **Free-tier availability changes or capacity disappears** | 4 | 3 | **High** | Containerised stack; provider chosen for portability; laptop tier always available as a floor | Move to a different free provider (config change); fall back to T-1 demos; document the outage |
| **RISK-018** | **Supersession semantics are wrong for real institutional practice** | 3 | 5 | **High** | Written assumption A-07; domain-expert review; the publish gate surfaces overlap conflicts | Extend the version model with explicit variants; require effective dates on every version |
| **RISK-019** | **Metadata extraction is inaccurate** (wrong dates/types) → wrong temporal filtering | 4 | 4 | **High** | Measured metadata F1 in Phase 2/3; **admin review before publish**; metadata provenance recorded | Correct the metadata; re-publish; audit affected answers |
| **RISK-020** | **Text-yield assumption (ASM-008) is wrong by > 2×** | 3 | 3 | **Medium** | Measured in the first task of Phase 2, before capacity commitment | Re-scale the model; sharding/quantisation as designed; update ADRs |

## 21.4 AI quality risks

| ID | Risk | P | I | Sev | Mitigation | Contingency |
|---|---|:-:|:-:|:-:|---|---|
| **RISK-021** | **Benchmark overfitting** — optimising the metric rather than the user | 4 | 4 | **High** | Guard metrics per slice; "no slice may regress" rule; benchmark changes reviewed like code; every adopted technique needs a stated mechanism | Rebuild the benchmark from held-out real queries when available; report slice-level results only |
| **RISK-022** | **Author-written questions are unrepresentative** | 4 | 3 | **High** | Adversarial slices (S7/S9/S10/S11); negative sampling from real headings; document the bias | Re-derive from real traffic once deployed; treat early results as provisional |
| **RISK-023** | **Residual hallucination** — a fluent, uncited, unsupported claim | 4 | 4 | **High** | Five defence layers; deterministic citation verification; abstention; UI separates cited from uncited claims; continuous measurement | Strengthen abstention thresholds (with the over-abstention cost measured); make generation opt-in; document the residual risk honestly rather than claiming "grounded" |
| **RISK-024** | **Small local models are simply worse** | 4 | 3 | **High** | Benchmark model candidates in Phase 7; keep the extractive path as a first-class alternative | Extractive-only as the product; hosted model behind opt-in; accept lower quality with measurement to back it |
| **RISK-025** | **Authorisation interacts badly with recall** — strict filters exclude relevant documents | 3 | 3 | **Medium** | Denormalised ACL for filter performance; measure pre/post-filter drop-off; escalation path to a pre-permissioned index | Materialise per-role indexes; relax granularity (cohort → department) with an audit trail |

## 21.5 Scalability, cost and operational risks

| ID | Risk | P | I | Sev | Mitigation | Contingency |
|---|---|:-:|:-:|:-:|---|---|
| **RISK-026** | **Free-tier hardware is insufficient for the demo corpus** | 3 | 3 | **Medium** | Seeded corpus sized to the hardware; precomputed embeddings reproducible from source; laptop tier | Shrink the demo corpus; run the bulk embed on a free GPU notebook (`OQ-006`) |
| **RISK-027** | **Cost explosion if generation is enabled everywhere** | 3 | 4 | **High** | Extractive-first by design; token quotas; cache; cost-per-1000-answers tracked | Enforce quotas per tenant; disable generation by default; raise prices consciously |
| **RISK-028** | **Operational overload for a single maintainer** | 3 | 4 | **High** | Deliberately few components; runbooks per alert; documented triggers for every escalation | Reduce scope consciously; drop features rather than operate them badly |
| **RISK-029** | **The free tier's *disk* is exhausted by logs/traces** | 3 | 3 | **Medium** | Cardinality limits; sampled debug logging; retention policies; disk alerts at 85 % | Aggressive retention truncation; disable tracing temporarily |
| **RISK-030** | **Project scope creep** — the "production-grade" mandate expands indefinitely | 4 | 4 | **High** | Phased roadmap with explicit exit gates; "reject with a trigger" discipline; the definition of done in §2.8 | Cut features; keep quality; document what was deliberately deferred |
| **RISK-031** | **Timebox exhausted before evaluation exists** — a system with no measured quality | 3 | 5 | **High** | Evaluation is Phase 7 and gates CI from Phase 4 onward; a minimal harness exists early | Ship with the extractive path only and published measurements; do not ship unmeasured generation |

## 21.6 Vendor lock-in risks

| ID | Risk | P | I | Sev | Mitigation | Contingency |
|---|---|:-:|:-:|:-:|---|---|
| **RISK-032** | **Hosted LLM lock-in** (prompt/format/eval differences) | 3 | 3 | **Medium** | Local-first default; `LLMProvider` port; provider-agnostic eval harness | Swap the adapter; re-run the benchmark; the harness makes this a run, not a project |
| **RISK-033** | **Object-store provider change** (e.g. MinIO governance/licensing) | 3 | 2 | **Medium** | `ObjectStore` port; server-generated keys; any S3-compatible target | Configuration change |
| **RISK-034** | **pgvector extension ecosystem is younger than dedicated vector DBs** | 3 | 3 | **Medium** | `VectorStore` port; measure recall/latency rather than assuming; escalation trigger documented | Dedicated vector service behind the port; shard by tenant |
| **RISK-035** | **Framework/dependency churn** (FastAPI, parsers, ONNX versions) | 4 | 2 | **Medium** | Minimal dependencies; pinned versions; the domain depends on ports, not frameworks | Pin or fork; the port boundary limits blast radius |

## 21.7 Project-complexity risks

| ID | Risk | P | I | Sev | Mitigation | Contingency |
|---|---|:-:|:-:|:-:|---|---|
| **RISK-036** | **Portfolio projects are judged on polish, not architecture** — a well-architected system with no demo looks empty | 3 | 3 | **Medium** | Every phase produces a demonstrable increment; seeded corpus; a 15-minute reviewer path ([§2.8](01-executive-summary-and-problem.md#28-what-done-means-for-this-project)) | Prioritise the demo path over the deepest architecture |
| **RISK-037** | **Documentation becomes a parallel project** | 4 | 3 | **High** | Docs written *with* each phase, not after; templates; 20–30 day estimate stated up front | Reduce doc scope to ADRs + runbooks + eval results |
| **RISK-038** | **Phase 0 documentation is mistaken for the deliverable** | 3 | 3 | **Medium** | Explicit "no application code yet" status in the README; the roadmap makes Phase 1 the first code | Ship code early and iterate on the architecture as real measurements arrive |

## 21.8 Top risks and their handling

| Rank | Risk | Handling |
|---:|---|---|
| 1 | **RISK-023** residual hallucination | Inherent to the technique. Five defence layers, continuous measurement, honest disclosure, cited-vs-uncited UI separation |
| 2 | **RISK-009** cross-tenant leakage | Two independent controls, exhaustive negative testing, alerting at any non-zero |
| 3 | **RISK-016/021/022** evaluation validity | Phase 2 corpus work, per-slice reporting, benchmark-change review, documented bias |
| 4 | **RISK-003** free-tier generative latency | Designed around, not hidden: extractive-first, measured, labelled |
| 5 | **RISK-031** unmeasured system | Evaluation gates CI from Phase 4; Phase 7 is a hard gate before production readiness |
| 6 | **RISK-030** scope creep | Phased gates; "reject with a trigger" discipline; done = reviewer-path works |

**The pattern worth noticing:** the highest risks are not technical failures. They are **measurement
failure** (RISK-021/022/031) and **trust failure** (RISK-009/023). This is characteristic of LLM
systems, and it is why this project's most important deliverables are the evaluation harness, the
threat model and the honest documentation — not the retrieval code.