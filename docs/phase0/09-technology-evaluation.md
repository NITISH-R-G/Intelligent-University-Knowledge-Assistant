# §10 Technology Evaluation

**Method.** For each capability: enumerate real options, state pros/cons, state free/open-source
feasibility and scale characteristics, state lock-in and the migration cost, then recommend — with the
recommendation's *reversal cost* made explicit.

**Standing rule.** No technology enters the design without a requirement ID. Where the choice is
**provisional** (pending Phase 7 measurement) it is labelled, not hidden.

---

## 10.1 Backend language and framework

| Option | Pros | Cons | Free/OSS | Scale | Lock-in | Reversal cost |
|---|---|---|---|---|---|---|
| **Python** (FastAPI / Django) | ML ecosystem co-located; parsers and model tooling are Python-native; fastest iteration for an AI project; largest hiring/reading pool for this domain | GIL (mitigated by process workers); typing discipline must be enforced; lower raw throughput than Go/Rust for CPU-light paths | Excellent | Per-process CPU work needs multi-process; fine at our profile | Low-medium | Low — logic is portable; only bindings change |
| **Go** | Excellent concurrency and low memory; single static binary; trivially small containers | No local ML ecosystem; would need Python sidecars for every model; more glue code | Excellent | Excellent | Low | **High** — two languages, two deployment paths |
| **Rust** | Best performance and safety | Slow to develop; ML integration painful; disproportionate to this workload | Excellent | Excellent | Low | Very high |
| **TypeScript/Node** | Shares types with the frontend; one language end to end | Weak for numeric/model work; worker threads awkward; parser quality in JS is weaker | Good | Adequate | Low | Medium |

**Decision: Python 3.12 + FastAPI for the API, with explicit multi-process workers.**

Reasoning: the domain *is* AI/retrieval; the parsers (`poppler`, `python-docx`), embedding runtimes
(`ONNX Runtime`, `sentence-transformers`) and evaluation tooling are Python. Choosing anything else
means running two runtimes and two deployment paths for a problem where CPU is not the bottleneck
(retrieval is ~200 ms dominated by Postgres, not by Python). Concurrency comes from process-based
workers plus async I/O, which suits this workload (I/O-bound on the DB, CPU-bounded on embedding in
a separate process anyway).

**Reversal:** the HTTP layer is isolated behind an OpenAPI contract, so the API could be rewritten
against the same contract without touching the domain.

*Mitigation for the GIL:* embedding/reranking runs in a **separate worker process**; the API handles
I/O-bound request orchestration. The GIL is not on the critical path.

## 10.2 Frontend

| Option | Pros | Cons | Free/OSS | Verdict |
|---|---|---|---|---|
| **React + TypeScript + Vite** | Largest ecosystem; SSE/streaming patterns well documented; accessible component libraries | Bundle size; build complexity | Excellent | **Chosen** |
| Svelte/SvelteKit | Smaller, faster, simpler | Smaller ecosystem; fewer accessible components | Excellent | Viable alternative |
| Next.js | SSR, routing | Server runtime we do not need (the API is separate); more moving parts | Excellent | Not justified — we do not need SSR |
| Server-rendered HTMX | Simplest, fastest to build, excellent accessibility baseline | Less suitable for streaming answer UI with inline citations | Excellent | Strong runner-up; reconsidered if UI complexity grows |

**Decision: React + TypeScript + Vite**, with the UI treated as a separate deployable static app.
Justification is not framework popularity: the answer UI needs streaming token rendering with citation
anchors that scroll to source — a reactive client problem. `HTMX` remains attractive for the admin
console, and we keep the API contract independent of the UI framework so that choice can be revisited
(`OQ-012`).

## 10.3 Primary database

| Option | Pros | Cons | Free/OSS | Scale | Lock-in |
|---|---|---|---|---|---|
| **PostgreSQL 16 + pgvector** | ACID across the whole domain; FTS built in; **vectors and lexical index and queue in one engine**; one backup story; RLS for tenancy; excellent tooling | HNSW build is CPU/IO heavy; ANN tuning needed; no native multi-region; ceiling ~10⁸ chunks single-node | Excellent (and free tiers exist: Neon, Supabase, self-hosted, Oracle Always Free) | Strong to ~10⁸ chunks | Low-medium (SQL + pgvector syntax) |
| Qdrant / Milvus / Weaviate (specialised vector DB) | Purpose-built ANN; better recall/latency at large scale; filtering primitives | **A second data store**: cross-store consistency, two backups, two failure modes, no shared transaction with documents | Mostly OSS; managed tiers paid | Better at 10⁸–10⁹ | **High** |
| Elasticsearch / OpenSearch | Excellent lexical search, hybrid via script_score | Heavy operational footprint, memory-hungry, poor fit for a free 1–2 GB VM; second store again | Excellent | Excellent | High |
| SQLite | Zero-ops | Single writer; no concurrent scale | Built in | Poor | Low |
| MySQL | Familiar | **No mature vector support**; FTS weaker | Good | Good | Medium |

