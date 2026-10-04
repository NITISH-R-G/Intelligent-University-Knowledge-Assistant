# §1–§2 Executive Summary and Problem Definition

---

## 1. Executive summary

### 1.1 What we are building

An **Intelligent University Knowledge Assistant**: a multi-tenant, version-aware Retrieval-Augmented
Generation (RAG) platform over institutional document collections. It ingests heterogeneous
documents (PDF, DOCX, TXT, HTML, and structured records such as timetables and course catalogues),
converts them into a versioned, hybrid-searchable knowledge base, and answers natural-language
questions with **grounded, cited answers that abstain when the corpus does not support an answer**.

The distinguishing property is not "a chatbot over PDFs". It is that the system treats institutional
knowledge as **versioned, permissioned, temporally-scoped data**. A regulation has an effective date
and a supersession chain. A handbook applies to one cohort. A circular is superseded but still
legally relevant for the period it covered. The system must be able to say *"this is what the rule
was between March and September 2025, and this is what it is now"* — and cite both.

### 1.2 Who uses it and why it exists

| User | Problem today | What the system gives them |
|---|---|---|
| Student | Asks a registrar or a senior, waits hours, gets an inconsistent verbal answer | A cited answer in under a second, with the clause and the effective date |
| Lecturer / TA | Repeats the same policy questions every term | One authoritative answer, with the document shown |
| Registry / administrator | Publishes a circular; nobody finds the updated version | Upload → parse → index → published, with a visible version history and rollback |
| Compliance / privacy officer | Cannot prove which document a user was shown | Immutable answer + citation + retrieval trace, retained and exportable |
| Platform engineer | Cannot evaluate whether retrieval got better or worse | A benchmark with recall/nDCG/groundedness gates in CI |

**Core value proposition:** *the fastest correct answer to a question about institutional policy,
with a citation that a human can check in one click, and a refusal when the answer is not knowable
from the corpus.*

The refusal is a feature. A system that answers everything is a system that is confidently wrong at
3am the night before an exam — the exact moment a university knowledge system is most damaging.

### 1.3 What is technically interesting here

Not the RAG pattern. The following are the parts that require real engineering judgement:

1. **Versioned, time-aware retrieval over policy documents.** A naive RAG index returns the
   lexically-best-matching chunk regardless of whether it has been superseded. Correct behaviour
   requires a supersession graph, effective-date filtering, and an explicit "as-of" query semantic.
   Most production RAG systems get this wrong and are therefore wrong precisely when it matters.
2. **Authorisation as a retrieval filter, not a post-processing step.** Every chunk carries resolved
   access-control principals; the ACL predicate is pushed into the index scan. Filtering *after*
   generation is a data-leak vulnerability, not a UX detail. This also has a non-obvious
   consequence: the answer cache key must include the principal set, or it becomes a cache-poisoning
   primitive ([ADR-007](../../docs/04-adrs/ADR-007-authorisation-enforced-at-the-retrieval-layer.md)).
3. **A free-hardware latency budget that changes the architecture.** Analytical modelling
   ([§4](03-scale-model.md)) shows a cross-encoder reranker costs ~33 s of CPU per query on the
   free tier, and a local LLM cannot meet any reasonable answer-latency SLO. Rather than hiding this
   behind a paid API, the design makes an **extractive answer path a first-class, separately
   evaluated capability** and puts generation behind a per-tier feature flag. University regulations
   are frequently answered by quoting a clause, so the "cheap" path is also frequently the *right*
   path.
4. **Chunk size is coupled to the embedding model's context window.** Most 384-dim embedding models
   cap at 512 tokens; an 800-token chunk is silently truncated and the tail is never embedded. The
   system therefore versions the chunking configuration and binds it to the embedding model ID, and
   refuses to ingest into an index whose model does not match the chunks' provenance.
5. **Ingestion is content-addressed and idempotent.** Re-uploading an unchanged document is a no-op;
   re-uploading a changed document creates a new immutable version, re-embeds only the chunks whose
   content actually changed, and atomically flips a pointer. This is what makes "repeated ingestion
   does not create duplicate knowledge" ([OPS-004](02-requirements.md)) true rather than aspirational.

### 1.4 What makes it production-grade

Production-grade here means specific, testable properties — not a posture:

- **Every retrieval is reproducible.** Query, filters, candidate IDs, scores and the prompt are
  persisted with the answer. A support ticket can be replayed.
