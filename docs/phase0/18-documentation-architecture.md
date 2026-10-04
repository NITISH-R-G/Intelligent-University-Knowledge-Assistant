# §19 Documentation Architecture

**Rule: no empty documents.** A directory tree that exists only to look comprehensive is worse than
no tree, because it advertises content that does not exist. Therefore this section defines the **plan**,
with an explicit **creation phase** for each document, and creates only the documents that Phase 0
genuinely produced.

---

## 19.1 Planned tree

```
docs/
├── 00-overview/          System context, how to read, glossary        [Phase 0 ✓]
├── 01-product/           Personas, use cases, user journeys, scope     [Phase 0 ✓]
├── 02-requirements/      Requirements, scale model, traceability      [Phase 0 ✓]
├── 03-architecture/      Context, options, decision matrix, chosen design
├── 04-adrs/              Architecture decision records                [Phase 0 ✓]
├── 05-rag/               Chunking, retrieval, ranking, context, evaluation
├── 06-data/              Schema, ER, migrations, partitioning, lifecycle
├── 07-api/               OpenAPI, request/response contracts, versioning, errors
├── 08-security/          Threat model, authn/authz, trust boundaries, secret handling
├── 09-reliability/       SLIs/SLOs, error budgets, DR, degradation, RTO/RPO
├── 10-observability/     Metrics, logs, traces, alerts, dashboards, runbooks
├── 11-testing/           Strategy, coverage, fixtures, load, failure testing
├── 12-performance/       Benchmarks, capacity model, latency budgets, tuning
├── 13-deployment/        Environments, pipelines, rollback, configuration
├── 14-operations/        Runbooks, on-call, maintenance, capacity, postmortems
├── 15-incidents/         Incident records, postmortems (written after the first one)
├── 16-evaluation/        Benchmark corpus, harness, results, model comparisons
├── 17-cost/              Tiers, cost model, cost-per-answer tracking
└── 18-future/            Scale path, multi-region, multi-institution, research directions
```

Directories are created **when their first document has content**. Phase 0 has produced real content for
`00-overview`, `01-product`, `02-requirements`, `03-architecture`, `04-adrs`, `08-security`,
`09-reliability`, `10-observability`, `16-evaluation`, `17-cost`, `18-future`, plus this section. The
rest are scheduled.

---

## 19.2 Document plan

Each row states **purpose / audience / update trigger / decisions recorded**. The update trigger is the
part most documentation plans omit, and the part that determines whether a document survives.

### `00-overview/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| System context | One-page orientation with the context diagram | Everyone | Topology change | System boundary |
| Glossary | Domain terms (chunk, version, in-force, ACL set) | Everyone | New domain term | Terminology |
| Reading guide | How to navigate the docs | New contributors | Doc-tree change | — |

### `01-product/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Personas | Who we build for | Product, reviewers | Scope change | Persona definitions |
| Use cases | Primary/secondary/out-of-scope | Everyone | Scope change | Scope boundaries |
| User journeys | End-to-end behaviours incl. failure | Engineers, QA | Feature change | Expected behaviour per journey |

### `02-requirements/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Requirements register | FR/NFR/SEC/OPS/PERF with AC and verification | Everyone | Any requirement change | **What the system must do** |
| Scale model | Workload assumptions and derivations | Architects | Assumption measured | Capacity basis |
| Assumptions register | Every ASM with sensitivity and verification | Reviewers | Any ASM confirmed/refuted | Uncertainty |
| Traceability matrix | Requirement → design → test → metric | Reviewers, QA | Any of the above | Coverage proof |

### `03-architecture/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| System context & boundaries | Trust boundaries, data classification | Security, engineers | Boundary change | Trust model |
| Architecture options | The four topologies evaluated | Reviewers | New option considered | Rejected designs and why |
| Decision matrix | Weighted scoring + sensitivity | Reviewers | Weight or score change | The decision basis |
| Recommended architecture | The chosen design and its rules | Engineers | Design change | Decomposition rules |
| Component diagrams | Live Mermaid sources | Engineers | Component change | Structure |
| Data flows | Ingestion, query, retrieval, citations | Engineers | Flow change | Behaviour |

