# §18 Cost Model

**Rules for this section.**

1. No paid service enters the MVP. Where a paid option appears, a free/open alternative is stated
   first and the paid option is marked optional and off by default.
2. Free-tier limits are stated **as limits**. A free tier that does not offer an SLA does not get a
   99.9 % SLO in this project.
3. Prices are `ASM` — planning assumptions that must be re-verified against current published pricing
   **before any purchase**. Free-tier offerings change frequently; a stale price in a portfolio
   document is worse than no price.
4. On a free-first project the dominant cost is **human time**, not money. That is measured honestly.

---

## 18.1 Tiers of cost

| Category | Meaning | Examples in this project |
|---|---|---|
| **FREE** | No monetary cost, run locally or on a permanently free resource | PostgreSQL, pgvector, MinIO, llama.cpp, ONNX Runtime, FastAPI, React, OpenTelemetry |
| **OPEN SOURCE** | Free software with a licence obligation | Everything above, plus `bge-m3`/`e5-small` weights, `poppler` |
| **SELF-HOSTED** | Free software on hardware we operate | The entire T-1/T-2 stack |
| **FREE TIER** | A provider's no-cost allowance with an SLA we do not control | Always-free VM, managed DB free tier, GitHub Actions on public repos, free-tier LLM APIs |
| **OPTIONAL PAID** | Purchased only if a stated trigger fires | GPU instance, managed Postgres, hosted LLM, object storage egress |

---

## 18.2 T-1 — Developer laptop (the primary demo tier)

| Component | Category | Monetary cost | Human cost |
|---|---|---|---|
| PostgreSQL + pgvector | Open source | **$0** | Setup |
| Object store (MinIO / local) | Open source | **$0** | Setup |
| Model runtime (llama.cpp / ONNX Runtime) | Open source | **$0** | Model download, tuning |
| Embedding model weights | Open source (Apache-2.0 / MIT) | **$0** | — |
| LLM weights (Qwen2.5 GGUF) | Open source (Apache-2.0) | **$0** | Quantisation choice |
| Reranker | Open source | **$0** | Flag-gated off |
| API + UI + worker | Open source | **$0** | Development |
| Observability (OTel, Prometheus, Grafana, Loki, Tempo) | Open source | **$0** | Dashboard build |
| **Laptop hardware** | Existing asset | **$0 incremental** | Electricity (~$1/month) |