- **Every ingestion is auditable and reversible.** Version history, content hash, parser version,
  chunking version and embedding model ID are recorded per version; a bad publish is a rollback,
  not an incident.
- **The system degrades in a defined order.** LLM unavailable → extractive answers. Vector index
  unavailable → lexical retrieval. One malformed PDF → quarantined with a reason, pipeline continues.
- **Security is modelled as an attack surface, not a checklist.** Retrieved documents are treated as
  untrusted, attacker-controlled input, including their metadata and filenames
  ([§13](12-threat-model.md)).
- **Quality is a number, not an opinion.** Recall@K, nDCG@10, groundedness, citation correctness and
  unsupported-claim rate are measured on a benchmark and gated in CI
  ([§12](11-rag-quality-strategy.md)).
- **Free is stated as free, including its limits.** The free tier's real availability, latency and
  throughput are documented, with the enterprise delta spelled out
  ([§18](17-cost-model.md)).

### 1.5 The single most important sentence in this document

> The system's correctness is determined by what it *refuses* to answer, not by what it answers.
> Everything below — versioning, ACLs, abstention, evaluation, observability — exists to protect
> that property.

---

## 2. Problem definition

### 2.1 Problem statement

> University institutional knowledge is distributed across thousands of documents in formats that
> resist search, maintained by many departments with no shared source of truth, and constantly
> revised. A student who needs to know a rule must either guess which document is authoritative or
> ask a human. The result is avoidable administrative load, inconsistent guidance, and real
> consequences for students who act on outdated information.

Three underlying failure modes make this a hard problem rather than a search problem:

| Failure mode | Why keyword search does not fix it |
|---|---|
| **Authority ambiguity** | Five circulars mention attendance. Which one applies, to whom, and when? Requires metadata + version semantics, not ranking. |
| **Paraphrase mismatch** | Users ask "can I sit a module exam in January?" about a document titled *Guidelines for Deferred Examinations*. Requires semantic retrieval. |
| **Partial evidence** | The answer requires combining the handbook (eligibility) with the registry circular (dates). Requires multi-hop assembly with per-claim citations. |

### 2.2 User personas

| Persona | Description | Primary need | Authorisation level |
|---|---|---|---|
| **P1 — Student** | Undergraduate/postgraduate, high query volume, exam-period spikes, low tolerance for slow answers | Fast, cited, plain-language answer | Read: public + own-cohort restricted |
| **P2 — Lecturer / Academic staff** | Needs policy answers plus own course/department documents | Same as P1, plus department scope | Read: public + department + own-authored |
| **P3 — Registry administrator** | Publishes circulars and timetables; owns correctness of official text | Upload, revise, supersede, roll back, preview impact | Write: own department documents |
| **P4 — Institution administrator** | Sets retention, publication windows, user provisioning | Global read + policy configuration | Admin: institution scope |
| **P5 — Platform operator / SRE** | Runs the system, on call | Dashboards, runbooks, safe rollback | Read: telemetry; no document access by default |
| **P6 — Privacy / compliance officer** | Answers "what was this user told, and from what source?" | Immutable answer + citation audit trail | Read: audit records only |

Note P5: **the operator role deliberately has no document-content read access by default.** Support
engineers need to read telemetry, not student records. This is a real design decision with a real
support cost, and it is recorded because it will be challenged during incident response.

### 2.3 Primary use cases

| ID | Use case | Success = |
|---|---|---|
| UC-1 | Factual policy question ("What is the minimum attendance?") | Cited answer, < 1 s, correct effective version |
| UC-2 | Rule lookup by entity ("What are the prerequisites for CSC402?") | Structured answer from course catalogue, cited |
| UC-3 | Multi-source question ("Am I eligible to resit, and when?") | Combined answer, each claim cited to its own document |
| UC-4 | Administrative publication (upload → publish) | Document searchable within a stated freshness SLO |
| UC-5 | Administrative revision (supersede) | New version live, old version retained and still retrievable as-of its window |
| UC-6 | Deletion / withdrawal | Content and derivatives removed and provably so, within a stated window |

### 2.4 Secondary use cases

