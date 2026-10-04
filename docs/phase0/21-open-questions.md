# §22 Open Questions

**Rule:** a question is only allowed to remain "we can decide later" if it states (a) what specific
information is missing, (b) who can supply it, (c) what the decision options are, (d) what the default
is if no answer arrives, and (e) which phase it blocks.

**Blocking** = Phase 1 cannot start without a decision. **Non-blocking** = Phase 1 can start; the
decision must be made before a named phase.

---

## 22.1 Blocking questions

| ID | Question | Information needed | Who answers | Default if unanswered | Blocks |
|---|---|---|---|---|---|
| **OQ-001** | Licence for the repository and for model weights | Intended use: employment portfolio vs public open source; any institutional affiliation | **Project owner** | MIT for code; model weights retain their upstream licences (Apache-2.0/MIT); corpora excluded unless licence permits redistribution | Phase 1 (repo metadata) |
| **OQ-002** | Scale target of record | Which numbers are the real goal: 20 institutions / 2 M documents (as modelled), or something else? Registered users per institution? | **Project owner / stakeholder** | Modelled values (ASM-001…005) stand, clearly labelled as assumptions | Phase 3 (partitioning) |
| **OQ-003** | Peak concurrency profile | Historical analytics: DAU ratio, peak concurrency ratio, peak-to-mean | **Stakeholder** (if they exist) | ASM-003 = 0.25, ASM-004 = 0.02, 20:1 peak-to-mean | Phase 10 (load targets) |
| **OQ-004** | Corpus profile: scans, languages, formats | A 100-document sample: format mix, text yield, scanned fraction, language mix | **Stakeholder / corpus owner** | English-only, machine-generated text, 6 % yield (ASM-008). **This is the highest-leverage unknown** | Phase 2 |
| **OQ-005** | Is a hosted LLM API acceptable for the demo? | Whether a third-party free-tier key may be used, given that prompts would leave our infrastructure | **Project owner** (a policy decision, not an engineering one) | No. Local-only; extractive path is the default answer mode | Phase 6 |
| **OQ-006** | Is free-tier GPU notebook compute acceptable for bulk embedding? | Whether Kaggle/Colab free GPU may run batch embedding (ToS, reproducibility, data exposure) | **Project owner** | No. Embedding runs on our own hardware; demo corpus sized to fit | Phase 2 |

## 22.2 Design questions requiring decisions before Phase 4–6

| ID | Question | Information needed | Who answers | Default if unanswered | Blocks |
|---|---|---|---|---|---|
| **OQ-007** | Is URL ingestion in scope at all? | Whether web circulars must be ingested | **Product decision** | **Deferred.** SSRF risk (RISK-012) against low value. File upload only | Phase 4 |
| **OQ-008** | Authorization granularity | Can institutions supply cohort/department ACL metadata, or should everything be institution-readable by default? | **Stakeholder / compliance** | Default: all published documents are institution-readable; restricted documents are institution-private. Simpler and safer than inventing a hierarchy with no data behind it | Phase 3 (schema) |
| **OQ-009** | Benchmark corpus licensing | Which public institutional documents may we redistribute? | **Legal/stakeholder** | Synthetic corpus, with the bias documented in `16-evaluation/benchmark-construction` | Phase 2 |
| **OQ-010** | Answer retention period | How long must queries/answers/citations be retained for audit? | **Compliance** | 90 days, configurable per tenant | Phase 3 (schema) |
| **OQ-011** | Multilingual requirement | Must the system answer in languages other than English, and does the corpus contain them? | **Stakeholder** | English-first; `multilingual-e5-small` selected so multilingual is *possible* without re-chunking | Phase 3 (model choice) |
| **OQ-012** | Frontend framework commitment | Is a specific frontend mandated (e.g. by a prospective employer or institution)? | **Project owner** | React + TypeScript; the API contract is UI-framework-independent either way | Phase 5 |
| **OQ-013** | Policy engine (OPA/Casbin) | Will authorisation rules need to be authored by non-engineers, or change at runtime? | **Project owner** | In-house pure-function policy in the domain layer | Phase 5 |
| **OQ-014** | Write-back integration | Must the system ever write to the registry/SIS (e.g. auto-answer submission)? | **Stakeholder** | No. Read-only. This is also a security control (no agency) | Non-blocking |
| **OQ-015** | **How much quality is lost at degradation level L3 (lexical-only)?** | Measured Recall@20 and nDCG@10 for the lexical leg alone | **Measured in Phase 7** | Unknown. Assumed ~0.55 Recall@20 (**assumed, not measured**) | Phase 11 (whether L3 is presentable) |
| **OQ-016** | OCR requirement | Is any part of the corpus scanned? | See OQ-004 | Out of scope. Low-yield documents are quarantined and reported | Phase 2 |
| **OQ-017** | Which model hosting is acceptable for T-2 | Self-hosted llama.cpp on the same VM, or a separate model host? | **Project owner** | Same VM; generation is opt-in slow mode | Phase 6 |
| **OQ-018** | Structured data depth | Should timetables/course catalogues be queryable relationally (exact, cheap) in addition to semantically? | **Product decision** | Yes for timetable/course lookups — exact relational answers are cheaper and more correct than retrieval for structured queries | Phase 5 |
| **OQ-019** | Feedback collection UX | Should users be able to flag "this is outdated"/"wrong"? | **Product decision** | Yes, minimal (one click). It is the cheapest quality signal available | Phase 5 |
| **OQ-020** | Deployment target for T-2 | Which always-free provider (ARM vs x86; region availability; durable-volumes policy) | **Project owner** | An always-free ARM VM with a **durable volume**; containerised so the provider is swappable | Phase 12 |

