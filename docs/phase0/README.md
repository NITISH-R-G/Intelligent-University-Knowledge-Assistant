# Phase 0 — Architecture Discovery

**Deliverable status:** complete, pending review.
**Date:** 2026-10-04
**Scope:** Architecture discovery only. No application code, no application configuration, no deployment manifests.
**Reviewer audience:** a staff/principal engineer reviewing the architecture without access to the originating conversation.

---

## 1. How to review this package

This is deliberately a *review* document, not a proposal to be admired. The correct way to use it:

| Step | Read | Question to answer |
|---|---|---|
| 1 | [PHASE0_OUTPUT_SUMMARY.md](../../PHASE0_OUTPUT_SUMMARY.md) | Are the decisions, rejections and assumptions the ones I would make? |
| 2 | [02-requirements.md](02-requirements.md) | Are the requirements specific enough to fail a test? |
| 3 | [03-scale-model.md](03-scale-model.md) | Do the derivations hold? Which assumptions are wrong? |
| 4 | [07-decision-matrix.md](07-decision-matrix.md) | Are the weights honest, or tuned to a predetermined answer? |
| 5 | [08-recommended-architecture.md](08-recommended-architecture.md) | Does the design actually serve the requirements? |
| 6 | [10-rag-architecture-options.md](10-rag-architecture-options.md), [11-rag-quality-strategy.md](11-rag-quality-strategy.md) | Is quality going to be measured, or asserted? |
| 7 | [12-threat-model.md](12-threat-model.md) | Is the retrieval layer treated as a security boundary? |
| 8 | [21-open-questions.md](21-open-questions.md) | What must be answered before code is written? |
| 9 | [22-architecture-review-checklist.md](22-architecture-review-checklist.md) | Does the gate pass? |

**The most productive review action** is to attack §4 (scale model) and §8 (decision matrix). Those are
the two places where a wrong assumption silently invalidates everything downstream. If you disagree
with a weight or a derivation, say so — §8 already contains two sensitivity re-weightings precisely so
that this disagreement is cheap to test.

## 2. Conventions used

| Convention | Meaning |
|---|---|
| `ASM-###` | **Assumption.** An unverified input to the design. Every assumption states how it would be verified. |
| `FR/NFR/SEC/OPS/PERF-###` | Requirement, with priority, acceptance criteria and verification method. |
| `ADR-###` | Architecture decision record. Immutable once accepted; superseded, not edited. |
| `RISK-###` | Risk register entry. |
| `THR-###` | Threat model entry. |
| `T-#` | Service tier (see [§9](08-recommended-architecture.md#91-service-tiers)). |
| `OQ-###` | Open question, with the information required to close it and the phase it blocks. |
| `EJD-###` | Engineering journal entry. |

Numbers in this document are either (a) **derived** and the derivation is shown, (b) **measured**
and marked as such, or (c) **assumed** and marked `ASM-`. There are no fourth category. Where a
figure is an order-of-magnitude engineering estimate it is labelled `ESTIMATE` and carries a
"measure this in Phase N" note.

## 3. What Phase 0 deliberately did *not* do

These are omissions with reasons, not gaps.

| Not done | Why | When |
|---|---|---|
| Final database DDL / schema | The conceptual model is stable; the physical schema is not, because index choice depends on measured corpus statistics. Premature DDL would ossify assumptions. | Phase 3 |
| Concrete API OpenAPI file | The domain model and authorisation model must be agreed first; contracts written before them get rewritten. | Phase 1 (skeleton) / Phase 5 (contract) |
| Vendor/model pinning to exact versions | Model choice must be *measured* on the benchmark (Phase 7), not asserted. Interfaces are defined; implementations are candidates. | Phase 7 |
| Kubernetes, Kafka, service mesh | No requirement in §3 demands them. Adding them now would be cost without evidence. See [ADR-001](../../docs/04-adrs/ADR-001-modular-monolith-with-asynchronous-workers.md). | Revisit at phase-gate, with data |
| Any application code | Explicit instruction, and correct: architecture that has not survived review should not be encoded. | Phase 1 |

## 4. Document index

See [README.md](../../README.md#phase-0-document-map) for the full §1–§23 map.

## 5. Status of each numbered section

| § | Section | Status | Notable open items |
|---|---|---|---|
| 1 | Executive summary | Complete | — |
| 2 | Problem definition | Complete | Assumptions A-01…A-08 need stakeholder confirmation |
| 3 | Requirements | Complete (99: 40 FR, 14 NFR, 16 SEC, 15 OPS, 14 PERF) | Of the 85 carrying a priority: 72 `MUST`, 12 `SHOULD`, 1 `POSTPONED` (FR-034). The 14 PERF requirements are specified as per-tier targets rather than a priority |
| 4 | Scale model | Complete with 20 assumptions | ASM-004 (text yield), ASM-009 (churn) dominate the result |
| 5 | User journeys | Complete (10 journeys) | Journey I/J define degradation contracts, not yet implemented |
| 6 | System boundaries | Complete | URL ingestion deferred to Phase 4 (`OQ-007`) |
| 7 | Architecture options | Complete (4 options, 12 dimensions) | — |
| 8 | Decision matrix | Complete + 2 sensitivity analyses | Weights are the reviewer's main attack surface |
| 9 | Recommended architecture | Complete | Tier-2 SLO depends on `OQ-005` (hosted LLM?) |
| 10 | Technology evaluation | Complete (15 decisions) | Embedding/LLM model picks are provisional pending Phase 7 |
| 11 | RAG options | Complete | Rerank is feature-flagged pending nDCG measurement |
| 12 | RAG quality strategy | Complete | Benchmark corpus must be built in Phase 7 — highest-effort item |
| 13 | Threat model | Complete (24 threats) | Residual risk accepted for THR-006, THR-011 |
| 14 | Reliability | Complete | Free-tier availability is genuinely worse; stated, not hidden |
| 15 | Observability | Complete | Alert thresholds need 14 days of baseline (Phase 9) |
| 16 | Data model | Conceptual only | Physical schema deferred to Phase 3 |
| 17 | Failure analysis | Complete (FMEA) | — |
| 18 | Cost model | Complete | Prices are `ASM` and must be re-verified before any purchase |
| 19 | Documentation architecture | Complete | Tree is planned, not pre-created empty |
| 20 | Roadmap | Complete (Phases 0–14) | — |
| 21 | Risk register | Complete (38 risks) | — |
| 22 | Open questions | Complete (20 questions) | 6 are blocking Phase 1 |
| 23 | Review checklist | Complete | — |