### `04-adrs/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| ADR-001…008+ | Immutable decision records | Reviewers, engineers | **Superseded** (never edited) | Decisions, alternatives, consequences |

### `05-rag/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Chunking strategy | Algorithm, parameters, coupling to embedding model | Engineers | Chunker change | Chunking decisions |
| Retrieval design | Lexical/dense legs, fusion, filters, ranking | Engineers | Retrieval change | Retrieval reasoning |
| Reranking policy | Which rankers, when, gated on what | Engineers | Measured nDCG change | Rerank justification |
| Context assembly | Budget, dedupe, structure, truncation | Engineers | Assembly change | Token-budget decisions |
| Citation & grounding | Output contract, verification, abstention | Engineers, security | Output-contract change | Hallucination controls |
| Prompt library | Versioned prompts with rationale | Engineers | Prompt change | Prompt purpose |

### `06-data/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Schema | Physical DDL and rationale | Engineers | Migration | Table design |
| Migrations | Applied-migration history and policy | Engineers, ops | Each migration | Schema evolution |
| Partitioning & sharding | Strategy and escalation triggers | Architects | Scale trigger | Data distribution |
| Lifecycle | Document/version/index state machines | Engineers | State machine change | State transitions |

### `07-api/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| OpenAPI spec | The contract (generated) | Clients, engineers | Endpoint change | Interface |
| Versioning policy | How breaking changes happen | Clients | Policy change | Compatibility rules |
| Error taxonomy | Code list and retryability | Clients | New error code | Error contract |
| Pagination & idempotency | Conventions | Clients | Convention change | Client obligations |

### `08-security/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Threat model | STRIDE + LLM threats, register | Security, reviewers | New data flow or threat | Threat inventory |
| Authn & AuthZ model | OIDC, RBAC+ABAC, policy table | Engineers, security | Policy change | Authorisation decisions |
| Trust boundaries | Where trust starts/ends | Security | Boundary change | Trust assumptions |
| Data classification | P0–P4 handling rules | Everyone | New data class | Handling requirements |
| Secret handling | Where secrets live, rotation, scanning | Engineers, ops | Rotation policy | Secret discipline |
| Security testing | Suites and gates | Security, QA | New threat | Verification of controls |

### `09-reliability/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| SLIs & SLOs | Definitions, targets per tier | Everyone | SLO change | Reliability promises |
| Error budget policy | Burn rates and the freeze policy | Owner | Policy change | Operational consequence |
| Degradation ladder | Levels and triggers | Engineers | Dependency change | Graceful degradation |
| DR & backups | RTO/RPO, restore drill | Ops | Drill result | Recovery capability |
| Dependency policy | What fails how | Engineers | New dependency | Failure handling |

### `10-observability/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Metrics catalogue | Every metric, type, labels, alert | Engineers | Metric change | Telemetry contract |
| Logging policy | Schema, levels, retention, redaction | Engineers | Schema change | Log discipline |
| Tracing design | Span model and sampling | Engineers | Span change | Debuggability |
| Alert catalogue | Alerts, severity, runbook links | Ops | Alert change | Alert→action mapping |
| Dashboards | What each dashboard answers | Everyone | Dashboard change | Operational views |

### `11-testing/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Test strategy | Layers, ownership, gates | Everyone | Gate change | Quality approach |
| Fixtures & factories | Test data strategy | Engineers | Schema change | Test scaffolding |
| Load & failure tests | Scenarios and targets | Engineers, ops | SLO change | Performance assurance |

### `12-performance/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Benchmarks | Measured latencies vs budget | Reviewers, engineers | Optimisation | **Before/after evidence** |
| Capacity model | Where the limits actually are | Architects | Measurement | Scaling boundaries |
| Tuning log | Index params, PG settings, batch sizes | Engineers | Tuning change | Tuning rationale |