---

## 22.3 Questions for the reviewer to answer directly

These do not need the stakeholder — they are architecture questions this package is submitting for
judgement, and each has a defensible default if the reviewer disagrees.

| ID | Question | Default taken if no objection |
|---|---|---|
| **OQ-R1** | Is a modular monolith + workers the right call, or should this be a service decomposition from the start? | B, per [§8](07-decision-matrix.md), with extraction seams pre-agreed |
| **OQ-R2** | Is the weighting in the decision matrix defensible, especially Cost+Operability at 0.26 combined? | Yes; the sensitivity analysis shows B wins under three different weightings |
| **OQ-R3** | Is making the **extractive** path the default on free tiers the right product call, or does it under-sell the system? | Yes, on quality grounds (a quoted clause is often the correct answer) with measurement in Phase 7 to confirm |
| **OQ-R4** | Is it acceptable that the default answer path contains **no LLM**? | Yes — FR-024 is a requirement, and the LLM is an enhancement behind a flag |
| **OQ-R5** | Are the security controls adequate given "no tools, no agency"? | Yes; that decision removes most of the OWASP LLM attack surface rather than mitigating it |
| **OQ-R6** | Is refusing cross-encoder reranking by default the right engineering call? | Yes, given ~33 s/query on CPU ([§4.4](03-scale-model.md)) and a measured admission test |
| **OQ-R7** | Is 99 requirements excessive or appropriate for this scope? | Appropriate; each maps to a test |
| **OQ-R8** | Should the roadmap put evaluation at Phase 7 rather than earlier? | No change — Phase 2 measures the corpus and Phase 4 has a CI gate; a full harness needs a built pipeline. But a *minimal* harness earlier is worth considering |
| **OQ-R9** | Is the assumption set adequate, or is something load-bearing missing? | 20 assumptions registered; ASM-004/008/009 dominate |
| **OQ-R10** | Is "99 % availability on T-2" an acceptable published claim, or should the demo avoid availability claims entirely? | Publish it as "~99 %, no SLA" with the reason stated |

---

## 22.4 Questions deliberately **not** being asked

Recorded because the absence of a question is sometimes a decision.

| Not asked | Why |
|---|---|
| "Which vector database is best?" | The question is premature. The answer depends on measured corpus statistics and a port exists either way |
| "Which LLM is most accurate?" | Answered by measurement in Phase 7, not by opinion in Phase 0 |
| "Should we use Kubernetes?" | Not a question until a trigger fires (§7.7). Asking it now invites a yes by default |
| "Should we use agents?" | Not a question. The absence of agency is a security control. Asking would suggest it is a live option |
| "Do we need a second team?" | Out of our control; the architecture is designed for one |
| "Should we fine-tune?" | No requirement; and the honest answer is "not before we have evaluation data showing retrieval, not generation, is the bottleneck" |

---

## 22.5 The decisions I made on the owner's behalf (flagged for confirmation)

These were resolvable with a defensible default, so they were decided rather than left open. Each is
reversible and each is listed so the owner can override with one word.

| Decision | Default chosen | Rationale | How to override |
|---|---|---|---|
| Monolith + workers over microservices | B | [§8](07-decision-matrix.md) | Say so; the matrix is re-runnable |
| No Redis in Phase 1 | Deferred | 133 QPS sustained; a cache outage should not be an outage | Raise measured QPS |
| No Kubernetes | Deferred | No requirement demands it | Fire a §7.7 trigger |
| No cross-encoder rerank by default | Deferred to GPU tiers | 33 s/query on CPU | Provide GPU; or show measured nDCG gain justifying a small CPU reranker |
| Extractive answer as the default | Yes | 29× faster and often more correct for policy | Prefer generation-first if a capable free LLM is available |
| Chunk size 400 tokens | Yes | Bounded by the 512-token encoder | Choose a longer-context model; then re-benchmark |
| One database | Yes | ACID across publication | Fire the isolation/scale trigger |
| No LangChain/LlamaIndex | Yes | Own the stages we must measure | Reconsider if the pipeline's complexity grows |
| No agents/tools | Yes | Security control | Requires a security review and a genuine requirement |
| URL ingestion deferred | Yes | SSRF risk vs low value | Answer OQ-007 yes; implement with the full SSRF suite |
| Benchmark synthetic-corpus acceptable if needed | Yes, with documented bias | Licence uncertainty | Secure permission for real documents |
| Observability via OpenTelemetry | Yes | Portability | Only if a specific backend feature is required |