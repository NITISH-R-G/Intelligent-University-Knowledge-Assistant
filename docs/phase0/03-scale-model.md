# §4 Scale Model

**Purpose.** Make the numbers that constrain the architecture explicit and falsifiable, so that when a
number is wrong, we know which decisions it invalidates.

**Status of every number in this document:** either `DERIVED` (arithmetic shown, reproducible),
`ASSUMED` (ASM-###, with verification), or `ESTIMATE` (engineering judgement; must be measured in the
stated phase). No other category is used.

---

## 4.0 Reconciling the brief's scale target with reality

The brief asks for a design that could serve *millions of documents* and *billions of chunks*. Working
the arithmetic backwards from a realistic university corpus:

```
1,000,000,000 chunks ÷ 11 chunks/document  =  90,909,091 documents
```

**Under the modelled document profile, a billion chunks requires ~91 million documents**, which is
roughly 300× the "millions of documents" figure in the same sentence. The two numbers are not
mutually consistent unless documents are far smaller than assumed, or the corpus includes a very
different document mix (e.g. code, transcripts, web-scale text, or one document per Q&A pair).

This is recorded rather than papered over. Two conclusions follow:

1. **The architectural stress target used here is 20 institutions / 2,000,000 documents / 22,000,000
   chunks** — squarely in the "millions" regime and achievable to reason about.
2. **The billion-chunk target is treated as an order-of-magnitude extrapolation** whose consequence is
   a single architectural fact: *no single-node vector index is sufficient past ~10⁸ chunks, so the
   design must not embed an assumption that the index is unsharded and indivisible.* That constraint
   is honoured by putting a versioned, tenant- and shard-scoped index abstraction behind a port
   ([ADR-002](../../docs/04-adrs/ADR-002-postgresql-pgvector-as-the-single-data-store.md)) and by
   partitioning the chunk table from day one — a partition key is cheap at 22M rows and expensive to
   add at 1B rows.

---

## 4.1 Assumption register

| ID | Assumption | Value used | Sensitivity | Verification |
|---|---|---|---|---|
| ASM-001 | Institutions in the stress scenario | 20 | Linear in corpus size | `OQ-002` |
| ASM-002 | Registered users per institution | 50,000 | Drives DAU only | `OQ-002` |
| ASM-003 | Daily active ratio | 0.25 | Standard range 0.15–0.40 | Analytics once deployed |
| ASM-004 | Peak concurrent answering sessions as a fraction of DAU | 0.02 | Dominates peak QPS; 0.01–0.03 | Analytics; `OQ-003` |
| ASM-005 | Questions per answering session per minute | 2 | 1–4 | Analytics |
| ASM-006 | Search/suggest requests per answer request | 3 | 2–5 | Analytics |
| ASM-007 | **Mean document size, raw bytes** | 250 KiB | Strong; drives everything below | 100-doc sample (`OQ-004`) |
| ASM-008 | **Text extraction yield** (text bytes ÷ raw bytes) | 6 % | **Highest-sensitivity input.** 6 %→15 % raises chunk count 2.5× | 100-doc sample |
| ASM-009 | Document churn (fraction superseded or edited per day) | 2 % | Dominates embedding cost | Registry data (`OQ-005`) |
| ASM-010 | Chunk size / overlap | 400 tok / 50 tok | Forced by encoder context window (§11.2) | Model card + measurement |
| ASM-011 | Tokens per character | 0.25 (4 chars/token) | ±10 % | Standard for English prose |
| ASM-012 | ANN index overhead over raw vectors | ×1.25 | ±40 % | Measured at index build |
| ASM-013 | PostgreSQL TOAST compression on text | ×0.40 | ±20 % | Measured |
| ASM-014 | A1 ARM core effective FP throughput (int8, batched) | 120 GOPS | ±50 % — the single most uncertain hardware input | **Measured in Phase 10** |
| ASM-015 | Laptop (M-series, 8-core) effective int8 throughput | 600 GOPS | ±50 % | Measured in Phase 10 |
| ASM-016 | GPU (A10G) effective FP16 throughput | 40,000 GOPS | ±50 % | Measured in Phase 10 |
| ASM-017 | A1 memory bandwidth (decode-bound) | ~13 GB/s | ±25 % | Published spec |
| ASM-018 | Peak-to-mean traffic ratio | 5:1 (×1.2 for the 20 % of hours around exams ≈ 20:1 vs daily mean) | Determines autoscaling need | Analytics |
| ASM-019 | FTS inverted-index size relative to text | ×0.35 | ±30 % | Measured |
| ASM-020 | Metadata bytes per chunk | ~512 B | ±40 % | Measured |