**Decision: PostgreSQL + pgvector** ([ADR-002](../../docs/04-adrs/ADR-002-postgresql-pgvector-as-the-single-data-store.md)).

The decisive argument is **one engine for documents, chunks, vectors, lexical index, job queue and
audit log** — which preserves ACID across "publish a new version and retire the old one"
(FR-009/FR-012). Splitting vectors into a specialised store means every publish becomes a
two-store, non-atomic operation, and a failure between them leaves the corpus internally inconsistent
— the *one* failure mode this project cannot tolerate.

Honest limitation: at 10⁸+ chunks, pgvector's HNSW recall/latency and build time degrade, and this is
where the design escalates to a specialised vector store behind `VectorStore`. The abstraction already
exists ([§9.10](08-recommended-architecture.md#910-decomposition-rules--how-b-becomes-c-without-redesign)),
so that escalation is an adapter, not a redesign.

**Chosen configuration:** partitioned `chunks` table (by tenant), `hnsw` index built on a **shadow
table and swapped atomically** (never in-place `REINDEX` while serving), `vector` stored as
`real[]` with fp16 storage where supported, and HNSW parameters tuned in Phase 10 against measured
recall.

## 10.4 Search component (lexical leg)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **PostgreSQL FTS (`tsvector`)** | Same engine, same transaction, same filter/ACL predicate — no cross-store inconsistency; language configs; GIN index | Recall on long/complex queries weaker than BM25; needs `websearch_to_tsquery`/config tuning | **Chosen** |
| Elasticsearch BM25 | Better lexical quality, analyzers, highlighting | Second store, operational weight, memory profile unfriendly to free tier | Rejected now; re-evaluate if lexical Recall@20 < 0.85 on the benchmark |
| OpenSearch | Same as ES, permissive licence | Same costs | Deferred |
| `pg_search` extension (ParadeDB, BM25 in Postgres) | BM25 inside Postgres — best of both | Extension availability on free tiers is uneven | **Strong candidate**; evaluate in Phase 7, adopt behind `LexicalSearchPort` if quality wins |

## 10.5 Object storage

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **S3-compatible API (MinIO self-hosted / any S3-compatible provider)** | Standard protocol; presigned URLs; free tier exists; local dev identical to prod | MinIO's licence/recent governance changes are a genuine concern | **Chosen, via the `ObjectStore` port** |
| Local filesystem | Simplest | Breaks the signed-URL model; no safe sharing; no multi-node | Dev-only fallback |

**Decision:** the `ObjectStore` port with an S3 adapter; MinIO for local development. The recent
MinIO governance/licensing situation is precisely why this port exists — swapping to any other
S3-compatible store is a configuration change, not a code change. Directories keyed by
`tenant/document/version` so deletion is a bounded prefix operation.

## 10.6 Queue and background processing

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **PostgreSQL job table (`FOR UPDATE SKIP LOCKED`)** | Zero new infrastructure; **exactly the same transaction as the state it changes**; visible in the same console; DLQ is a status column | Polling overhead; ceiling ~1–5 k jobs/s (we need 10/s) | **Chosen** |
| Redis Streams | Fast; simple consumer groups | Another store with its own durability and persistence story; not needed at 10 jobs/s | Deferred ([ADR-003](../../docs/04-adrs/ADR-003-no-redis-in-phase-1-deferred.md)) |
| RabbitMQ / NATS | Mature, good routing | Another service to run and secure on a 1–2 GB free VM | Deferred until queue depth genuinely needs it |
| Kafka / Redpanda | Throughput and replay | Operationally heavy; no requirement needs it; eventual-consistency hazards | Rejected — see [ADR-001](../../docs/04-adrs/ADR-001-modular-monolith-with-asynchronous-workers.md) |

**Decision: PostgreSQL job queue**, with a `JobQueuePort` so a broker can replace it. Chosen because
claiming a job and writing its result can be **one transaction** — which is what makes
exactly-once-effect ingestion possible with at-least-once delivery (OPS-004).

## 10.7 Cache

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **In-process cache (TTL dict) + Postgres for durable state** | Zero infrastructure; correct at our scale; ACL-namespaced by construction | Per-replica; needs a cache stampede guard (single-flight + jitter) | **Chosen for Phase 1** |
| Redis | Shared across replicas, better hit rate | New component with new failure modes; at 133 QPS sustained the win is small; a cache outage should not be an outage | Deferred behind `CachePort` ([ADR-003](../../docs/04-adrs/ADR-003-no-redis-in-phase-1-deferred.md)) |
| CDN | Perfect for static assets | Cannot cache per-principal answers | Used for static assets only |

**No Redis until a measurement justifies it.** Trigger: p95 hit rate < 30 % across ≥ 3 replicas, or
> 200 QPS sustained.

## 10.8 Embedding model

Candidates, all with permissively licensed weights so the free/OS constraint holds:

| Model | Params | Dim | Max ctx | Multilingual | Notes |
|---|---:|---:|---:|---|---|
| `bge-small-en-v1.5` | 33 M | 384 | 512 | No | **Fastest viable on CPU** (4.5 chunks/s on 4×A1) |
| `bge-base-en-v1.5` | 109 M | 768 | 512 | No | Middle ground |
| `multilingual-e5-small` | 118 M | 384 | 512 | Yes | **Candidate default** — multilingual, 1.3 chunks/s on CPU |
| `bge-m3` | 568 M | 1024 | 8192 | Yes | Best quality + long context, but **0.22 chunks/s on CPU** → GPU tiers only |
| `jina-embeddings-v3` | 570 M | 1024 | 8192 | Yes | Requires `trust_remote_code` → supply-chain risk (`SEC-012`); declined |
| `text-embedding-3-small` (hosted) | — | 1536 | 8191 | Yes | Paid; no free tier; sends text to a third party |

**Decision (provisional, benchmark-gated):**
- **Bulk indexing: `multilingual-e5-small` (384-d) on CPU tiers; `bge-m3` (1024-d) on GPU tiers.**
- **Query-time: the largest model we can afford per query**, because query embedding costs ~18 ms
  while bulk embedding costs days ([§4.3](03-scale-model.md#43-embedding-throughput--the-real-constraint)).
  The mismatch is legal **because every chunk records its `embedding_model_id`** and the system
  refuses to compare vectors from different models.

**Two hard constraints:**
1. **Chunk size ≤ model max sequence length.** Most 512-context models silently truncate an 800-token
   chunk, so the tail is never embedded and never retrievable. Chunking config is therefore *bound to*
   the embedding model and versioned (FR-013).
2. **Embedding model change = index version change** → full re-embedding, made a background job with a
   shadow index and an atomic swap (FR-014).

## 10.9 LLM

| Option | Pros | Cons | Free | Notes |
|---|---|---|---|---|
| **Local `llama.cpp` / Ollama, Qwen2.5-1.5B/3B-Instruct (Q4)** | Zero marginal cost, **no data egress**, offline, GGUF is permissively licensed | ~60 s end-to-end on 4×A1 ([§4.7](03-scale-model.md#47-generation-feasibility-on-the-free-tier)); quality limited | Yes | **Default on T-1; opt-in slow mode on T-2** |
| Local 7–8B on GPU | Acceptable latency (~2.5 s), much better quality | Needs a GPU | Only via free-tier GPU notebooks (fragile, ToS) or a paid instance | T-3 |
| Hosted free-tier APIs (Gemini/Groq/etc.) | Excellent quality, fast | **Prompts leave our infrastructure** — unacceptable for restricted institutional documents; free tiers have ToS/limits/rate limits; availability is not ours | Free tier exists | **Optional, off by default**, `OQ-005` |
| Hosted paid APIs | Best quality/effort | Cost at scale ([§18](17-cost-model.md)); egress; lock-in | No | Documented escape hatch, not the default |

**Decision:** local-first behind `LLMProvider`. The rule is **no document text leaves the trust boundary
by default** — that is a security requirement (`SEC-004`, BND-1), not a preference. A hosted adapter
exists for portability and is disabled unless a tenant explicitly opts in.

## 10.10 Reranker

| Option | Pros | Cons | Cost on our hardware | Verdict |
|---|---|---|---|---|
| **Structural/lexical reranker** (exact-term coverage, section match, doc-type prior, recency) | Microseconds; no model; explainable | Weaker than a cross-encoder on semantic subtlety | Free | **Default on all tiers** |
| MiniLM-scale cross-encoder | Real cross-attention signal | 2.3 s on 4×A1; 450 ms on a laptop | — | Optional on laptop/GPU |
| `bge-reranker-base` (278 M) | Strong | **33 s on 4×A1, 5.6 s on a laptop** ([§4.4](03-scale-model.md#44-reranking--infeasible-on-cpu-so-it-is-not-a-default)) | Prohibitive | GPU tiers only, flag-gated, **admitted only if ΔnDCG@10 ≥ 0.02** |
| Hosted rerank API | Good quality | Cost + egress per candidate | Paid | Not used |

**Decision: structural reranker everywhere; cross-encoder as a measured, flag-gated GPU feature.**
This is the clearest place in the project where "add a reranker" is the wrong answer, and we have the
arithmetic to say why.

## 10.11 Document parsing

| Format | Chosen tool | Why | Fallback / risk |
|---|---|---|---|
| PDF | `poppler` (`pdftotext -layout`) via a bound library, or `pypdfium2` | Mature, fast, preserves layout | Scanned PDFs yield little text → detect low yield and flag for OCR (**out of scope**, `OQ-004`) |
| DOCX | `mammoth` / `python-docx` | Preserves heading structure — critical for section paths | Complex layouts (text boxes) degrade |
| HTML | `readability`-style main-content extraction | Strips nav/ads/boilerplate | Malicious HTML: sanitise before parsing |
| TXT / MD | Native | Trivial | Encoding detection needed |
| CSV | `csv` module with dialect sniffing | Structured; routed to the relational path, not embeddings | Formula injection on **export** is the risk, not ingest |

**All parsing runs in the sandboxed subprocess** (no network, no credentials, hard limits) because
these libraries parse attacker-controlled binaries and have a long CVE history (`SEC-007`).
**Pervasive-parsing detection** (a PDF that is 95 % identical to another) is a quarantining signal
(THR-008).

## 10.12 Authentication and authorisation

| Concern | Choice | Why |
|---|---|---|
| Authentication | **OIDC / OAuth 2.1** with a hosted provider (Authentik/Keycloak self-hosted, or a hosted free IdP) | We must not build password storage ([SEC-001]). Self-hosting keeps data on our infrastructure. |
| Token format | Short-lived JWT access tokens + refresh rotation | Stateless verification; horizontal scaling needs no session affinity |
| Authorisation | **In-house, in the domain layer**: RBAC **+** ABAC over (tenant, role, department, cohort, ACL set) | Vendor-neutral policy engines (OPA/Casbin) were considered and **deferred**: they add a service and a policy language, and our policy is not that dynamic yet. Revisit if policy authoring becomes a product requirement (`OQ-013`) |
| Enforcement | Data-access layer injects tenant scope **+** PostgreSQL RLS as an independent second control | One control is one bug away from a leak ([BND-2](05-system-boundaries.md)) |
| Session/UI | Same-origin, httpOnly cookies for the web app; bearer tokens for API clients | Reduces token exposure in the browser |

## 10.13 Observability

| Concern | Choice | Free/OSS | Notes |
|---|---|---|---|
| Instrumentation | **OpenTelemetry** (traces, metrics, logs) | Excellent | Vendor-neutral — the single best lock-in hedge in this project |
| Metrics | Prometheus + Grafana | Excellent | Grafana Cloud free tier available if hosting is unwanted |
| Logs | JSON to stdout → **Loki** (or any sink via `LogPort`) | Excellent | Schema versioned; PII redaction tested |
| Traces | **Tempo** (OTel-native, Grafana-coupled) | Excellent | Retention cost grows with sampling; sample intelligently |
| Collection | OTel Collector | Excellent | Single egress point; also the natural place to redact |
| Errors | Sentry self-hosted or OSS-compatible | Yes | Optional; error tracking with request-ID linkage |
| Alerts | Alertmanager | Excellent | **Every paging alert links to a runbook** (`OPS-010`) |

**Decision: OpenTelemetry everywhere.** Its portability is the deciding factor: it is the only
observability choice where leaving later is a config change rather than a rewrite.

## 10.14 Testing

| Layer | Tool | Scope | Note |
|---|---|---|---|
| Unit | `pytest` | Domain logic: authorisation, chunking, RRF, versioning rules, citation verification | Fast, deterministic, no I/O |
| Property-based | `Hypothesis` | **Chunking idempotency, ACL predicate construction, version-selection rules, RRF monotonicity** | Where invariants matter more than examples |
| Integration | `pytest` + real Postgres (containerised) | Retrieval correctness, migrations, queue semantics | Real DB, not a mock: the ACL and index behaviour *is* the product |
| API/contract | Schemathesis-style fuzzing against the OpenAPI schema | Contract conformance | Catches input the hand-written tests missed |
| Retrieval eval | Custom harness + fixed benchmark | Recall@K, nDCG, MRR, latency | CI-gated |
| Answer eval | Groundedness + citation correctness + abstention, deterministic checks where possible | Regression on hallucination | LLM-judge only where a deterministic check is impossible; **judge is pinned and versioned** |
| Security | Bandit, `pip-audit`, Semgrep, plus the custom injection/ACL suites | TH-01…TH-08 in the threat model | Custom suites matter more than scanners |
| Load | Locust / k6 | PERF-001…009 | Nightly |
| E2E | Playwright | Critical user journeys A, C, D, G | Browser-level proof |

**Deliberate non-choice:** no mocking framework for the database. A mock Postgres cannot tell us
whether an HNSW index returns the right documents — and that is the entire product.

## 10.15 Deployment and CI/CD

| Concern | Local dev | Free demo | Production-scale |
|---|---|---|---|
| Orchestration | Docker Compose | Single VM + Compose, or systemd | Compose on N VMs is sufficient at our scale; Kubernetes only at a §7.7 trigger |
| CI | — | **GitHub Actions** (free for public repos) | Same |
| CD | — | GitHub Actions → pull-based VM deploy | Same + blue/green behind a proxy |
| IaC | — | Ansible or plain cloud-init scripts | Terraform at scale |
| Secrets | `.env` locally, **never committed** | Secret store or env file with 600 perms | Secret manager + rotation |
| Migrations | `alembic`/`migrations` on start | Applied by deploy job with a lock | Online, backwards-compatible, zero-downtime |
| Feature flags | Local config | Config file + per-tenant overrides | External flag service at scale |

**Decision: Docker Compose as the primary orchestration mechanism for Phases 1–11.** Kubernetes is
explicitly rejected for now ([§7.7](06-architecture-options.md#77-what-would-change-the-answer)):
adding it would be resume theatre, not engineering, because nothing in §3 requires it.

## 10.16 Cross-cutting: languages and tools selected

| Concern | Choice | Rationale |
|---|---|---|
| Typing | `mypy` strict (or pyright), `any` banned in domain code | NFR-001 |
| Linting/formatting | `ruff` | Fast, single tool, replaces flake8+black+isort |
| Testing | `pytest` + `Hypothesis` | Ecosystem fit |
| Migrations | Alembic (or framework-native) | Versioned, reversible, diffable |
| Config | Typed settings object, validated at startup | OPS-014 |
| Observability SDK | OpenTelemetry Python | Portability |
| Contracts | OpenAPI 3.1 generated from types, diffed in CI | Prevents silent breaking changes |
| IaC/docs | Mermaid in-repo; `mkdocs` for the doc site | Diagrams live in version control (principle D) |

## 10.17 Decision summary — what is *not* in the stack, and why

| Rejected | Reason | Trigger to revisit |
|---|---|---|
| Kubernetes | No requirement; operational cost on a solo project | > 100 QPS sustained, or > 3 deployables needing independent scaling |
| Kafka/Redpanda | Throughput need is 10 jobs/s | Event replay becomes a product requirement |
| Redis | Duplicate-cache hit rate insufficient to justify a component | Hit rate < 30 % across replicas, or > 200 QPS |
| A dedicated vector database | Postgres + pgvector is sufficient to ~10⁸ chunks and preserves ACID | Corpus > ~10⁸ chunks, or measured recall degrades |
| Elasticsearch | Second store; poor fit for a free-tier memory budget | Lexical Recall@20 < 0.85 on the benchmark |
| LangChain / LlamaIndex | The retrieval pipeline here is ~5 well-defined steps with explicit scoring, timing and evaluation; a framework would hide exactly the behaviour we need to measure and would couple the domain to a fast-moving dependency | Never, unless the pipeline grows complexity these libraries demonstrably reduce |
| A dedicated vector-search library in Python (e.g. FAISS) | We have pgvector in the transactional store; a second index means a second consistency problem | Not planned |
| An agent framework | No requirement; and the absence of agency is a security control ([BND-1](05-system-boundaries.md)) | Never, unless a genuine requirement appears |
| Redis Pub/Sub for cache invalidation | Invalidatable by index version in Postgres | — |

**On LangChain/LlamaIndex specifically:** the strongest argument for using one is speed of assembly; the
strongest argument against is that this project's core deliverable is *measured retrieval quality and
observable stage-by-stage latency*, both of which a framework abstracts away and both of which we must
own. Given that the pipeline is small, a framework would be a dependency on someone else's roadmap in
exchange for saving perhaps 200 lines. Rejected — and the rejection is recorded so that revisiting it
is a deliberate act rather than drift.