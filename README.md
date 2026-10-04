# Intelligent University Knowledge Assistant

A production-grade, free-first Retrieval-Augmented Generation (RAG) platform for institutional
knowledge: regulations, examination rules, timetables, handbooks, circulars, policies and notices.

> **Status: Phase 0 — Architecture Discovery. No application code exists yet.**
> This repository currently contains architecture, requirements and decision records only.

## What this is

A system that ingests institutional documents (PDF / DOCX / TXT / HTML), turns them into a
versioned, searchable knowledge base, and answers user questions with **grounded, cited answers**
that abstain when the corpus does not support an answer.

The design target is a system that could serve multiple institutions at university scale. The
implementation target is free and self-hostable. The two are reconciled explicitly in
[docs/phase0/17-cost-model.md](docs/phase0/17-cost-model.md), never by pretending free
infrastructure has production capacity.

## Start here

| Document | Purpose | Audience |
|---|---|---|
| [PHASE0_OUTPUT_SUMMARY.md](PHASE0_OUTPUT_SUMMARY.md) | Reviewer handoff: decisions, rejections, assumptions, risks, prerequisites | AI/engineering reviewer |
| [docs/phase0/README.md](docs/phase0/README.md) | Index of the Phase 0 review, reading order, how to review it | Reviewer |
| [docs/phase0/22-architecture-review-checklist.md](docs/phase0/22-architecture-review-checklist.md) | Gate that must pass before Phase 1 | Reviewer + implementer |
| [docs/phase0/21-open-questions.md](docs/phase0/21-open-questions.md) | Every unresolved question, with the information required to close it | Stakeholder |
| [docs/04-adrs/](docs/04-adrs/) | Architecture decision records (ADR-001 … ADR-008) | Implementer |
| [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md) | Dated decision history with evidence and measured impact | Reviewer |

## Phase 0 document map

Sections 1–23 of the architecture review, in order:

| § | Document | Content |
|---|---|---|
| 1–2 | [01-executive-summary-and-problem.md](docs/phase0/01-executive-summary-and-problem.md) | What we build, for whom, problem definition, scope, assumptions |
| 3 | [02-requirements.md](docs/phase0/02-requirements.md) | FR / NFR / SEC / OPS / PERF with acceptance criteria + verification |
| 4 | [03-scale-model.md](docs/phase0/03-scale-model.md) | Workload model, derivations, assumption register |
| 5 | [04-user-journeys.md](docs/phase0/04-user-journeys.md) | 10 journeys incl. injection, LLM outage, retrieval outage |
| 6 | [05-system-boundaries.md](docs/phase0/05-system-boundaries.md) | System context, trust boundaries, security boundary |
| 7 | [06-architecture-options.md](docs/phase0/06-architecture-options.md) | Four topologies, scored on 12 fitness dimensions |
| 8 | [07-decision-matrix.md](docs/phase0/07-decision-matrix.md) | Weighted matrix + 2 sensitivity re-weightings |
| 9 | [08-recommended-architecture.md](docs/phase0/08-recommended-architecture.md) | Chosen design, component/data-flow diagrams, 3 service tiers |
| 10 | [09-technology-evaluation.md](docs/phase0/09-technology-evaluation.md) | 15 technology choices with pros/cons/lock-in |
| 11 | [10-rag-architecture-options.md](docs/phase0/10-rag-architecture-options.md) | Which RAG techniques are justified, and which are deferred |
| 12 | [11-rag-quality-strategy.md](docs/phase0/11-rag-quality-strategy.md) | Evaluation strategy, metrics, regression gates |
| 13 | [12-threat-model.md](docs/phase0/12-threat-model.md) | STRIDE + OWASP LLM Top 10, 24 threats with residual risk |
| 14 | [13-reliability-model.md](docs/phase0/13-reliability-model.md) | SLIs, SLOs, error budgets, RTO/RPO, degradation |
| 15 | [14-observability-model.md](docs/phase0/14-observability-model.md) | Metrics/logs/traces/alerts, mapped to SLOs |
| 16 | [15-data-model.md](docs/phase0/15-data-model.md) | Conceptual model, ER diagram, lifecycle |
| 17 | [16-failure-analysis.md](docs/phase0/16-failure-analysis.md) | Per-component FMEA: slow / corrupt / wrong / unavailable |
| 18 | [17-cost-model.md](docs/phase0/17-cost-model.md) | Free / OSS / free-tier / optional paid; enterprise migration |
| 19 | [18-documentation-architecture.md](docs/phase0/18-documentation-architecture.md) | Full doc tree, purpose, audience, update trigger |
| 20 | [19-implementation-roadmap.md](docs/phase0/19-implementation-roadmap.md) | Phases 0–14 with exit criteria |
| 21 | [20-risk-register.md](docs/phase0/20-risk-register.md) | 38 risks with probability, impact, contingency |
| 22 | [21-open-questions.md](docs/phase0/21-open-questions.md) | Blocking vs non-blocking questions |
| 23 | [22-architecture-review-checklist.md](docs/phase0/22-architecture-review-checklist.md) | Pre-Phase-1 gate |
| — | [23-diagram-register.md](docs/phase0/23-diagram-register.md) | Every diagram, its decision, and the phase that owns it |

## Principles this repository follows

1. **Spec-driven.** No feature without a requirement ID, acceptance criteria and a verification method.
2. **Decision records.** Every consequential choice is an ADR with rejected alternatives and tradeoffs.
3. **Free-first, honest.** Free infrastructure is used and its limits are stated as limits.
4. **Evaluation before optimisation.** A technique ships only if it measurably improves a benchmark.
5. **No resume theatre.** No technology without a requirement it serves.
6. **Untrusted by default.** Retrieved documents are attacker-controlled input, including their metadata.

## Licence

To be decided — see [OQ-001](docs/phase0/21-open-questions.md).