> **Note on ASM-008.** A 6 % text yield is realistic for university circular PDFs (page furniture,
> logos, scanned inserts, embedded fonts) and is the assumption most likely to be wrong in either
> direction. DOCX yields are typically 80–95 %. If the real corpus skews to DOCX, the chunk count
> could be **10× higher** and the corpus 10× larger than modelled. This is why Phase 2 must begin by
> measuring yield on a real sample before any capacity commitment is made.

---

## 4.2 Corpus derivation

```
ASM-007  mean raw document            = 250 KiB = 256,000 B
ASM-008  text yield                   = 6 %
         extracted text per document  = 256,000 × 0.06          = 15,360 B
ASM-011  tokens                       = 15,360 × 0.25            ≈  3,840 tokens
ASM-010  chunk 400 tok, overlap 50    = stride 350 tokens
         chunks per document          = ⌈3,840 ÷ 350⌉           =  11 chunks
ASM-001  institutions                 = 20 × 100,000 docs       =  2,000,000 documents
         TOTAL CHUNKS                 = 2,000,000 × 11          =  22,000,000 chunks
```

### Storage

| Quantity | Formula | Result |
|---|---|---|
| Original files (object store) | 2M × 250 KiB | **0.51 TB** |
| Extracted text, all documents | 2M × 15 KiB | 30.7 GB |
| Chunk text + metadata (raw) | 22M × 3,712 B | 81.7 GB |
| Chunk text + metadata (post-TOAST, ×0.40) | — | **32.7 GB** |
| FTS inverted index (×0.35 of text) | — | ~11 GB `ESTIMATE` |
| Vectors @ 384-d fp16 | 22M × 768 B | **16.9 GB** |
| Vectors @ 384-d + 25 % ANN overhead | — | 21.1 GB |
| Vectors @ 768-d fp16 + overhead | 22M × 1,536 B × 1.25 | 42.2 GB |
| Vectors @ 1024-d fp16 + overhead | 22M × 2,048 B × 1.25 | 56.3 GB |
| **Postgres total (384-d, incl. catalog, queue, audit)** | — | **~90–110 GB** `ESTIMATE` |
| **Postgres total (1024-d)** | — | ~130–160 GB `ESTIMATE` |

