# ADR-005: Hybrid Retrieval with RRF Fusion, Structural Ranking, and Gated Reranking

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Requirements** | FR-020, FR-021, FR-023, FR-024, FR-028, PERF-001, PERF-002, PERF-010 |
| **Related** | ADR-002, ADR-004, ADR-007 |

## 1. Decision

The default retrieval path is:

```
question → normalise
         → parallel lexical (PostgreSQL FTS) + dense (pgvector ANN)
         → RRF fusion (k = 60)
         → hard filters: tenant ∧ ACL ∧ PUBLISHED ∧ effective window ∧ not superseded
         → structural/lexical rerank (always on, explainable)
         → deduplicate + boilerplate removal
         → context assembly within a token budget
```

A **cross-encoder reranker is off by default** and admitted only on tiers with a GPU, behind a
per-tenant feature flag, and only if it improves **ΔnDCG@10 ≥ 0.02** without regressing Recall@20 or
adding more than 250 ms to p95.

## 2. Context

Institutional queries fail in two opposite ways:

- **Lexical failure**: "CSC402", "Regulation 7.3", "10%", "2024-25" — exact identifiers that dense
  embeddings blur. A user searching for a regulation number needs the exact match.
- **Semantic failure**: "can I sit a module exam in January?" against a document titled *Guidelines for
  Deferred Examinations*. Exact token overlap near zero.

Neither signal alone covers both. This is the classic hybrid-retrieval argument, and it is genuinely
strong here — policy documents are simultaneously highly lexical (numbers, codes, dates) and highly
paraphrasable.

The second consideration is **hardware**. The standard fix for ANN recall loss is a cross-encoder
reranker. On the hardware this project targets it is not viable.

## 3. The cost calculation that shaped this ADR

```
FLOPs per rerank query = 2 × params × 400 tokens × n_pairs
```

| Reranker | GFLOP/query | 4× A1 CPU | Laptop | 1× A10G |
|---|---:|---:|---:|---:|
| `bge-reranker-base` (278 M) × 15 pairs | 3,336 | **33 s** | **5.6 s** | 83 ms |
| MiniLM-L6 cross-encoder (22.7 M) × 15 pairs | 272 | 2.3 s | 450 ms | 6.8 ms |

**A 278 M cross-encoder costs 33 seconds per query on the free tier and 5.6 seconds even on a
developer laptop.** It is not a reranker on this hardware class; it is a stall. Including it by default
would have meant the system is unusable on the tier it is designed for.

Meanwhile, query-time embedding costs **18 ms** and hybrid retrieval **~80 ms** — so retrieval
optimisation is cheap and *should* be invested in, while reranking is expensive and must be earned.

## 4. Alternatives considered

| Alternative | Why rejected as the default |
|---|---|
| **Vector search only** | Fails exact-identifier queries (regulation numbers, course codes, percentages). A university knowledge system that cannot reliably find "Regulation 7.3" is broken |
| **Lexical search only** | Fails paraphrase entirely. Also: it is the *degraded* path, so making it the default would leave us with no improvement path |
| **Weighted score fusion** (α·BM25 + (1−α)·cosine, normalised) | Requires **score calibration between two incomparable systems**. BM25 scores are unbounded and corpus-dependent; cosine is bounded. RRF uses rank only, so it needs no calibration and no per-corpus tuning |
| **Cross-encoder rerank by default** | 33 s/query on the target hardware (§3). Would breach PERF-003 by an order of magnitude |
| **ColBERT / late interaction** | Requires a different index and breaks ADR-002's one-engine decision. Rejected on complexity, and revisit only at ≥ 10⁸ chunks |
| **Semantic chunking** | Doubles the dominant ingestion cost. Rejected pending measurement (T8) |
| **Elasticsearch BM25 + plugin vector search** | Second store; loses atomic publication; heavy memory footprint on a free tier. `pg_search` BM25 inside Postgres remains a candidate behind `LexicalSearchPort` |

## 5. Why this option

1. **Hybrid is provably complementary here**: exact identifiers *and* paraphrase are both core to the
   query distribution. Neither leg dominates, so fusion should beat both (admission test: nDCG@10
   ≥ max(leg)).
2. **RRF is calibration-free and robust.** Rank-based fusion cannot be destabilised by one leg's score
   distribution changing with corpus size — which on a continuously updated corpus is a real operational
   hazard.
3. **One engine means the ACL predicate is identical for both legs.** This is what makes ADR-007
   implementable, and it is the strongest practical argument for ADR-002.
4. **Structural reranking is free and explainable.** Exact-term coverage, section-path match, doc-type
   prior, recency prior and heading overlap cost microseconds and — critically — record *which signal
   fired*, so ranking is debuggable. That is worth more operationally than a few points of nDCG.
5. **Gating the expensive stage on measurement** means we add complexity only when it demonstrably pays.

