# §17 Failure Analysis

**Method.** For each major component: what happens when it **fails**, becomes **slow**, returns
**corrupted data**, becomes **unavailable**, or returns **incorrect data** — and how the system
recovers.

The distinctions matter. "Slow" and "unavailable" have different fixes; "corrupted" and "incorrect"
have different detection mechanisms, and *incorrect data* is the hardest class because nothing crashes.

---

## 17.1 Failure mode summary

| Component | Fails | Slow | Corrupted | Unavailable | Incorrect data | Recovery |
|---|---|---|---|---|---|---|
| API process | Crash | Latency spike | n/a | LB replaces | n/a | Restart; stateless (NFR-005) |
| Postgres | Connection loss | Slow queries | Corruption | **Total outage** | Replication lag | Failover, PITR restore |
| pgvector index | Build fails | Slow scan | Corrupt graph | ANN leg unavailable | Bad recall | Shadow rebuild + atomic swap |
| FTS index | Build fails | Slow scan | Corrupt | Lexical leg unavailable | Bad recall | Rebuild from chunks |
| Worker | Crash | Slow stages | n/a | Jobs stall | n/a | Lease timeout + retry |
| Sandbox parser | Crash | Hang | Malformed output | Jobs fail | Wrong text | Non-retryable → quarantine |
| Embedding runtime | OOM | Slow | Bad vectors | Bulk pauses | Silent quality loss | Breaker; batch resize |
| LLM runtime | OOM/crash | Slow | Malformed output | Breaker → extractive | Confident wrongness | Breaker, half-open probe |
| Reranker | Crash | Slow | n/a | Skip (structural) | Misordering | Flag off |
| Object store | 5xx | Slow | Partial PUT | Ingestion blocks | n/a | Retry with backoff |
| OIDC provider | Down | Slow | n/a | New logins fail | Misconfigured claims | Fail closed; token lifetime |
| Queue (PG table) | Lock contention | Backlog | n/a | Same as PG | Poison job | DLQ; backoff |
| Cache | Down | — | Stale entries | Fall through | **Stale answers** | TTL + version keys |
| Authorisation | Bug | Slow | n/a | n/a | **Over-permissive** | RLS second control; audit |

---

## 17.2 API process

| Failure | Behaviour | Detection | Recovery |
|---|---|---|---|
| Crash | LB removes it; in-flight requests error | Liveness probe | Restart. **No data loss** because no authoritative state is in-process (NFR-005) |
| Memory leak → OOM | Restart loop | RSS alert (> 80 % of limit) | Restart; profile the leak. Bounded memory is what makes scaling safe (PERF-014) |
| Latency spike | Requests queue at LB | p95 burn alert | Shed generation first, then rate limit |
| Thread/connection exhaustion | Requests queue | Pool saturation alert | Shed load; check for a slow query holding connections |
| Graceful shutdown (SIGTERM) | Stop accepting, drain, checkpoint | — | NFR-006: zero lost acknowledgements |