**Decision consequence.** At 384-d, the entire stress corpus fits comfortably in a single
single-instance PostgreSQL instance with room for the transactional workload and the queue. At
1024-d it still fits, but HNSW build time and cache behaviour become the limiting factors, and this
is the first point at which the index must be sharded by tenant. This directly informs
[ADR-002](../../docs/04-adrs/ADR-002-postgresql-pgvector-as-the-single-data-store.md) and the
decomposition rules in [§9.6](08-recommended-architecture.md#910-decomposition-rules--how-b-becomes-c-without-redesign).

### Growth rate

```
ASM-009  churn 2 % of chunks per day  = 22,000,000 × 0.02   = 440,000 chunks/day
         vector growth @384-d        = 440,000 × 768 B      = 0.34 GB/day  (~10 GB/month)
         text growth                 = 440,000 × 3,712 B    = 1.6 GB/day   (~49 GB/month)
```

Steady-state growth is ~60 GB/month of database and ~10 GB/month of new object bytes at full stress
scale. **Storage is not the binding constraint; embedding compute is.** This is the central finding
of §4.3.

---

## 4.3 Embedding throughput — the real constraint

Method: transformer encoder FLOPs ≈ `2 × parameters × tokens` per forward pass (matmul-dominated;
attention is secondary for 400-token inputs). Throughput = effective FLOPS ÷ FLOPs per chunk.
Effective FLOPS figures are `ESTIMATE` from published accelerator specs at 30–50 % utilisation for
batched int8/FP16 GEMM. **All of these must be measured in Phase 10 before any capacity commitment.**

```
FLOPs per chunk = 2 × params × 400
```

| Model | Params | GFLOP/chunk | 4× A1 CPU (120 GOPS) | Laptop (600 GOPS) | 1× A10G (40,000 GOPS) |
|---|---|---|---|---|---|
| `bge-small-en-v1.5` int8 | 33 M | 26.7 | **4.5 chunks/s** | 22.5 chunks/s | — |
| `multilingual-e5-small` int8 | 118 M | 94.4 | **1.3 chunks/s** | 6.4 chunks/s | — |
| `bge-m3` FP16 | 568 M | 454 | **0.22 chunks/s** | 0.44 chunks/s | **88 chunks/s** |

### What this means

| Workload | bge-small on 4× A1 | e5-small on 4× A1 | bge-m3 on 4× A1 | bge-m3 on A10G |
|---|---|---|---|---|
| Demo corpus, 500 docs (5.5k chunks) | 20 min | 72 min | 7 h | 1 min |
| Small tenant, 2,000 docs (22k chunks) | 82 min | 4.8 h | 28 h | 4 min |
| Full stress corpus (22M chunks) | **57 days** | **200 days** | **3.2 years** | **2.9 days** |
| Daily churn at stress scale (440k chunks) | 27 h | 3.8 days | 23 days | 1.4 h |

### Consequences, stated plainly

1. **The free tier cannot embed the stress corpus. At all.** Not with a slower model, not with more
   patience — 57 days for a small model, 3.2 years for a large one. Any claim otherwise would be
   fiction. The free deployment targets a **2,000-document demo corpus** (82 minutes of background
   embedding), which is exactly the right order of magnitude for a demonstration.
2. **A pre-embedded seed corpus is a genuine engineering requirement, not a convenience.** So that a
   reviewer can run the system in minutes, the demo ships a *reproducibly generated* seed: versioned
   source documents + a build script that regenerates the index from scratch. The shortcut is
   *reproducibility of the build*, not shipping opaque binary artefacts.
3. **Embedding must be an independently scalable, independently swappable component.** This is the
   single strongest argument for the port abstraction in
   [ADR-004](../../docs/04-adrs/ADR-004-provider-ports-and-adapters.md) and for a worker topology in
   [ADR-001](../../docs/04-adrs/ADR-001-modular-monolith-with-asynchronous-workers.md). It also means
   the architecture must permit **burst compute elsewhere**: a free-tier GPU notebook (Kaggle/Colab
   free GPU) can embed a 22k-chunk tenant in ~4 minutes. `OQ-006` covers whether this is acceptable
   (it has ToS and reproducibility implications).
4. **Query-time embedding is cheap; bulk embedding is not.** A 32-token query with `bge-small` costs
   2.1 GFLOP ≈ **18 ms** on 4× A1. Therefore retrieval quality and embedding quality are separable
   decisions: the query path can use a *different, larger* model than the bulk path if the
   benchmark justifies the cost, because 18 ms is affordable and 57 days is not.
5. **Model size is a *throughput* decision before it is a *quality* decision.** Phase 7 will measure
   quality; Phase 10 will measure throughput; the chosen model must pass both.

---

## 4.4 Reranking — infeasible on CPU, so it is not a default

```
FLOPs per query = 2 × params × 400 tokens × n_pairs
```

| Reranker | Params | Pairs | GFLOP/query | 4× A1 | Laptop | A10G |
|---|---|---|---|---|---|---|
| `bge-reranker-base` | 278 M | 15 | 3,336 | **33 s** | **5.6 s** | 83 ms |
| MiniLM-L6 cross-encoder | 22.7 M | 15 | 272 | **2.3 s** | 450 ms | 6.8 ms |

**Finding.** A conventional 278 M cross-encoder is not a reranker on this hardware class — it is a
30-second stall, on the free tier *and* on a developer laptop. It is viable only with a GPU.

**Consequences, which are architectural:**

1. Reranking is **off by default**, behind a per-tenant feature flag
   ([OPS-011](02-requirements.md#34-operational-requirements-ops)), and **on only on tiers with a GPU**
   ([§9.1](08-recommended-architecture.md#91-service-tiers)).
2. It is admitted **only if it earns its place on the benchmark** (§11.4): a threshold of
   **ΔnDCG@10 ≥ 0.02** and no regression in Recall@20. If RRF fusion alone already reaches
   Recall@20 ≈ 0.95, an 83 ms GPU stage that moves nDCG by 0.003 is complexity we decline to carry.
3. On tiers without a GPU, a **zero-cost lexical+structural reranker** substitutes: exact-term
   coverage, section-path match, document-type prior, recency prior, and parent-section overlap.
   This costs microseconds and captures a meaningful part of the ordering quality.
4. **This is the clearest example in the project of "no, we do not build that yet".** A cross-encoder
   reranker is the expected component in every RAG tutorial. On free hardware it is the wrong choice,
   and saying so with arithmetic is more valuable than including it.

---

## 4.5 Query load derivation

```
ASM-001/002  registered users  = 20 × 50,000          = 1,000,000
ASM-003     DAU               = 1,000,000 × 0.25     = 250,000
ASM-004     peak concurrency  = 250,000 × 0.02       = 5,000 sessions
ASM-005     questions/min     = 2
             peak answer QPS  = 5,000 × 2 ÷ 60       = 167 QPS
ASM-006     + search/suggest  = 167 × 4              = 667 QPS peak (all endpoints)
             sustained         = 20 % of peak         = 133 QPS
```

| Metric | Value | Notes |
|---|---|---|
| Peak answer QPS (exam period) | 167 | `ESTIMATE`, ×5 uncertainty |
| Peak all-endpoint QPS | 667 | Answers + search + suggest |
| Sustained all-endpoint QPS | 133 | Outside exam windows |
| Answers/day | 2.88 M | |
| Answers/peak-month | 86.4 M | |
| Context tokens/day | ~7.2 G | 2,200-token context + 300-token output, generation path only |
| Peak-to-mean ratio | ~20:1 vs daily mean | 5:1 within an hour; exams drive the rest (ASM-018) |

**Consequences:** PERF-008 (20 QPS sustained) leaves ~6.7× headroom over modelled sustained load, which
is deliberate: a workload model built entirely from assumptions needs headroom. Autoscaling is not
required at this scale — a fixed worker count with a bounded queue and backpressure is sufficient and
far cheaper. **Kubernetes is therefore not justified by this workload** ([ADR-001](../../docs/04-adrs/ADR-001-modular-monolith-with-asynchronous-workers.md)).

---

## 4.6 Latency budget (T-2, GPU-backed tier)

Itemised against PERF-001…PERF-004. `ESTIMATE`; each line is instrumented individually in Phase 10 so
the budget becomes measured rather than asserted.

| Stage | Budget (ms) | Tier-dependent? | Degradation if unavailable |
|---|---:|---|---|
| Authn + authz | 20 | No | Fail closed |
| Query normalisation | 5 | No | n/a |
| Query rewriting *(optional, flag)* | 150 | Yes | Skip |
| Query embedding | 60 | Yes (18 ms CPU / 60 ms GPU) | **Lexical-only retrieval** |
| Hybrid retrieval (FTS + ANN, RRF fusion) | 80 | No | Lexical only |
| ACL enforcement + dedupe | 20 | No | Fail closed |
| Reranking *(optional, flag)* | 180 | Yes — **0 on T-1/T-2** | Structural reranker |
| Context assembly | 10 | No | n/a |
| LLM prefill (2,000 tok) | 900 | Yes | **Extractive answer** |
| LLM decode (250 tok @ 60 tok/s) | 4,170 | Yes | **Extractive answer** |
| Citation verification | 120 | No | n/a |
| Serialisation + network | 30 | No | n/a |
| **Total (full generation path)** | **≈ 5,740 ms** | | |
| **Total (extractive path)** | **≈ 195 ms** | | |

The extractive path is **29× faster** than the generation path. This is the single strongest
architectural argument in the document for treating retrieval-only as a first-class product mode
rather than a fallback ([FR-024](02-requirements.md#retrieval-and-answering)).

---

## 4.7 Generation feasibility on the free tier

| Tier | Hardware | Prefill | Decode | 2,200-ctx + 250-tok answer |
|---|---|---|---|---|
| T-1 developer laptop | 8-core M-series, 1.5 B Q4 | ~400 tok/s | ~60 tok/s | **~9 s** ✅ meets PERF-004 |
| T-2 free cloud VM | 4× A1 ARM, 1.5 B Q4 | ~50 tok/s | ~13 tok/s | **~59 s** ❌ 7× over PERF-004 |
| T-2 free cloud VM | 4× A1 ARM, 7 B Q4 | ~15 tok/s | ~5 tok/s | **~170 s** ❌ |
| T-3 GPU tier | 1× A10G, 7–8 B FP16 | ~4,000 tok/s | ~110 tok/s | **~2.5 s** ✅ |

**This is the honest core of the free-first constraint:** a free 4-core VM cannot meet a
production-grade answer-latency SLO with a local LLM. Rather than hiding this behind a paid API, the
architecture responds in three ways:

1. **The extractive path is the default on T-1/T-2** (≈195 ms p95, [PERF-002](02-requirements.md#35-performance-requirements-perf)).
   For regulations, timetables and FAQs — a large share of real traffic — quoting the correct clause
   *is* the correct answer. This is a quality decision as much as a performance one, and Phase 7 must
   test whether extractive answers are actually preferred on the benchmark.
2. **Generation on T-1/T-2 is opt-in, labelled "slow mode",** with the measured expectation stated in
   the UI. Silently slow is worse than honestly slow.
3. **A hosted LLM adapter exists behind the same port**, is disabled by default, and its cost model is
   published in [§18](17-cost-model.md). `OQ-005` asks the stakeholder whether a hosted free-tier key
   is acceptable for the demo; this is their decision, not an engineering one.

---

## 4.8 What breaks if the assumptions are wrong

| If this is wrong | Consequence | Response |
|---|---|---|
| ASM-008 yield is 15 %, not 6 % | 2.5× more chunks → ~55M chunks, ~200 GB DB | Still fits Postgres at 384-d; HNSW build time becomes the pain point. Re-shard by tenant; consider IVF+quantisation. |
| ASM-008 yield is 30 % (DOCX-dominant corpus) | ~110M chunks, ~400 GB DB | Crosses the sharding threshold. Decomposition plan (§9.6) is already in place; execute it. |
| ASM-004 concurrency is 0.10 of DAU (retakes + exam season) | Peak 25,000 sessions → 833 answer QPS | 5× PERF-008. Autoscaling and read replicas become justified. Architecture supports it; cost does not. |
| ASM-001 institutions = 100 | 110M chunks | Same as above. |
| Churn is 10 %, not 2 % | 2.2M chunks/day to re-embed | Infeasible on CPU (14 days/day). GPU or hosted embedding becomes mandatory. |
| The corpus is scanned images | OCR pipeline; 20–50× ingestion cost | See `OQ-004`; would change Phase 2 substantially. |
| Multilingual corpus dominates | Small multilingual model at 1.3 ch/s | Ingestion 5× slower; model choice is throughput-driven. |

**The design's exposure is concentrated in ASM-004, ASM-008 and ASM-009.** Everything else degrades
gracefully. Phase 2 therefore begins with a *measurement* task (corpus profiling) before any pipeline
code is written — this is the single most important sequencing decision in the roadmap
([§20 Phase 2](19-implementation-roadmap.md#203-phase-2--corpus-profiling-and-document-ingestion)).