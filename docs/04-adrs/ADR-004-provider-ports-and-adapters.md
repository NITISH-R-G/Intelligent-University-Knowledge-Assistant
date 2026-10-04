# ADR-004: Provider Ports and Adapters (Dependency Inversion at the Core)

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Requirements** | NFR-002, NFR-013, SEC-004, §10 (technology evaluation) |
| **Related** | ADR-001, ADR-002, ADR-005 |

## 1. Decision

Define **ports in the domain layer** for every replaceable capability, and implement adapters in the
infrastructure layer selected by configuration:

| Port | Default adapter | Alternatives behind the port |
|---|---|---|
| `LLMProvider` | local llama.cpp | hosted APIs (opt-in, default off) |
| `EmbeddingProvider` | local ONNX (e5-small / bge) | hosted embedding APIs |
| `Reranker` | structural (in-process) | cross-encoder (GPU), hosted |
| `VectorStore` | pgvector | specialised vector DB |
| `LexicalSearch` | PostgreSQL FTS | `pg_search` BM25, OpenSearch |
| `DocumentParser` | poppler / python-docx / readability | alternatives per format |
| `ObjectStore` | S3-compatible (MinIO local) | any S3-compatible target |
| `AuthProvider` | OIDC client | any OIDC issuer |
| `JobQueue` | PostgreSQL job table | NATS / RabbitMQ |
| `CachePort` | in-process TTL | Redis |
| `Clock` / `IdGenerator` | system | deterministic fakes for tests |

The domain layer **imports nothing from infrastructure**. This is enforced by a CI import-boundary lint
rule, not by convention.

## 2. Context

Every capability in an LLM system is provided by a fast-moving external ecosystem. Lock-in risk is
highest exactly where the project is most exposed:

- The corpus is the data; **the embedding model determines whether retrieval works**. Changing it
  requires a full re-embed — an operation we want to perform for quality reasons without a rewrite.
- Local models will lose to hosted models on quality at some point, and the project must be able to
  switch without a redesign.
- The free-tier provider will change (RISK-017). Containerisation handles the runtime; ports handle
  the API surface.
- Parsers, storage engines and search indexes will each have a migration event.

Conversely, over-abstracting is a real cost: ports must earn their complexity.

## 3. Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Call vendor SDKs directly from application code** | Simplest, fastest to write. Maximum lock-in; a model change becomes a rewrite; makes Phase 7's cross-provider comparison impossible |
| **Use a framework (LangChain / LlamaIndex) that abstracts providers** | Would give provider portability for free. Rejected because it also abstracts away **the stage-by-stage behaviour we must measure** — timings, scores, fusion behaviour. Our core deliverable is measured retrieval quality; the framework hides exactly that. It also makes the domain depend on a fast-moving dependency. See §10.17 |
| **Abstract only the LLM** | Insufficient: the embedding model is the higher-risk lock-in (a full re-embed) and the vector store is the higher-cost one |
| **Ports for everything, including internal utilities** | Over-abstraction. A port earns its place only where a second implementation is plausible or a test double is genuinely needed |
| **Configuration-driven plugin system** | Adds a runtime dependency-resolution problem for no benefit at this scale |

## 4. Why this option

1. **It makes the expensive reversals cheap.** An embedding-model change is a re-embed job plus an
   adapter — not a redesign. That is the specific reversal we are most likely to need (§4.3 of the
   review shows CPU throughput alone may force a model change).
2. **It makes evaluation provider-agnostic**, so comparing two embedding models or two LLMs is a
   **run** rather than a project. That is what makes the §11 admission tests real.
3. **It keeps the domain testable without infrastructure.** `Clock` and `IdGenerator` exist precisely so
   temporal logic (effective windows, supersession, retention) is deterministically testable — temporal
   correctness is the project's core promise, and time-dependent bugs are the classic failure.
4. **It enforces the security boundary structurally.** `LLMProvider` has no method that accepts a tool,
   a callback, or a URL. The "no agency" control becomes a **type-system property**, not a policy
   document. This is a genuine and underappreciated benefit.
5. **It costs roughly one interface and one file per capability.**

## 5. Advantages

