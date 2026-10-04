# §23 Architecture Review Checklist

**Purpose.** The gate that must pass before Phase 1 writes application code. Every item states **how to
verify it**, not merely what it is. Items marked ❌ block Phase 1.

**Reviewer:** sign off on the "Reviewer verdict" column. A reviewer who cannot verify an item should
mark it ❌ rather than ✓ — a checklist rubber-stamped is worse than no checklist.

---

## 23.1 Requirements completeness

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 1 | Every function has a requirement ID | Count FRs vs features in the roadmap | ✅ | [§3.1](02-requirements.md) |
| 2 | Every requirement has acceptance criteria a test can fail | Read 10 random ACs; ask "how would this test fail?" | ✅ | AC column throughout §3 |
| 3 | Every requirement has a verification method | Check the Verify column is non-empty | ✅ | Verify column throughout §3 |
| 4 | Every `MUST` appears in a user journey | Cross-check the coverage table | ✅ | [Journey coverage](04-user-journeys.md#journey-to-requirement-coverage-check) |
| 5 | No requirement is orphaned | Automated cross-reference | ✅ | Journey coverage table |
| 6 | Requirements that conflict are identified with a resolution | Read §3.6 | ✅ | [§3.6](02-requirements.md#36-requirement-conflicts-already-identified) |
| 7 | Priorities are honest (`POSTPONED` used where appropriate) | Count `POSTPONED` | ✅ | FR-034 marked POSTPONED |
| 8 | Post-MVP ideas are explicitly out of scope | Read §2.5 | ✅ | [§2.5](01-executive-summary-and-problem.md) |

## 23.2 Scale assumptions

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 9 | Every load-bearing number is `DERIVED`, `ASSUMED` or `MEASURED` — never asserted | Scan §4 for unattributed figures | ✅ | [§4](03-scale-model.md) |
| 10 | Every assumption has an ID, a sensitivity and a verification method | Read the assumption register | ✅ | ASM-001…020 |
| 11 | Calculations are reproducible | Re-derive one chunk-count and one storage figure | ✅ | [§4.2](03-scale-model.md#42-corpus-derivation) shows every step |
| 12 | Contradictions with the brief are stated, not smoothed over | The billion-chunk reconciliation | ✅ | [§4.0](03-scale-model.md#40-reconciling-the-briefs-scale-target-with-reality) |
| 13 | Sensitivity is stated for the highest-impact assumptions | ASM-008 marked highest-sensitivity | ✅ | [§4.8](03-scale-model.md#48-what-breaks-if-the-assumptions-are-wrong) |
| 14 | A plan exists to replace assumptions with measurements | Phase 2's first deliverable | ✅ | [Phase 2](19-implementation-roadmap.md#203-phase-2--corpus-profiling-and-document-ingestion) |
| 15 | Hardware performance figures are labelled as estimates needing measurement | ASM-014…016 + Phase 10 deliverables | ✅ | [§4.3](03-scale-model.md) |

## 23.3 Architecture justification

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 16 | At least three viable options were analysed | Four options | ✅ | [§7](06-architecture-options.md) |
| 17 | Each option has a diagram, components, data flow, pros/cons | Read §7 | ✅ | 4 diagrams |
| 18 | A weighted decision matrix exists with justified weights | Check weights sum to 1.00 and each has a rationale | ✅ | Weights sum verified = 1.00 |
| 19 | The matrix was tested for weight sensitivity | Read §8.4 | ✅ | 3 scenarios |
| 20 | The matrix is not the only arbiter — non-compensatory vetoes exist | Read §8.6 | ✅ | Veto test table |
| 21 | The recommendation explains why the others were rejected | Read §9.11 | ✅ | Restated rejection rationale |
| 22 | Diagrams exist for context, high-level, container and data flows | Count Mermaid diagrams | ✅ | [Diagram register](23-diagram-register.md) |
| 23 | Each diagram explains a decision, not decoration | Spot-check captions | ✅ | Every diagram has a "why" paragraph |
| 24 | Decomposition rules make future extraction feasible | Read §9.10 R1–R8 | ✅ | Pre-agreed extraction order |

## 23.4 Tradeoffs documented

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 25 | Every major decision is an ADR with alternatives and consequences | Count ADRs vs decisions | ✅ | [ADR-001…008](../04-adrs/) |
| 26 | Rejected alternatives are named in each ADR | Read one ADR | ✅ | "Rejected alternatives" sections |
| 27 | Costs of the chosen option are stated, not just benefits | ADR-002, ADR-005 disadvantages | ✅ | Consequence sections |
| 28 | Falsifying conditions ("this changes if…") are recorded | §7.7, §9.7, ADRs | ✅ | Trigger tables |
| 29 | Complexity explicitly rejected has a reason and a revisit trigger | §10.17 | ✅ | Rejected-technology table with triggers |
| 30 | "Decide later" items state the information needed | §22 | ✅ | OQ format |

## 23.5 Security modelled

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 31 | Trust boundaries are drawn and labelled | Read §6 | ✅ | 3 boundary diagrams |
| 32 | Data classification exists | Read §6.4 | ✅ | P0–P4 |
| 33 | Retrieved content is treated as untrusted input | Read Journey H | ✅ | 7 defence layers |
| 34 | The model has no tools/agency/network | Check BND-1 | ✅ | [§6.5](05-system-boundaries.md) |
| 35 | STRIDE applied to components and flows | Read §13 | ✅ | STRIDE diagram |
| 36 | OWASP LLM Top 10 mapped | Read §13.3 | ✅ | All 10 rows mapped |
| 37 | Every threat has mitigation, residual risk and test | Count columns | ✅ | 24 threats, all fields |
| 38 | Accepted residual risks are explicit | Read §13.5 | ✅ | 3 accepted risks with triggers |
| 39 | Authorisation is at the retrieval layer, not post-hoc | Check ADR-007 | ✅ | Journey A/C/B all show pre-filtering |
| 40 | Cache keys include the authorisation context | Check ADR-007 | ✅ | `acl_set_hash` in key |
| 41 | Security suites are specified and blocking | Read §13.6 | ✅ | Gate table |
| 42 | Multi-tenancy isolation options analysed with triggers | Read §6.5 | ✅ | 4 options + escalation triggers |

## 23.6 Failure modes modelled

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 43 | Every component analysed for fail/slow/corrupt/unavailable/incorrect | Read §17 | ✅ | FMEA tables |
| 44 | Degradation order is defined and observable | Read §14.7 | ✅ | 6-level ladder |
| 45 | The hard invariant (no answer without evidence) holds at every level | Check L5 | ✅ | Explicit |
| 46 | Silent quality failure has detection mechanisms | Read §17.10 | ✅ | 6 silent regressions with detectors |
| 47 | Retry policy is classified, not defaulted | Read §9.9 | ✅ | Retryable/non-retryable table |
| 48 | Poison jobs have a DLQ path and replay | Read §17.5 | ✅ | DLQ + replay |
| 49 | Single points of failure are named | Read §14.5 | ✅ | Postgres named as irreducible |

## 23.7 SLOs defined

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 50 | SLIs are user-visible outcomes, not machine metrics | Read §14.2 | ✅ | 10 SLIs |
| 51 | SLOs are set per tier with stated reasons | Read §14.3 | ✅ | 3 tiers |
| 52 | Free-tier SLOs are honestly lower | T-2 at ~99 %, generative not an SLO | ✅ | [§14.3](13-reliability-model.md) |
| 53 | Error budgets have a consumption policy with consequences | Read §14.4 | ✅ | Freeze policy |
| 54 | Latency budget is itemised per stage | Read §4.6 | ✅ | 11 stages |
| 55 | RTO/RPO stated per tier with honest free-tier limits | Read §14.6 | ✅ | 24 h T-2 RPO stated plainly |
| 56 | A correctly-wrong answer is treated as a failure | SLI-05 | ✅ | [§14.2](13-reliability-model.md) |
| 57 | SLOs are marked as reviewable with a date | Read §14.11 | ✅ | 30-day review trigger |

## 23.8 Observability planned

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 58 | Metrics exist for every SLO | Map SLI→metric | ✅ | [§15.2](14-observability-model.md) |
| 59 | Logs are structured, versioned, and PII-free | Read §15.3 | ✅ | Schema v1 + redaction test |
| 60 | Traces cover every pipeline stage | Read §15.5 | ✅ | 18 span types |
| 61 | Alerts are symptom-based with runbook links | Read §15.6 | ✅ | 17 alerts |
| 62 | Dashboards are defined for each audience | Read §15.7 | ✅ | 8 dashboards |
| 63 | Cardinality discipline is specified | Read §15.1/15.2 | ✅ | CI guard |
| 64 | Telemetry content rules are enforced at the collector | Read §15.3 | ✅ | Defence in depth |
| 65 | A support question can be answered end-to-end from telemetry | Walk §15.8 | ✅ | Trace-to-answer walkthrough |

## 23.9 RAG evaluation planned

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 66 | The benchmark corpus strategy is defined | Read §12.2 | ✅ | 13 slices + gold core |
| 67 | Retrieval metrics defined with targets | Read §11.8, §12.3 | ✅ | 10 metrics |
| 68 | Generation metrics defined with targets | Read §12.4 | ✅ | 11 metrics |
| 69 | Safety-critical metrics are deterministic, not model-judged | Read §12.4 | ✅ | 6 of 11 are deterministic |
| 70 | LLM-judge is pinned, calibrated, never security-critical | Read §12.4 | ✅ | Protocol table |
| 71 | Every advanced technique has a numeric admission test | Read §11.1 | ✅ | 20 techniques |
| 72 | Rejected techniques have reasons and triggers | Read §11.1 | ✅ | T8/T10/T17/T18/T19/T20 |
| 73 | CI gates block regressions | Read §12.7 | ✅ | Gate table with tolerances |
| 74 | Before/after reporting is required | Read §12.8 | ✅ | EJD template |
| 75 | Evaluation weaknesses are stated | Read §12.10 | ✅ | 7 weaknesses |
| 76 | Evaluation is early enough to gate later phases | Phase 7, with CI gates from Phase 4 | ✅ | [Roadmap](19-implementation-roadmap.md) |

## 23.10 Cost constraints satisfied

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 77 | No paid service in the MVP | Scan the tech evaluation for defaults | ✅ | All defaults are OSS/self-hosted |
| 78 | Every optional paid service has a free alternative documented | Read §18.5 | ✅ | Full disclosure table |
| 79 | Free-tier limits are stated as limits | Read §18.3 | ✅ | Limitations table |
| 80 | Enterprise migration path is described | Read §18.4 | ✅ | T-3 cost model |
| 81 | Cost-driving levers are identified | Read §18.4 sensitivity | ✅ | Extractive-first is the top lever |
| 82 | Lock-in is assessed per component | Read §10 | ✅ | Lock-in column throughout |
| 83 | Ports exist before any commercial evaluation | Check ADR-004 | ✅ | All providers ported |

## 23.11 Documentation structure established

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 84 | Doc tree designed with purpose/audience/update-trigger | Read §19.2 | ✅ | Per-document table |
| 85 | No empty directories created | Check the filesystem | ✅ | Only populated dirs exist |
| 86 | Diagrams live in-repo as Mermaid | Count `.md` with Mermaid | ✅ | [Diagram register](23-diagram-register.md) |
| 87 | Diagram register maps each diagram to a decision and phase | Read the register | ✅ | 23 diagrams |
| 88 | Documentation effort is estimated honestly | Read §19.4 | ✅ | 20–30 days |

## 23.12 Testing strategy established

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 89 | Test layers defined with scope | Read §10.14 | ✅ | 9 layers |
| 90 | Tests map back to requirements | Read §20.2 traceability | ✅ | Requirement→phase→test |
| 91 | Property-based testing identified for invariants | Read §10.14 | ✅ | Chunking, ACL, version rules, RRF |
| 92 | Security tests are blocking | Read §13.6 | ✅ | All blocking |
| 93 | Load and failure testing planned | Phase 10 | ✅ | Scenarios defined |
| 94 | No mocking of the database where behaviour matters | Read §10.14 | ✅ | Deliberate, with rationale |
| 95 | Test-corpus bias is acknowledged | Read §12.10 | ✅ | Author-written questions listed |

## 23.13 Multi-tenancy

| # | Check | Verify | Status | Evidence |
|---|---|---|---|---|
| 96 | Isolation options analysed without premature choice | Read §6.5 | ✅ | 4 options |
| 97 | Escalation triggers defined | Read §6.5 | ✅ | 4 triggers |
| 98 | Noisy-neighbour risks identified and mitigated | Read §14.9 | ✅ | Concurrency limits + per-tenant SLO |
| 99 | Tenant-aware observability planned | Dashboard 6 | ✅ | Per-tenant views |
| 100 | Tenant export/delete planned | FR-049, FR-010 | ✅ | Both specified |

## 23.14 Reviewer sign-off

| Item | Verdict | Notes |
|---|---|---|
| Requirements complete | ☐ | |
| Scale assumptions explicit and falsifiable | ☐ | |
| Architecture justified and alternatives rejected rigorously | ☐ | |
| Tradeoffs documented with reversal triggers | ☐ | |
| Security modelled with residual risk accepted explicitly | ☐ | |
| Failure modes modelled including silent quality failure | ☐ | |
| SLOs defined honestly per tier | ☐ | |
| Observability mapped to user experience | ☐ | |
| RAG evaluation planned with numeric admission tests | ☐ | |
| Cost constraints satisfied with honest free-tier limits | ☐ | |
| Documentation structure established | ☐ | |
| Testing strategy established and requirement-mapped | ☐ | |

**Phase 1 may begin when:** all §23.1–§23.13 items are ✅ **and** the six blocking open questions
(OQ-001…OQ-006) are answered or their documented defaults are accepted **and** the sign-off above is
signed.

---

## 23.15 Known gaps in this checklist (stated, not hidden)

| Gap | Why it exists | How to close |
|---|---|---|
| No measured numbers for anything in §4 | Nothing has been built | Phase 2 (corpus) and Phase 10 (hardware) |
| No working code to review | Phase 0 forbids code | Phase 1+ |
| Benchmark corpus not yet selected | Licence unknown | OQ-009 |
| No security review by a second person | Single-maintainer reality | Documented limitation; mitigated by data-driven suites so a reviewer can verify the policy table rather than the tests |
| Free-tier provider not selected | Depends on OQ-020 | Phase 12 |

**These are the correct gaps for Phase 0.** A checklist claiming no gaps at this stage would itself be
evidence of insufficient rigour.