# §3 Requirements

**Conventions.** Priority is `MUST` / `SHOULD` / `POSTPONED`. Acceptance criteria are written so that
a test can fail. Verification is one of: `test` (automated test, named), `bench` (evaluation
benchmark), `load` (load test), `audit` (manual/runner check), `inspect` (log/metric/trace
inspection). Every requirement maps to at least one test in Phase 1–14; the mapping is maintained in
the traceability matrix ([§20](19-implementation-roadmap.md#206-requirement-traceability-summary)).

Requirements are numbered by domain and **gaps in numbering are intentional**: numbers are stable
identifiers, so a deleted requirement leaves a hole rather than silently renumbering history.

**Count and priority breakdown (verified against this document):** 99 requirements — 40 `FR`, 14 `NFR`,
16 `SEC`, 15 `OPS`, 14 `PERF`. Of the 85 that carry a priority: **72 `MUST`, 12 `SHOULD`, 1 `POSTPONED`**
(FR-034, labelled as such). The 14 `PERF` requirements are stated as **per-tier targets** rather than
priorities, because a latency target without a tier is meaningless in a free-first system.

---

## 3.1 Functional requirements (FR)

### Ingestion and document lifecycle

| ID | Requirement | Pri | Rationale | Acceptance criteria | Verify |
|---|---|---|---|---|---|
| FR-001 | Ingest PDF, DOCX, TXT, HTML, Markdown and CSV via file upload | MUST | Core corpus formats | Each format parses to text with ≥ 90 % of human-visible body text recovered on the golden corpus | test |
| FR-002 | Ingest from a URL, restricted to an allowlist of hosts, deferred to Phase 4 | SHOULD | Web circulars exist; SSRF risk is high | No request to a non-allowlisted or private address; redirect chain validated per hop; test suite proves the block | test |
| FR-003 | Extract document structure: headings, section paths, list items, tables, page numbers | MUST | Enables citations that name a *clause*, and metadata filtering by section | ≥ 95 % of golden documents produce a non-empty section path; table cells retrievable as text | bench |
| FR-004 | Extract metadata: title, author, department, publication date, effective date, supersedes, document type, language | MUST | Authority and temporal filtering are impossible without it | Metadata extraction F1 ≥ 0.85 for title/date/type on the golden corpus | bench |
| FR-005 | Assign a stable document identity independent of filename and upload session | MUST | Files get renamed; identity must survive | Uploading the same content under two names yields one document with two sources | test |
| FR-006 | Create an immutable document version on every content change; never mutate a published version | MUST | Auditable answers require immutability | After 3 updates, all 3 versions are retrievable as-of their effective windows; no version row is ever updated in place | test |
| FR-007 | Compute a content hash and skip re-ingestion when unchanged | MUST | Idempotency; avoids duplicate knowledge and re-embedding cost | Uploading identical bytes twice creates zero new versions and zero new embeddings | test |
| FR-008 | Re-embed only chunks whose content hash changed on re-ingestion | MUST | Cost and freshness | A 5 %-changed document re-embeds ≤ 8 % of its chunks | test |
| FR-009 | Support supersession: link a new version as superseding an old one; exclude superseded versions from default retrieval | MUST | The single most important correctness property for policy documents | Querying a superseded regulation returns the in-force version; querying with an as-of date in the old window returns the old version | test |
| FR-010 | Support withdrawal/deletion with a bounded purge window | MUST | Right to erasure / corrections | Deleted content returns zero results within 15 min, including vector rows; a deletion audit record is retained | test |
| FR-011 | Quarantine a document that fails validation or parsing, with a machine-readable reason, without stopping the pipeline | MUST | One bad PDF must not block a batch of 500 | Batch of 500 containing 5 malformed files completes with 495 published and 5 quarantined | test |
| FR-012 | Support rollback to a previous version as a single atomic operation | MUST | Ops requirement; incorrect publication is a real risk | Rollback is a pointer flip; completes < 5 s; is itself audited | test |
| FR-013 | Track and expose per-version provenance: parser version, chunking version, embedding model, index version | MUST | Reproducibility and safe reindexing | Every chunk row exposes all four; a mismatch between chunk provenance and index model is detectable | inspect |
| FR-014 | Scheduled and event-driven reindex without downtime | SHOULD | Model upgrade path | New index built in parallel and swapped atomically; zero failed requests during swap | test |

### Retrieval and answering

| ID | Requirement | Pri | Rationale | Acceptance criteria | Verify |
|---|---|---|---|---|---|
| FR-020 | Answer questions with hybrid retrieval (lexical + dense) fused by RRF | MUST | Neither signal alone is sufficient; §11 justifies it | Hybrid ≥ either single signal on nDCG@10 and Recall@20 on the benchmark | bench |
| FR-021 | Apply authorisation as a filter inside the index scan, never after generation | MUST | Post-filtering is a data-leak vulnerability | Zero unauthorised chunks in the retrieved candidate set for 100 % of ACL test cases, verified inside the query layer | test |
| FR-022 | Attach a citation to every factual claim, resolvable to document + version + section + page | MUST | Trust requires checkability | 100 % of claims in a graded answer sample have a resolvable citation; broken-citation rate = 0 | bench |
| FR-023 | **Abstain** when the corpus does not support an answer | MUST | Hallucination control; the core correctness property | On the "answer absent" slice, ≤ 5 % unsupported-claim rate and ≥ 90 % correct abstention | bench |
| FR-024 | Return a search-only mode (ranked passages, no generation) | MUST | Degradation path; also the fastest path for factual lookups | p95 < 500 ms; usable when the LLM is disabled | test |
| FR-025 | Ground generated answers in retrieved context only, with citations required by the output contract | MUST | Reduces unsupported claims | Unsupported-claim rate on the in-corpus slice ≤ 2 % | bench |
| FR-026 | Verify citations after generation: each cited span must actually occur in the cited chunk | MUST | Cheap, deterministic hallucination check — no judge model needed | Citation-correctness ≥ 0.97; violations are stripped and reported | test |
| FR-027 | Support an as-of date / effective-window query semantics | MUST | Policy correctness | "What was the rule in June 2025?" returns the June-2025 version | test |
| FR-028 | Deduplicate near-identical chunks across documents before context assembly | MUST | Boilerplate headers waste context and mislead | Context token waste from boilerplate < 10 % | bench |
| FR-029 | Expose retrieval diagnostics to authorised operators: candidate IDs, scores, filters applied | MUST | Debuggability; UC-10 | Diagnostics are retrievable by answer ID; contain no unauthorised content | test |
| FR-030 | Enforce a context token budget and report truncation | MUST | Cost and latency control; silent truncation hides evidence | No answer is generated from a context the user cannot see; truncation is reported in the response metadata | test |
| FR-031 | Support incremental refinement ("no, I meant the January exam") retaining the session context | SHOULD | Conversational correction is the common case | Follow-up reuses prior resolved entities; no full re-ingestion of session history | test |
| FR-032 | Optional query rewriting/decomposition, **feature-flagged, off by default** | SHOULD | Only if benchmark shows gain | Enabling it improves nDCG@10 by ≥ 0.02 on the benchmark; otherwise it stays off | bench |
| FR-033 | Answer caching keyed by tenant, normalised query, corpus index version **and principal set hash** | SHOULD | Latency and cost | Cache hit rate ≥ 30 % in benchmark replay; a principal-set change can never serve another principal's cached answer | test |
| FR-034 | Semantic result clustering to avoid 10 near-identical passages | POSTPONED | Marginal once dedup runs; re-evaluate if nDCG plateaus | — | — |

### Administration and API

| ID | Requirement | Pri | Rationale | Acceptance criteria | Verify |
|---|---|---|---|---|---|
| FR-040 | Versioned REST API with an OpenAPI 3.1 contract | MUST | Contract-first; clients must not scrape internals | Contract published; all endpoints documented; breaking changes rejected in CI by schema diff | test |
| FR-041 | Idempotency-Key on all mutating endpoints | MUST | Retries must not duplicate documents | Repeating a request with the same key yields the same resource and no duplicate | test |
| FR-042 | Cursor-based pagination on all list endpoints | MUST | Offset pagination breaks under concurrent writes | Paging through 10k items under concurrent insertion returns no duplicates and no gaps | test |
| FR-043 | Structured error taxonomy with stable machine-readable codes | MUST | Clients must branch on errors, not parse prose | Every error maps to a documented code; unknown errors return `internal_error` with a request ID | test |
| FR-044 | Tenant-scoped API: every request carries a resolved tenant; cross-tenant access is impossible by construction | MUST | Isolation failure is the highest-severity risk | 100 % of endpoints are tenant-scoped or explicitly `platform`-scoped; a cross-tenant test suite passes | test |
| FR-045 | Rate limiting per tenant and per principal, configurable | MUST | Abuse, cost control, noisy-neighbour | Limits enforced at the edge and the application; 429 carries `Retry-After` | test |
| FR-046 | Bulk ingestion via signed upload URLs so bytes do not transit the API | MUST | Large files, memory, timeout limits | API never receives file bytes; a 25 MB file uploads and publishes successfully | test |
| FR-047 | Admin UI for upload, preview, publish, supersede, rollback, delete | MUST | Administrators are users, not scripts | An administrator completes each lifecycle operation without writing code | audit |
| FR-048 | Streaming responses for generated answers (SSE) | MUST | Perceived latency | First token < 2 s on T-2; client renders progressively | test |
| FR-049 | Export a tenant's data (documents metadata, queries, answers) as JSON | SHOULD | Portability and compliance | Export completes for a 5,000-document tenant < 60 s | test |
| FR-050 | Per-tenant configuration (models, chunking, retrieval depth, features) | SHOULD | Noisy-neighbour control; institutional variation | Two tenants with different configs coexist; no cross-tenant bleed | test |

## 3.2 Non-functional requirements (NFR)

| ID | Requirement | Pri | Rationale | Acceptance criteria | Verify |
|---|---|---|---|---|---|
| NFR-001 | Strong static typing at every boundary, with the type checker as a CI gate | MUST | Contracts are the primary defence against integration bugs | `typecheck` fails the build on any error; no `any` in domain code (lint rule) | test |
| NFR-002 | Dependency inversion: the domain defines ports; providers are adapters selected by configuration | MUST | Provider lock-in is the top maintainability risk | Swapping the embedding provider requires no change to retrieval or domain code; proven by a test that runs the suite against two adapters | test |
| NFR-003 | Deterministic components: given the same inputs and pinned versions, chunking, embedding and retrieval are reproducible | MUST | Replay and regression testing are impossible otherwise | Same document + same versions → byte-identical chunk IDs; recorded in CI | test |
| NFR-004 | Idempotent ingestion (see FR-007) end-to-end, including retries at every stage | MUST | At-least-once queues demand idempotent consumers | Replaying an entire ingestion batch produces an identical index state | test |
| NFR-005 | No application state in process memory that is not reconstructable | MUST | Horizontal scaling and restart safety | Killing any instance mid-request loses no committed data | test |
| NFR-006 | Graceful shutdown: stop accepting, drain in-flight work, checkpoint jobs | MUST | Deploys must not drop work | SIGTERM during a batch loses 0 completed-upload acknowledgements | test |
| NFR-007 | Backpressure: bounded queues with explicit overload behaviour, never unbounded growth | MUST | Memory exhaustion is the classic RAG outage | Queue at capacity rejects with 503 + `Retry-After`; RSS stays flat under 3× overload | load |
| NFR-008 | End-to-end request correlation via propagated request/trace IDs | MUST | Debugging a distributed pipeline without it is archaeology | A trace for a single question is retrievable by its request ID | inspect |
| NFR-009 | Accessibility: WCAG 2.1 AA for the user interface | SHOULD | Public-sector requirement; also just correct | Automated axe scan passes with zero critical/serious violations; keyboard navigation complete | test |
| NFR-010 | PII never written to application logs by default | MUST | Log-based leakage is a top real-world breach vector | Log schema validated by a test that injects PII-shaped values into a request and asserts absence | test |
| NFR-011 | Data model supports hard deletion of personal data while retaining legally required audit records, with the distinction encoded in the schema | MUST | Erasure vs audit are in tension; it must be explicit | A documented retention matrix exists and is enforced by a scheduled job | test |
| NFR-012 | Onboarding: a new developer reaches a working local system in one command, documented | MUST | Portfolio and maintainability | `make up` (or equivalent) yields a working system in < 5 min on a laptop | audit |
| NFR-013 | Documentation of every ADR and requirement is part of the build gate | SHOULD | Design that lives only in chat is lost | CI fails if a code change alters a public contract without a matching doc change | test |
| NFR-014 | Deterministic seeds for the evaluation benchmark so results are comparable across runs and machines | MUST | Benchmark noise makes optimisation meaningless | Two runs on the same commit produce identical aggregate metrics within tolerance | test |

## 3.3 Security requirements (SEC)

Threat IDs in parentheses refer to [§13](12-threat-model.md).

| ID | Requirement | Pri | Rationale | Acceptance criteria | Verify |
|---|---|---|---|---|---|
| SEC-001 | OIDC/OAuth 2.1 authentication with short-lived access tokens and refresh rotation | MUST | Industry standard; avoids building auth | Token validation with audience, issuer, expiry, and algorithm allow-list; no custom password store | test |
| SEC-002 | Role-based **and** attribute-based authorisation evaluated server-side on every request | MUST | RBAC alone cannot express cohort/department scope | Authorisation decision is a pure function of (principal, resource, action) with a unit-test matrix | test |
| SEC-003 | Tenant isolation enforced at the data layer (mandatory scope on every query) | MUST | Highest-severity failure mode (THR-003) | Automated test suite attempts cross-tenant access on 100 % of endpoints; all denied | test |
| SEC-004 | All retrieved document content is treated as untrusted input and cannot influence control flow | MUST | Indirect prompt injection (THR-006) | Injection corpus in the benchmark; no instruction from a document changes system behaviour, tool use or output format | test |
| SEC-005 | Output encoding: generated content is rendered as text, never as executable markup; no `dangerouslySetInnerHTML` equivalent | MUST | Improper output handling (THR-007) | A document containing HTML/script payloads renders inert; XSS test suite passes | test |
| SEC-006 | Uploads validated by content sniffing, not by declared MIME type; size and page/count limits enforced pre-parse | MUST | Malicious upload (THR-004) | A `.pdf` that is an ELF binary or a zip bomb is rejected before the parser runs | test |
| SEC-007 | Document parsing runs sandboxed with CPU, memory, wall-clock and recursion limits | MUST | Parser RCE/DoS is the classic office/PDF RCE path | Parser worker cannot exceed limits; a zip bomb times out and is quarantined | test |
| SEC-008 | Secrets never in code, images, or logs; loaded from environment/secret store; rotated | MUST | Secret leakage (THR-012) | Secret scanning in CI; a deliberate test secret is not present in the built image or logs | test |
| SEC-009 | SSRF defence for URL ingestion: allowlist, DNS resolution checks, re-validation per redirect, egress proxy | MUST | SSRF (THR-005) | Tests for private-IP literal, DNS rebinding, redirect to metadata IP, and non-standard port all fail closed | test |
| SEC-010 | Structured audit log of all privileged actions (upload, publish, supersede, delete, role change) | MUST | Non-repudiation; compliance | Audit records are append-only, include actor, tenant, resource, before/after, request ID | test |
| SEC-011 | Per-tenant, per-principal quotas on ingestion, storage, query volume and token spend | MUST | Unbounded consumption (THR-011) | Quota enforcement tested; exceeding it produces a documented, non-destructive failure | test |
| SEC-012 | Supply-chain controls: pinned dependency versions, SBOM, vulnerability scanning, and a build that is reproducible from the lockfile | MUST | Insecure dependencies (THR-013) | SBOM generated per release; critical CVE blocks merge | test |
| SEC-013 | Content-level malware and injection heuristics run before publication, quarantining rather than deleting | MUST | Poisoned documents (THR-008) | Known-injection test document is quarantined with a reason; original retained for admin review | test |
| SEC-014 | Prompt-injection detection is defence-in-depth only, never the primary control | MUST | Detectors are evadable; over-reliance is itself a vulnerability | Architecture states the primary control is untrusted-content handling + no agency + output contract | inspect |
| SEC-015 | TLS everywhere; security headers (CSP, HSTS, `X-Content-Type-Options`, frame ancestors) | MUST | Baseline appsec | Headers asserted in automated tests | test |
| SEC-016 | Data-at-rest encryption for the object store and database volumes | SHOULD | Baseline | Encryption enabled; documented key management | audit |

## 3.4 Operational requirements (OPS)

| ID | Requirement | Pri | Rationale | Acceptance criteria | Verify |
|---|---|---|---|---|---|
| OPS-001 | `/healthz` (liveness) and `/readyz` (readiness) with meaningful dependency checks | MUST | Orchestrators and humans need them; they mean different things | Liveness does not check dependencies; readiness fails when retrieval is unusable | test |
| OPS-002 | Structured JSON logs with a documented schema, PII redaction, and sampled debug levels | MUST | Machine-parseable incident response | Log schema versioned; a test asserts redaction | test |
| OPS-003 | Metrics for every SLO plus per-stage latency histograms | MUST | You cannot SLO what you do not measure | SLI metrics exist and are exported before Phase 9 exit | inspect |
| OPS-004 | Idempotent ingestion: re-running a batch produces an identical index state | MUST | At-least-once delivery is the default; duplicates are the failure | Replay of a 1,000-document batch is byte-identical in index state | test |
| OPS-005 | Exponential backoff with jitter and a dead-letter path for exhausted retries | MUST | Poison messages must not spin forever | A permanently failing job lands in the DLQ with full context after N attempts; replays from the DLQ | test |
| OPS-006 | Timeouts on every outbound call, with budgets propagated down the call graph | MUST | Retry storms are caused by missing timeouts | A hung dependency cannot exceed its stage budget; verified by fault injection | test |
| OPS-007 | Circuit breakers on LLM, reranker and external model endpoints | MUST | A slow model must not consume the whole request budget | Breaker opens after threshold; fallback path engages; half-open probes | test |
| OPS-008 | Database migrations are forward-only, backwards-compatible with the running version, and reversible | MUST | Zero-downtime deploys | Migration applied while serving traffic, with no error spike; tested in CI | test |
| OPS-009 | Blue/green or rolling deployment with automatic rollback on SLO burn | MUST | Deploys are the main cause of outages | A deliberately bad release is auto-rolled-back within 5 min | test |
| OPS-010 | Runbook per alert, linked from the alert itself | MUST | An alert with no action is noise | 100 % of paging alerts link to a runbook with a verification step | inspect |
| OPS-011 | Feature flags for retrieval depth, reranking, generation and caching, per tenant | MUST | Lets us disable expensive paths when hardware is short | A flag flip changes behaviour without a deploy | test |
| OPS-012 | Documented, tested backup and restore procedure with a stated RPO/RTO | MUST | Backups that have never been restored are not backups | A restore drill completes within RTO and passes integrity checks | audit |
| OPS-013 | Postmortem template and blameless review practice; at least one postmortem per SLO breach | SHOULD | Learning loop | Template exists; a real postmortem is written in Phase 11 | audit |
| OPS-014 | Configuration is externalised, validated at startup, and defaults are safe | MUST | Misconfiguration is a top outage cause | Invalid configuration fails fast at startup with a clear message | test |
| OPS-015 | Load and soak tests run in CI nightly against the benchmark corpus | SHOULD | Regressions are found before users find them | Nightly report with p50/p95/p99 and error rate, trending | load |

## 3.5 Performance requirements (PERF)

Targets are per-tier; see the [service tier table](08-recommended-architecture.md#91-service-tiers).
T-3 numbers are the committed contractual targets; T-1 numbers are demo-tier and are explicitly
*not* production commitments.

| ID | Requirement | Target (T-2) | Target (T-3) | Rationale | Verify |
|---|---|---|---|---|---|
| PERF-001 | Search-only latency | p95 < 500 ms | p95 < 250 ms | The fast path; must feel instant | load |
| PERF-002 | Extractive answer latency (no LLM) | p95 < 900 ms | p95 < 400 ms | Primary free-tier answer path | load |
| PERF-003 | Generated answer latency (TTFT) | p95 < 2.0 s | p95 < 1.2 s | Perceived responsiveness | load |
| PERF-004 | Generated answer latency (complete) | p95 < 8 s | p95 < 6 s | Generation dominates; budget in §4.4 | load |
| PERF-005 | Ingestion: upload → searchable (p95, doc ≤ 5 MB) | < 120 s | < 60 s | Administrators need confirmation of publication | load |
| PERF-006 | Ingestion throughput | 2 docs/s sustained | 10 docs/s sustained | Batch import of a new institution | load |
| PERF-007 | Corpus embedding throughput (incremental) | ≥ 1 chunk/s | ≥ 40 chunk/s | Governs freshness after updates | load |
| PERF-008 | Query throughput | 20 QPS sustained | 100 QPS sustained | Comfortably above the modelled peak with headroom | load |
| PERF-009 | Peak burst tolerance | 3× sustained for 10 min without error-rate increase | — | Exam-period spikes | load |
| PERF-010 | Retrieval-stage share of end-to-end latency | < 40 % | < 30 % | Ensures generation cost does not dominate the budget | inspect |
| PERF-011 | Availability (query API), monthly | 99.5 % | 99.9 % | See §14 for why these differ by tier | inspect |
| PERF-012 | Recovery point objective / recovery time objective | RPO 24 h / RTO 4 h | RPO 15 min / RTO 1 h | Free-tier backup reality vs production | audit |
| PERF-013 | Degraded-mode latency when generation is disabled | p95 < 500 ms | — | Degradation must be an improvement, not a new outage | load |
| PERF-014 | Memory ceiling per instance | < 2 GB resident | < 4 GB | Bounded memory is what makes autoscaling possible | load |

### Note on PERF-003/004 and the free tier

T-1 (free 4-core ARM VM, no GPU) **cannot** meet PERF-003/004 for generation. Measured estimate:
a 1.5B-parameter Q4 model on 4 ARM cores yields ~13 tok/s decode and ~50 tok/s prefill, so a
2,000-token context and 250-token answer take roughly 60 s. This is stated as a limitation, not
hidden: on T-1 the extractive path (PERF-002) is the default answer path, and generation is an
opt-in "slow mode" with a visible warning. See [§4.4](03-scale-model.md#47-generation-feasibility-on-the-free-tier)
and [RISK-003](20-risk-register.md).

## 3.6 Requirement conflicts already identified

| Conflict | Resolution |
|---|---|
| NFR-010 (no PII in logs) vs OPS-003 (rich diagnostics) | Diagnostics store *identifiers and scores*, never content. Retrieval snippets are stored in the answer record, not in logs, and are access-controlled. |
| FR-033 (answer cache) vs SEC-001/SEC-003 (per-principal isolation) | Cache key includes a principal-set hash; a cache entry is only readable if the requesting principal's effective ACL set matches. Verified by an explicit negative test. |
| PERF-005 (freshness) vs cost/throughput on T-1 | Freshness SLO is tier-specific and honestly stated. The queue depth and backlog age are SLO'd, so slowness is visible rather than silent. |
| FR-021 (ACL in the scan) vs FR-033 (caching) | Cache correctness is defined as a function of the ACL set; if a principal's ACL changes, its cache namespace changes. This is why the ACL set is a cache-key component and not just a filter. |
| FR-023 (abstain) vs user expectation of "an answer" | Product decision: abstention is presented as a *finding* ("no authoritative source found; here is what exists"), not an error. This is a UX requirement, tracked in Phase 5. |