- Swapping the embedding model, LLM, parser, store or queue is an implementation change.
- Domain tests need no model, no database and no network.
- Temporal logic is deterministic and exhaustively testable.
- The security posture is partly encoded in types (no agency, no tool calls).
- CI can enforce the boundary, so it cannot silently decay.
- Cost/quality trade-offs become measurable experiments.

## 6. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation |
|---|---|
| **Leaky abstractions** — a port that must expose a vendor quirk to be usable | Rule: a port expresses the *capability we need*, not the vendor's full surface. If a port grows a vendor-specific method, that is a design smell to fix in review |
| Extra indirection and boilerplate | One interface + one adapter per capability; adapters are thin |
| Risk of over-abstraction | Only 11 ports, each justified above. `Clock`/`IdGenerator` exist for real reasons (temporal correctness) |
| Performance cost of abstraction | Negligible: these are millisecond-to-minute operations, and adapters can be optimised internally |
| Risk of "interface for everything" paralysis | Explicitly **not** abstracting internal helpers, pure functions, or domain logic |

## 7. Tradeoffs

We traded a small amount of structural overhead for **the ability to change the highest-risk
dependencies without rewriting the system** — and, less obviously, for **security properties expressed in
types**. That second benefit was not the original motivation and turned out to be the more valuable one.

## 8. Operational consequences

- Adapters are selected by **configuration**, so the same image runs in every environment.
- Every adapter has a **contract test suite** run against it in CI, so a new adapter cannot diverge
  from the port's semantics (e.g. normalising embeddings, paginating consistently, enforcing timeouts).
- Capability health is observable per adapter, so a degraded provider is visible as an adapter state.

## 9. Scaling consequences

- **Embedding worker**: the adapter boundary is exactly what makes step 1 of the extraction order
  ([§9.10](../phase0/08-recommended-architecture.md#910-decomposition-rules--how-b-becomes-c-without-redesign))
  mechanical.
- **Model serving**: a local adapter and a hosted adapter can coexist, letting a burst be absorbed by a
  GPU worker while steady state runs locally.
- **Queue/cache**: replacing them behind their ports does not touch business logic.

## 10. Security consequences

| Consequence | Detail |
|---|---|
| **No agency is structural** | `LLMProvider.complete(messages, options)` accepts no tool definitions, no callbacks, no URLs. There is no method through which document text could cause a side effect |
| **Network egress is contained** | Only adapters make network calls; the domain cannot. A hosted adapter is an explicit, opt-in, audited code path |
| **Parser isolation is uniform** | Every parser implementation must satisfy the same sandbox contract test |
| **Credentials live only in adapters** | The domain has no access to secrets, so a domain bug cannot leak one |
| **Supply-chain surface is explicit** | Each adapter is where third-party code enters; each is separately reviewed and scanned |

## 11. Cost consequences

Enables cost control as a configuration change: a free-tier instance can use local adapters, and a
production deployment can enable a hosted adapter for quality — **with no code change and no re-testing
of the retrieval pipeline**, because the benchmark is provider-agnostic. This is how the project escapes
a provider's pricing without a rewrite ([§18.5](../phase0/17-cost-model.md)).

## 12. Migration / replacement strategy

The port *is* the strategy. To migrate:

1. Implement the port in a new adapter.
2. Run the port's contract test suite against it.
3. Run the evaluation benchmark with the new adapter selected.
4. Compare per-slice metrics; adopt only on evidence.
5. Flip configuration.

**For an embedding model specifically**, the extra step is a full re-embed behind a new `IndexVersion`,
built as a shadow index and swapped atomically — which is exactly why chunks record their
`embedding_model_id` and why the index refuses to serve a mixed-generation index.

## 13. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| A port is too coarse, forcing vendor leakage | A vendor-specific method appears on a port | Widen that port deliberately, or add a second narrow port. Do not silently pass vendor objects through the domain |
| Ports become ceremony | A port with one implementation and no second candidate | Delete it. This is the discipline that keeps the abstraction honest — reviewed at each ADR |
| Over-abstraction slows delivery | Measurable velocity impact | Re-review the port list; remove what has not earned its place |