## 6. Advantages

- Covers both failure modes; fusion measured to beat both legs.
- Retrieval is fast: ~195 ms for the full extractive path, 29× faster than generation.
- Reranking decisions are explainable and recorded in the retrieval trace.
- The expensive stage is additive and flag-gated; enabling it needs no redesign.
- The degraded path (lexical only) is a *strict subset* of the same pipeline — no separate code path to
  maintain, and no risk of the degraded path behaving differently from the healthy one.

## 7. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation |
|---|---|
| **Without a reranker, ordering quality depends on RRF + structural signals** | Accepted; quantified in Phase 7 as OQ-015 and compared against a reranked variant before any conclusion is claimed |
| Two legs = two latency budgets and two failure modes | Measured separately; leg imbalance is an explicit alert |
| RRF discards score magnitude | Acceptable; if a measured case needs magnitude, add a third signal to the structural ranker instead of complicating fusion |
| Structural rerank weights are hand-tuned | They are **benchmark-tuned** in Phase 7 against nDCG@10, and the tuning is recorded in the engineering journal |
| FTS is not true BM25 | `pg_search` evaluation behind `LexicalSearchPort`; adopt on measured Recall@20 evidence only |

## 8. Tradeoffs

We traded **the last few points of ranking quality** for **a pipeline that runs at interactive latency
on the hardware this project actually targets**, and for **explainability of every ranking decision**.
For a system where a wrong document in the context window produces a wrong answer to a student, an
auditable ordering is worth more than an opaque one that scores 0.01 higher.

## 9. Operational consequences

- **Per-leg latency and candidate counts** are metrics, not debug output; leg imbalance alerts.
- **Ranking traces** are stored with the answer so "why did this win?" is answerable after the fact.
- **Feature flags** per tenant for reranking and rerank depth (OPS-011).
- **Circuit breaker** on the rerank path, because it is the most expensive stage.
- **Two benchmarks must be maintained**: with and without reranking, so enabling it is always an
  evidence-based decision.

## 10. Scaling consequences

| Change | Effect on this design |
|---|---|
| > 10⁸ chunks | Both legs scale with the `VectorStore` partition; `LexicalSearchPort` unchanged |
| > 200 QPS | API replicas scale horizontally; retrieval is read-only and stateless |
| ANN recall degrades | **The correct response is to add the reranker** — which is exactly why the gate is ΔnDCG-based rather than absolute. At scale, recall loss is real and the economics change |
| GPU becomes available | Reranker enabled behind a flag; no code change |

## 11. Security consequences

- **The ACL filter runs inside both legs' queries**, so an unauthorised chunk is never a candidate.
  Filtering after fusion would be a data-leak vulnerability ([ADR-007](../../docs/04-adrs/ADR-007-authorisation-enforced-at-the-retrieval-layer.md)).
- **The temporal filter is a hard predicate, not a ranking signal.** A superseded version is excluded
  by construction rather than demoted — demotion can be defeated by a strong lexical match, which is
  precisely the failure mode the project exists to prevent.
- **Retrieved content is untrusted** regardless of how highly it ranks; ranking is not a trust signal.
- **Retrieval diagnostics** (candidates, scores) contain no content, so an operator can debug ranking
  without read access to documents.

## 12. Cost consequences

| Stage | Marginal cost |
|---|---|
| Query embedding | ~18 ms CPU |
| Both legs + RRF | ~80 ms |
| Structural rerank | Microseconds |
| **Cross-encoder rerank** | **+180 ms (GPU) or +5.6 s (laptop) or +33 s (free CPU)** |

So retrieval is cheap and generation is expensive. The pipeline is therefore **retrieval-first by
design and by economics**: answering well without generating is simultaneously the fastest, the
cheapest and — for policy questions — frequently the most accurate option.

## 13. Migration / replacement strategy

- Lexical leg → `pg_search` or OpenSearch: implement `LexicalSearchPort`; no fusion change.
- Dense leg → dedicated vector DB: implement `VectorStore`; RRF and structural ranking unchanged.
- Reranker model → any: implement `Reranker`; the flag stays.
- Fusion algorithm → weighted-score fusion: implement behind the same retrieval port; re-run the
  benchmark per slice before adopting.

## 14. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| RRF underperforms weighted fusion | nDCG@10 gap ≥ 0.02 | Implement weighted fusion behind the port; keep RRF as the default until measured otherwise |
| Structural rerank adds nothing | nDCG@10 unchanged with and without | Reduce to a single relevance prior, or drop it — a reranker that does not rerank should be removed |
| Lexical-only degradation is too large to be presentable | OQ-015 measured gap is severe | State the degradation plainly in the UI instead of presenting weak results as normal |
| The rerank gate is never met | No ΔnDCG ≥ 0.02 at any tier | **Delete the cross-encoder from the design.** A component that never earns its admission should not exist |