**Key property:** because the API holds no authoritative state, killing it mid-request loses nothing
that was not committed. This is a design invariant (R6 in [§9.10](08-recommended-architecture.md#910-decomposition-rules--how-b-becomes-c-without-redesign)), not a lucky accident.

---

## 17.3 PostgreSQL — the critical dependency

| Failure | Behaviour | Detection | Recovery |
|---|---|---|---|
| **Unavailable** | **Total outage.** Every capability needs it | `db_up` metric, readiness probe | Failover (managed) or restart; RTO 10 min (T-2), 1 min (T-3) |
| **Slow** | `statement_timeout` fires per stage; requests degrade to errors | Query latency percentiles; slow-query alerts from `pg_stat_statements` | Identify the slow statement; usually an unindexed filter or a rebuild running concurrently |
| **Connection exhaustion** | Requests queue in the pool, then fail | `db_pool_wait_seconds` | Find the leak; most often a missing `finally` or a query inside a loop |
| **Deadlocks** | Transactions abort; retried with random delay | `db_deadlock_total` | Retry (3×, immediate, jittered). Normal under concurrency; a spike means a locking-order bug |
| **Corruption** | Reads fail or return wrong pages | Checksums, `pg_verifybackup`, integrity checks after restore | PITR restore; then **verify document count, vector integrity, ACL integrity** before declaring recovery |
| **Replication lag** | Reads of answers lag | `db_replica_lag_seconds` | Accept for analytics; **never** route document reads to a lagging replica |
| **Disk full** | Writes fail; **ingestion stops before queries** | Disk alert at 85 % | Ingestion pauses; queries (read-only) continue |
| **Index build monopolises IO** | Query latency degrades | Stage of slow queries | `maintenance_work_mem` tuning; build during quiet hours; **build into a shadow table**, never `CREATE INDEX` on a live large table |

**The last row is the sharpest operational edge in Option B.** HNSW build on 22 M chunks is a
multi-hour, IO-heavy operation. Doing it in place on a serving database is a self-inflicted outage. The
design therefore mandates **shadow-index build + atomic swap** (FR-014), and the build runs only on a
worker with reduced resource priority.

### Why there is no cache-only fallback here

Serving cached answers while the database is unavailable would keep a 200-status service alive — and
would serve **stale** answers about **policy**, with no way to indicate staleness. For this system that
is equivalent to the supersession bug we are spending the whole architecture preventing. **The honest
503 is the correct failure.**

---

## 17.4 Retrieval indexes

| Failure | Behaviour | Detection | Recovery |
|---|---|---|---|
| ANN unavailable (build failed, extension error) | **Lexical leg only**; `degraded: lexical_only` | ANN error rate; degraded-response metric | Rebuild into a shadow table; swap atomically |
| ANN **corrupt** (invalid vectors, wrong distance op) | Silently poor recall — **the dangerous case** | **Recall probe**: a fixed 50-query benchmark sampled per hour; alert if recall drops > 0.05 | Rebuild from chunks; embeddings are the source, so this is safe |
| ANN slow | Scan latency rises; p95 breaches | Per-leg latency histogram (fts vs ann) | Tune `ef_search`; check cache hit ratio; consider IVF+quantisation |
| FTS unavailable | ANN leg only | Error rate | Rebuild from chunk text |
| **Both unavailable** | `503 retrieval_unavailable` | — | **No answer is generated.** Hard invariant |
| Filter drops too much (over-filtering) | Relevant docs silently excluded; **looks like "not in the corpus"** | `retrieval_candidates_total` post-filter drop-off | Compare pre/post-filter counts; a sudden drop indicates a broken effective-date or ACL predicate |

**Corrupt-index detection deserves emphasis.** A corrupt vector index does not error; it returns
plausible, wrong neighbours. Nothing crashes and no alert fires on error rate. The only reliable
detection is a **canary recall probe**: a fixed query set with known expected results, run periodically,
alerting on recall regression. This is a cheap and genuinely important control, and it is the reason
"we have metrics" is not sufficient for search quality.

---

## 17.5 Ingestion worker

| Failure | Behaviour | Detection | Recovery |
|---|---|---|---|
| Crash mid-job | Job lease expires; another worker reclaims | Lease expiry counter | Retry. **Content hash makes the retry a no-op** if work already committed (OPS-004) |
| Poison job | Fails repeatedly | `job_retries_total` | After max attempts → **DLQ** with full context. Does not block the queue |
| **Stuck job** (worker alive but job never finishes) | Lease expiry reclaims it even though the worker is alive | Lease expiry without worker death | Designed for: the lease, not the worker's health, determines ownership |
| Slow stage | Backlog grows | Queue age metric | Concurrency limit; reduce batch size; check if the embedding runtime is the bottleneck |
| Queue saturated | New jobs rejected `503` + `Retry-After` | Queue depth | Backpressure by design (NFR-007). Ingestion is *degraded*, not broken |
| One tenant floods ingestion | Their backlog grows | **Per-tenant** queue view (Dashboard 6) | Per-tenant concurrency limit; query traffic unaffected — noisy-neighbour containment |
| Partial index write | Transaction rolls back; nothing visible | Transaction error | Retry from stage 7; stages 1–6 are pure functions of the input and do not repeat |
| Version conflict (two admins publish) | 409 with a diff | — | Optimistic locking on `revision` |

**The partial-write case is why the transaction boundary matters.** If chunks were written by a
background job that commits incrementally, a crash could leave a half-indexed document that is
retrievable and wrong. One transaction for stage 7 makes "partially indexed" unrepresentable.

---

## 17.6 Document parsing

| Failure | Behaviour | Detection | Recovery |
|---|---|---|---|
| Parser crash (library bug) | Job fails; retryable 3× | Job error | Retry; then DLQ. **Other documents unaffected** (FR-011) |
| **Zip bomb** | Sandbox wall-clock/memory limit fires | `parser_timeouts_total` | **Non-retryable** → quarantine. Retrying a bomb is a DoS amplifier |
| Path traversal in archive | Sandbox has no host filesystem to escape into | — | Contained by design (BND-5) |
| Sandbox escape | The severe case | — | Containment (no network, no creds). Residual risk THR-021 |
| Produces **empty or near-empty text** (scanned PDF) | Chunks are empty; nothing retrievable | **Yield check**: text bytes ÷ raw bytes below threshold | Flag for review; OCR is out of scope (`OQ-004`) |
| Produces **wrong text** (bad encoding, column mangling) | Retrieved text is garbage | Sample-based content inspection during development; per-source citation-share anomaly in production | Re-parse with a different parser version; `parser_version` recorded so affected versions are identifiable |
| Hangs on a malformed file | Sandbox wall-clock kills it | Timeout metric | Non-retryable → quarantine |
| Metadata extraction wrong (wrong date, wrong type) | Wrong temporal filtering — **answers cite the wrong version** | Admin review at publish; metadata-quality benchmark | Correct the metadata; the document is `DRAFT` until an admin approves, which is exactly why that gate exists |

**The last row is the argument for the DRAFT→publish gate.** Metadata errors are only dangerous once a
document is live. Human review before publication converts a potential correctness incident into a
detected data-quality issue.

---

## 17.7 Embedding and model runtimes

| Failure | Behaviour | Detection | Recovery |
|---|---|---|---|
| Embedding OOM | Batch fails | Error rate | Halve batch size; retry. **Partial index writes are prevented by the transaction boundary** |
| Embedding model **silently wrong output** (wrong normalisation, wrong pooling) | Vectors are valid but semantically wrong → recall collapses **with no error** | Canary recall probe; embedding-verification test (encode a known sentence, check the expected neighbours) | Full re-embed; fix the adapter. **The most dangerous class of bug in the whole system** because nothing fails |
| Query embedding fails | Lexical leg only | Error rate | Degraded flag |
| Bulk embedding fails | Ingestion pauses; backlog grows | Queue age | Breaker; retry; escalate (GPU or hosted) |
| Model dimension mismatch | Query vector cannot be compared to index vectors | **Explicit check at startup and on index activation** | Fail fast with a clear message. **Never** silently pad or truncate |
| Model revision changed without re-embed | Mixed-model index → garbage comparisons | **Startup assertion** (all chunks' `embedding_model_id` == active index model) | Block serving the index; re-embed |
| LLM OOM | Breaker opens | Error rate | Extractive path |
| LLM **slow** | Per-request timeout at the stage budget | Timeout rate; breaker on timeout rate | Extractive fallback |
| LLM **malformed output** | Contract validation fails | `llm_invalid_output` | Fall back to extractive; **never pass malformed output through** |
| LLM **confidently wrong** | No crash, no error, **wrong answer** | Citation verification catches fabricated spans; groundedness benchmark catches the rest in CI | Residual risk; see below |

### The irreducible case: a fluent, uncited, unsupported claim

This is the one failure the system cannot fully eliminate. Layers reduce it; abstention and citation
verification catch most of it; the benchmark measures the residual rate (target ≤ 2 %). But a model
that states a plausible, uncited, wrong claim with complete fluency will occasionally be produced, and
no amount of prompt engineering makes that probability zero.

**Engineering response (not a claim of a fix):**

1. Measure it continuously (unsupported-claim rate) and alert on a shift.
2. Make the UI structurally resistant: claims appear with citations; a user checking a claim sees its
   source. **Uncited claims are visually distinguishable** — this is a product mitigation, and arguably
   the strongest one.
3. Prefer the extractive path on constrained hardware, where the failure surface is much smaller.
4. State the residual risk honestly in user-facing documentation rather than claiming "grounded".

**A claim that the system is "hallucination-free" would be a false claim, and it would be the most
damaging statement in the project.**

---

## 17.8 External dependencies

| Failure | Behaviour | Detection | Recovery |
|---|---|---|---|
| OIDC provider down | **Fail closed** for new logins; existing tokens valid | Login error rate | Wait; token lifetime bounds exposure. Optional: local admin bypass for break-glass, audited |
| OIDC **misconfigured** (wrong audience/issuer) | Verification fails closed | Auth failure rate | Configuration fix. Never "relax verification to fix it" |
| OIDC **sends unexpected claims** | Roles not derived correctly → **over-permissive** | Authz deny-rate anomaly; a test principal with no roles | Fail closed on unknown role claims; map unknown roles to no permissions |
| Hosted model API down/rate-limited | Breaker → local or extractive | Error rate | Degrade |
| Hosted model API **logs our prompts** | Data egress we did not intend | Contract review; opt-in only; classification check | Disable provider; document; cannot un-leak — which is why it is opt-in and default-off |
| Package supply chain compromised | Arbitrary code execution at build/run | `pip-audit`, lockfile review, SBOM diff | Pin prior version; rebuild; rotate secrets if build-time |

**"Fail closed on unknown role claims" is a deliberate, conservative choice.** An unmapped claim from
an IdP means our role model has drifted from the IdP's. Granting unknown claims access would be
failing open on the one component where that is catastrophic.

---

## 17.9 Cross-cutting failure: partial failure of a *publish*

The most consequential multi-component failure, and the reason for the state machine:

| Step | Failure | State after | User impact |
|---|---|---|---|
| Upload | — | `UPLOADED` | None |
| Validate | Rejected | `QUARANTINED` | None (admin notified) |
| Parse/chunk/embed | Failed | `PARSE_FAILED` | None (old version still live) |
| Index write | Rolled back | Unchanged | None |
| Publish (pointer flip) | Fails | `READY` (not published) | **None** — the old version remains authoritative |
| Publish succeeds, then indexing of a *new* version fails later | Old superseded, new incomplete | `PUBLISH_FAILED` | **Partial outage** — mitigated by R4: the publish gate verifies chunk count and embedding completeness before flipping |

**The invariant:** a document is never in a state where a new version is live but incomplete. Publish
is a **verified** transaction, not just a write. This is what makes Journey E safe.

## 17.10 Cross-cutting: silent quality regression

The failure mode with the least tooling support and the greatest user impact.

| Trigger | Why silent | Detection | Prevention |
|---|---|---|---|
| Embedding model swapped incorrectly | No error; recall changes | Canary recall probe | Startup assertion on model/version match |
| Chunking version changed without full reindex | Mixed chunking in one index | Reconciliation job (chunker version distribution) | `chunker_version` on every chunk; index build refuses mixed versions |
| Retrieval bug merged | No error; ranking changes | **Nightly full eval** + per-slice comparison | CI retrieval gate |
| ACL projection drift | Queries silently miss documents | **DI-13 reconciliation alert** | Reconciliation job |
| Prompt change that weakens grounding | No error; more unsupported claims | Citation-strip-rate alert + nightly eval | Version-controlled prompts; eval gate |
| Index built without the temporal filter | Old versions rank highly | S8 temporal-correctness slice | Temporal filter is structural, not optional |

**Every row in this table is a failure mode we have, in Phase 0, committed to a specific detection
mechanism.** That is the difference between hoping and engineering. The list is the specification for a
large part of Phase 9's instrumentation work.