| ID | Use case | Notes |
|---|---|---|
| UC-7 | Timetable / structured lookup | Handled by structured extraction + relational store, **not** by embeddings |
| UC-8 | Web/URL ingestion | Deferred to Phase 4 — highest SSRF risk, lowest value (`OQ-007`) |
| UC-9 | Cross-institution federation | Designed for, not built; tenancy is a first-class dimension from day one |
| UC-10 | Audit export ("show me what P1 was told on 3 March") | Required for compliance; drives the answer-record schema |
| UC-11 | Bulk corpus seeding for demos | Pre-embedded seed corpus so a reviewer can run in minutes, recomputable from source |

### 2.5 Explicitly out of scope

Naming what will not be built is more valuable than naming what will.

| Out of scope | Reason | Revisit |
|---|---|---|
| Autonomous agents with tool use / write actions | OWASP LLM06 excessive agency; the value is in trustworthy answers, not autonomy. No requirement demands it. | Not planned |
| Fine-tuning or model training | No requirement; the corpus is the knowledge, not the weights. | Not planned |
| Kubernetes, service mesh, Kafka | No requirement forces them. Complexity without evidence. | At scale gate, with data |
| Full-text editing of source documents | The system is a knowledge *consumer*. A document of record stays in its system of record. | Never |
| OCR of scanned images | High cost, low value on the target corpus; realistic but not required. | If corpus is scanned (`OQ-004`) |
| Real-time collaborative features | Unrelated to the problem. | Never |
| Writing back to the registry / SIS | Requires trust federation we do not have; risky. | `OQ-014` |
| Speech / multilingual UI | Model support exists; corpus support is uncertain. | `OQ-011` |

### 2.6 Assumptions

These are **assumptions, not facts**. Each states its impact if wrong.

| ID | Assumption | Impact if wrong | How to verify |
|---|---|---|---|
| A-01 | Target corpus is predominantly **machine-generated text** PDFs/DOCX (not scans) | OCR pipeline required; ingestion 10–50× slower; changes §4 storage and §20 Phase 2 | Inspect a 100-document real sample (`OQ-004`) |
| A-02 | Corpus is **predominantly English**, with a meaningful minority needing translation | Multilingual model + cross-lingual eval needed; retrieval quality drops in the minority languages | Language census of sample (`OQ-011`) |
| A-03 | Documents are **semistructured** (headings, clauses, numbered sections) | Semantic chunking + parent/child retrieval pay off; flat chunking would be enough otherwise | Measured in Phase 2 on real samples |
| A-04 | Each institution can supply **authoritative ACL metadata** (or we default to all-institution-read) | Over-engineered authorisation UI with no data behind it | Stakeholder confirmation |
| A-05 | Answers must be **citable to a specific document version**, not just a document | Changes the answer schema and the audit model; without it, UC-10 is impossible | Legal/compliance confirmation |
| A-06 | Administrators will tolerate a **publish-then-live** workflow (review before indexing) | Auto-publish changes the trust model for untrusted content | Workflow interview |
| A-07 | A **single active version** per document plus a supersession chain is sufficient (no parallel in-force variants) | Needs a variant dimension and a combinatorial explosion of version selection | Domain expert review |
| A-08 | Peak load is **spiky** (exams, admissions) with a 20–40× peak-to-mean ratio | Determines whether autoscaling is needed or a queue with backpressure suffices | Historical web analytics if available |

### 2.7 Constraints

| Constraint | Consequence for the architecture |
|---|---|
| **Free/open-source only for the implementation** | No managed vector DB, no paid LLM in the reference deployment; model serving is local or optional-hosted behind an interface |
| **Single-operator maintainability** | Fewest components that can be justified; no on-call rotation across teams |
| **Must be demonstrable and reproducible by a reviewer on a laptop** | Seeded corpus + pre-computed embeddings + one-command bring-up is a requirement, not a convenience |
| **No production data** | Evaluation corpus must be synthetic-but-realistic or public; this is the largest single risk to the evaluation strategy ([RISK-011](20-risk-register.md)) |
| **Privacy** | Student-identifiable data must not enter the retrieval corpus; answers may reference identity only via the audit record |
| **Timebox** | Portfolio-grade depth over feature breadth; a smaller system done properly beats a larger one done shallowly |

### 2.8 What "done" means for this project

A reviewer can, in under 15 minutes: clone the repo, run one command, ask a question, get a cited
answer, and then read the evaluation report showing that the answer is grounded. A hostile reviewer
can upload a prompt-injection document, show that it is quarantined, and read the test that proves it.
An engineering reviewer can read the ADRs and find that each states what was rejected and why.