**Human cost estimate:** setup ≤ 30 min via one documented command (NFR-012); embedding a 2,000-document
corpus ≈ 16–80 min of background CPU ([§4.3](03-scale-model.md#43-embedding-throughput--the-real-constraint)).

**T-1 cost: $0.** This is the tier that a portfolio reviewer will actually run, so it is the tier that
must be genuinely frictionless. That is why the seeded corpus and precomputed embeddings are
deliverables rather than conveniences.

---

## 18.3 T-2 — Free demonstration deployment

| Component | Category | Cost | Stated limits (not marketing) |
|---|---|---|---|
| Compute (4 vCPU / 16–24 GB always-free ARM VM) | Free tier | **$0** | No SLA. Sleeps/reboots. Regional capacity limited. **Free-tier terms can change** |
| PostgreSQL + pgvector | Self-hosted on that VM | **$0** | 16–24 GB RAM shared with everything else; ~100 GB disk cap |
| Object store (MinIO) | Self-hosted | **$0** | Bounded by disk; no multi-region |
| Queue | Same Postgres | **$0** | ~1–5 k jobs/s ceiling (need 10/s) |
| Model runtime | Self-hosted | **$0** | **~60 s per generated answer** ([§4.7](03-scale-model.md#47-generation-feasibility-on-the-free-tier)); no cross-encoder rerank |
| TLS | Let's Encrypt (free) | **$0** | Renewal automation required |
| Monitoring | Self-hosted OSS | **$0** | Single host = single point of failure for telemetry too |
| CI | GitHub Actions (public repos) | **$0** | Quotas; runners may be unavailable |
| Domain | Free subdomain (e.g. `*.duckdns.org`, provider free subdomain) | **$0** | No SLA; provider may revoke |
| Backups | Nightly dump to the same host / free object tier | **$0** | **RPO 24 h** — the honest free-tier limit ([§14.6](13-reliability-model.md)) |

**T-2 cost: $0**, with these honest consequences, all stated in the user-facing documentation rather
than discovered during a demo:

| Consequence | Why it is unavoidable for free |
|---|---|
| ~99 % availability, not 99.9 % | Free tiers have no SLA |
| 24 h RPO for content and audit records | No free PITR in most providers |
| Generated answers take ~60 s → extractive is the default | 4 CPU cores cannot serve a local LLM at interactive latency |
| Single host = single point of failure | No second node for free |
| Ingestion ~1.3–4.5 chunks/s | CPU-bound embedding ([§4.3](03-scale-model.md)) |
| Cross-encoder reranking unavailable | 33 s/query ([§4.4](03-scale-model.md)) |

**A note on honesty in the demo.** If the free demo shows a 60-second answer with no explanation, it
looks like a broken project. Showing *"extractive answer in 200 ms; generative mode enabled on the
production tier"* with the measurement that justifies it turns a limitation into evidence of engineering
judgement. **This is the single most important presentation decision in the project.**

### Free-tier durability risk (RISK-017)

The recommended free VM provider's free tier has been subject to capacity and policy changes. The
contingency is architectural rather than financial: the whole stack is containerised, so a provider
change means a `docker compose` target change, not a redesign. The provider is selected by *container
portability*, not by feature.

---

## 18.4 T-3 — Production-scale architecture (a migration exercise, not a plan)

**Purpose:** answer "what would this cost at the modelled stress scale?" with arithmetic, so the
free-to-paid transition is a decision rather than a surprise. All prices are `ASM` and must be verified.

### Assumptions (from [§4](03-scale-model.md))

| Input | Value |
|---|---|
| Sustained QPS (all endpoints) | 133 |
| Peak QPS | 667 |
| Corpus | 2 M documents / 22 M chunks |
| DB size (384-d, incl. indexes) | ~90–110 GB |
| Object storage | 0.51 TB originals |
| Answers/month (peak) | 86.4 M |
| Context + output tokens per generated answer | 2,500 |
| Daily re-embedding (2 % churn) | 440 k chunks/day |

### Cost estimate at stress scale

| Layer | Sizing basis | Monthly cost `ASM` | Notes |
|---|---|---|---|
| **API compute** | 133 QPS sustained, 667 peak; ~200 ms/req; 4 vCPU per ~40 QPS | 6 × 4 vCPU | ≈ $400–700 |
| **Database** | ~100 GB with vectors; 133 QPS mixed read/write | 2 × 8 vCPU, 64 GB, 200 GB SSD, managed HA | ≈ $700–1,500 |
| **Read replica** | Search reads | 1 × 8 vCPU, 64 GB | ≈ $300–600 |
| **Object storage** | 0.51 TB originals + 31 GB text; 5 yr growth | ~$15 (storage) + egress | Egress dominates at scale |
| **Embedding worker (GPU)** | 440 k chunks/day at ~90 chunks/s ⇒ ~1.4 h/day GPU; sized for burst | 1 × GPU instance, always-on for query embedding | ≈ $500–1,500 |
| **LLM inference** | 86.4 M answers/month × 2,500 tokens. **Self-hosted:** 2 × GPU for ~60 tok/s each | Self-hosted ≈ $2,000–4,000 · Hosted ≈ $4,000–15,000 | **The dominant line item** |
| **Queue / cache** | In Postgres; Redis if hit rate justifies | $0–100 | Deferred until measured |
| **Observability** | Metrics + logs + traces at [§15.9](14-observability-model.md) volume | $100–400 | Managed backends cheaper than self-hosted at this volume |
| **Egress (answers)** | 133 QPS × ~3 KB ≈ 34 GB/day ≈ 1 TB/month | ≈ $80–100 | |
| **Backups / PITR** | ~100 GB + 0.5 TB objects | ≈ $50–150 | |
| | | **≈ $4,000–10,000/month self-hosted** · **≈ $6,000–18,000/month with hosted LLMs** | `ASM` |

### Cost sensitivity — what actually drives the bill

| Lever | Effect | Note |
|---|---|---|
| **Extractive-first answering** | Eliminates the dominant line item for the majority of queries | The §1 architectural decision is also the primary **cost** decision. If 70 % of traffic is answered extractively, LLM cost falls ~70 % |
| Model size (7 B vs 30 B) | Linear-ish in GPU count for self-hosted | Benchmark on quality first; 30 B is not automatically worth 4× |
| Dimensionality (384 vs 1024) | 2.7× vector memory and index build time | Affects DB tier and re-embedding cost |
| Corpus churn | Linear in embedding cost | If churn is 10 %, GPU cost triples |
| Answer cache hit rate | Direct multiplier on LLM cost | ≥ 30 % target |
| Managed vs self-hosted | 2–3× price difference for lower operational burden | A real trade of money for attention |
| Multi-region | Roughly doubles infrastructure | Only justified by an availability requirement |

**The most useful conclusion in this section:** the largest cost is generation, and the largest cost
control is *answering well without generating*. That aligns the engineering, the latency and the budget —
which is the strongest kind of decision, because there is no tension to manage later.

---

## 18.5 Optional paid services — full disclosure

For each: free alternative, open-source alternative, cost at scale, lock-in, migration strategy.

| Service class | Free alternative | Open-source alternative | Cost at scale (ASM) | Lock-in | Migration strategy |
|---|---|---|---|---|---|
| **LLM API** | Provider free tier (rate-limited, ToS-bound); local model | **Qwen2.5 / Llama via llama.cpp** (default) | $0.0000015–0.000015 per 1 k tokens | **High** — prompt and output formats differ; evals differ | `LLMProvider` port + eval harness to compare providers on the same benchmark. The port already exists ([ADR-004](../../docs/04-adrs/ADR-004-provider-ports-and-adapters.md)) |
| **Embedding API** | Some providers' free tiers | **e5-small / bge-m3 locally** (default) | ~$0.00002 per 1 k tokens; 440 k chunks/day ≈ $2.6/month at scale — small | **High** — dimensions and normalisation differ; requires full re-index | `EmbeddingProvider` port; a provider change is a full re-embed, so the port is mandatory |
| **Rerank API** | None meaningful | **bge-reranker on GPU** | ~$1–3 per 1 k docs | Medium | `Reranker` port; already flag-gated off by default |
| **Vector DB** | Postgres free tier | **pgvector (default)** | Managed pgvector ~$0.10–0.25/GB-mo; a dedicated service ~$300–1,500/mo + egress | **High** | `VectorStore` port. Escalation trigger: > 10⁸ chunks ([§7.7](06-architecture-options.md#77-what-would-change-the-answer)) |
| **Managed Postgres** | Self-hosted | **PostgreSQL (default)** | ~$200–800/mo for the described size | Low-medium — standard SQL | Lowest-risk migration; standard tooling |
| **Object storage** | MinIO self-hosted | **MinIO / S3 API** | ~$0.023/GB-mo + egress | Low | `ObjectStore` port; keys are already server-generated |
| **Observability backend** | Grafana Cloud free tier | **Prometheus/Grafana/Loki/Tempo (default)** | $25–200/mo at our volume | Low (OTel) | OTel-native; config change |
| **Auth provider** | Local Keycloak/Authentik | **Self-hosted OIDC (default)** | Free self-hosted; $0–25/user/mo hosted | Low-medium | OIDC is a standard |
| **GPU for embedding burst** | Free-tier GPU notebooks (Kaggle/Colab) | Local GPU / rented spot | $0.30–1.50/hr | Low | Offload is a worker change only |

### The rule applied to every optional paid service

1. Free or open-source alternative is **implemented first**.
2. The port exists **before** any commercial evaluation, so switching is an adapter, not a refactor.
3. The evaluation harness ([§12](11-rag-quality-strategy.md)) is provider-agnostic, so a comparison
   between providers is a **run**, not a project.
4. No paid service may receive document content without explicit per-tenant opt-in and a
   data-classification check (THR-005).
5. Any paid adoption must be recorded as an ADR with the trigger that fired.

---

## 18.6 What is free, precisely

| Capability | Free mechanism |
|---|---|
| Ingestion, parsing, chunking | Self-hosted OSS |
| Lexical + dense retrieval | pgvector + FTS |
| Reranking (structural) | In-process, no model |
| Extractive answering | In-process |
| Generative answering (slow) | Local llama.cpp |
| Embedding | Local ONNX |
| Auth | Self-hosted OIDC |
| Object storage | MinIO / S3 API |
| Queue + DLQ | Postgres |
| Cache | In-process |
| Observability | OTel + Prometheus/Grafana/Loki/Tempo |
| CI/CD | GitHub Actions (public repo) |
| TLS | Let's Encrypt |
| Evaluation | Local harness |

**What is *not* free at stress scale:** GPU inference for low-latency generation, GPU embedding at
corpus scale, high availability, and PITR with a short RPO. This is stated plainly rather than implied
by silence.

---

## 18.7 Cost governance

| Practice | Rationale |
|---|---|
| Every optional paid path requires an ADR with the **trigger** that fired | Prevents drift into paid services |
| A monthly cost review of tokens, storage, GPU hours and cache hit rate | Cost regressions are invisible until they are large |
| `tokens_total` and `embeddings_total` are first-class metrics from day one | You cannot control a cost you do not measure |
| The cost-per-1000-answers metric is reported alongside the quality metrics | A quality optimisation that doubles cost must be justified on both axes |
| Free-tier limits are documented in the deployment runbook | The day the free tier changes, the runbook already says what to check |

## 18.8 The honest summary

| | T-1 | T-2 | T-3 |
|---|---|---|---|
| Monetary cost/month | **$0** | **$0** | **$4k–18k** (`ASM`) |
| Real availability | 100 % (local) | ~99 % | 99.9 % |
| Real answer latency (generative) | ~9 s | ~60 s | < 8 s |
| Corpus ceiling | ~10⁵ chunks | ~10⁵ chunks | 10⁸+ chunks per instance |
| What breaks first | — | Embedding throughput; availability | LLM inference cost |

**The free tier is not a toy tier.** It is a complete, evaluated, cited-answer system with a real
retrieval pipeline, a real security model and real observability. What it lacks is scale and
generative latency — and both limitations are measured, documented and attributed to a specific
hardware constraint rather than to an unexamined architectural choice. **That difference between "it
does not scale yet" and "we know exactly why it does not scale yet" is the engineering claim this
project makes.**