### `13-deployment/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Environments | Local/T-2/T-3 definitions | Engineers | Environment change | Configuration differences |
| CI/CD pipeline | Stages, gates, artifacts | Engineers | Pipeline change | Release process |
| Rollback | Procedure and triggers | Ops | Incident | Recovery procedure |
| Configuration | Every setting and its default | Engineers | Setting change | Configuration contract |

### `14-operations/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Runbooks | One per paging alert | Ops | Alert or system change | **Operational procedures** |
| Maintenance | Index rebuild, re-embedding, retention jobs | Ops | Job change | Routine operations |
| Postmortem template | Structure for learning | Everyone | After any Sev-1/2 | Blameless review format |

### `15-incidents/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Incident log | Date, impact, cause, action | Everyone | Each incident | Real history |
| Postmortems | Full write-ups | Everyone | Each Sev-1/2 | Learning |

### `16-evaluation/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Benchmark construction | How the dataset was built, and its biases | Reviewers | Dataset change | Evaluation validity |
| Harness | How to run, reproduce, extend | Engineers | Harness change | Reproducibility |
| Results | Per-slice metrics per commit | Reviewers | Every eval run | Quality over time |
| Model comparisons | Provider/model/parameter comparisons | Reviewers | New candidate | Selection evidence |

### `17-cost/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Cost model | Tiers, unit economics | Owner, reviewers | Price or volume change | Financial exposure |
| Cost per answer | Tracked over time | Owner | Ongoing | Efficiency trend |

### `18-future/`
| Document | Purpose | Audience | Update trigger | Records |
|---|---|---|---|---|
| Scale path | How to reach 10⁸ chunks | Architects | At scale | The escalation plan |
| Multi-region | Architecture, consistency trade-offs | Architects | When required | Future topology |
| Multi-institution | Federation, onboarding, isolation options | Architects | Second tenant | Tenant model evolution |
| Research directions | Things measured but not yet adopted | Reviewers | Opportunity | Deliberately deferred work |

---

## 19.3 Documentation standards

| Standard | Rule |
|---|---|
| **Diagrams in-repo** | Mermaid in Markdown; renders in GitHub and the docs site; diffable; no binary image drift (principle D) |
| **No diagram without a caption** explaining which decision or behaviour it explains | Decoration is prohibited |
| **Every ADR immutable** | Superseded, never edited; a correction is a new ADR |
| **Requirement IDs stable** | Deleted IDs leave gaps; renumbering destroys traceability |
| **Docs in the same PR as the change** | Enforced by a CI check that a changed public contract requires a doc change (NFR-013) |
| **No orphan documents** | A document not linked from the index is deleted |
| **Diagrams reviewed as carefully as code** | A wrong diagram is worse than none: it misleads exactly when it is needed |
| **Measurement claims dated** | "p95 = 180 ms" needs a date and hardware; undated numbers are treated as assumptions |

## 19.4 Documentation effort (honest estimate)

| Phase | Documents | Est. effort | Note |
|---|---|---|---|
| Phase 0 | ~30 files | Complete | This package |
| Phase 1 | ~8 | 1–2 days | Foundations, ADR-009…012 |
| Phase 2 | ~6 | 2–3 days | Ingestion + schema |
| Phase 3 | ~5 | 2 days | Chunking, indexing, migrations |
| Phase 4–6 | ~10 | 4–6 days | Retrieval, generation, citations |
| Phase 7 | ~4 | 3–5 days | Evaluation (the documentation mirrors the harness) |
| Phase 8–10 | ~8 | 3–4 days | Security, observability, performance |
| Phase 11–12 | ~6 | 2–3 days | Operations, deployment |
| Ongoing | — | 1–2 h/week | Runbooks, postmortems, ADRs, journal |

**Total ≈ 20–30 days of documentation work.** That is a real cost and is stated rather than hidden. The
reason it is worth it: for a portfolio project, documentation *is* the primary evidence of engineering
quality, and it is the only part a reviewer can evaluate without running the system.