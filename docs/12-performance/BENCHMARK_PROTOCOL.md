# Benchmark Protocol

Phase 1 — reproducible measurement of the engineering foundation.

| Field | Value |
|---|---|
| **Status** | Active |
| **Applies to** | `benchmarks/` |
| **Framework** | `benchmarks/framework/` |
| **Runner** | `python -m benchmarks.run` |
| **Last reviewed** | 2026-10-04 |

---

## 0. The most important section

This project classifies every performance or capacity number as exactly one of:

| Class | Meaning |
|---|---|
| **ESTIMATE** | Derived by calculation or reasoning. Not measured. |
| **BENCHMARK** | Produced by running the harness in this repository, with the protocol below. |
| **TARGET** | A level the project intends to reach. Not yet achieved. |
| **SLO** | A level the project commits to, with consequences attached. |
| **OBSERVED** | A measurement of a real deployed system, as opposed to a harness run. |

**A `BENCHMARK` produced by this protocol is never an `OBSERVED` measurement.** The
harness runs in one process on one machine. Nothing here is evidence about production
behaviour, and §19 lists exactly what is excluded.

Two rules follow, and both are enforced in code rather than by convention:

1. A benchmark that cannot run reports `BLOCKED` with a reason. It never reports `0`.
   Zero milliseconds is a claim — an excellent one — and a blocked benchmark has made no
   claim at all. Enforced by `BenchmarkResult.validate`, tested by
   `test_blocked_must_not_carry_statistics`.
2. Every result carries a `scope`. `in_process` numbers are labelled so they cannot be
   quoted as production latency.

---

## 1. Purpose

To make performance claims about this codebase **checkable rather than asserted**.

Every latency figure in Phase 0 that could not be justified was corrected by the review
that produced this protocol. What remains is a need for a mechanism that produces
defensible numbers, records enough context to interpret them, and reports honestly when
it has nothing to measure.

This protocol defines that mechanism. It does **not** define performance requirements;
see §16.

## 2. Scope

**In scope.** Micro-benchmarks of code that exists in Phase 1: configuration loading,
domain functions, health aggregation, and the HTTP request path through the real
application.

**Out of scope, deliberately.**

- Retrieval, generation, embedding or ingestion latency. None of that code exists.
  Benchmarking it would require a mock, and a number from a mock presented as system
  performance is worse than no number.
- Load and soak testing. Phase 0's OPS-015 assigns that to a nightly CI job against a
  real service, and OPS-009/PERF-009 to k6. This harness measures one process
  synchronously; it is not a load generator and must not be described as one.
- Network-path latency. See §19.

## 3. Benchmark environment

| Property | Requirement |
|---|---|
| Interpreter | CPython 3.12+ (`requires-python`; verified on 3.14.3) |
| Process model | Single process, single event loop, no concurrency |
| Database | Local PostgreSQL only, when available. Never a hosted instance |
| Network | None. The harness makes no outbound request |
| Cost | $0. Every dependency is open source and local |

Cost and network are hard constraints from the project's free-first ADR-006, not
preferences. A benchmark harness that called a hosted service would make measurement
conditional on someone else's availability and would leak a dependency that a
reproducibility claim cannot survive.

## 4. Hardware and software disclosure

Every result records, via `benchmarks/framework/environment.py`:

- Git commit SHA, and **whether the working tree was dirty** — a dirty tree means the
  SHA does not fully describe the code that ran
- Python version and implementation
- Platform, machine, processor, CPU count
- Versions of the ten distributions most likely to move a number: fastapi, starlette,
  pydantic, pydantic-settings, uvicorn, structlog, psycopg, sqlalchemy, alembic, httpx

The full set of 47 pinned versions lives in `requirements.lock` and is not duplicated
into every result.

A latency number presented without its environment is not a result. It is an anecdote.

## 5. Warm-up policy

**20 warm-up iterations by default, executed and discarded.** They are never counted,
never reported, and never included in any percentile.

First-call costs are real and are not the steady state: module import, lazy
initialisation, CPU cache misses, allocator warm-up. Mixing them into the distribution
produces a p99 that no steady-state caller will ever experience.

The count is still recorded on the result (`warmup_iterations`), so a reader knows how
much was discarded.

**Warm-up is not sufficient for the first benchmark in a process.** See §11 — this is a
measured limitation, not a solved problem.

## 6. Number of iterations

| Benchmark class | Measured iterations | Rationale |
|---|---|---|
| Async / API (per suite default) | 500 | Stable p99 without a long run |
| Sync domain functions | 200 | Sub-microsecond; variance is proportionally larger |
| Live database connect | 25 | Connections are expensive; 25 still resolves a p95 |

**MEASUREMENT METHODOLOGY — NOT A PERFORMANCE REQUIREMENT.** These counts are chosen for
a stable distribution within a few seconds. They are not derived from any Phase 0
requirement, and Phase 0 defines none.

## 7. Timing methodology

- `time.perf_counter_ns()` — monotonic, highest resolution available without a
  dependency. `time.time()` is neither monotonic nor high-resolution, and a clock
  adjustment mid-run can produce a negative duration.
- Only the operation is timed. Setup — building the application, constructing settings,
  connecting to the database — happens outside the timed region.
- Sync operations are timed directly. Async operations run on **one** event loop created
  for the entire benchmark. Creating a loop per iteration would measure loop construction
  and teardown, which for an in-process coroutine is the larger cost by a wide margin.
- The operation's return value is discarded. Retaining results would add allocator
  pressure that production would not feel.

## 8. Percentiles

**Nearest-rank, no interpolation.**

```
rank  = ceil(p / 100 × N)
index = min(max(rank − 1, 0), N − 1)
```

Reported: **p50, p90, p95, p99**, plus min, median and max.

Nearest-rank returns a value that was **actually observed**. An interpolated p95 of
3.7 ms between two samples that never occurred is arithmetic, not measurement, and is the
usual source of a confidently wrong latency claim. A test asserts the returned value is
always a member of the input.

p90 is reported alongside the OPS-015 p50/p95/p99 trio because a regression usually
appears in the p90 before it reaches p99.

## 9. Mean and median treatment

Both are reported, and **they are different functions**:

- `median` — the statistical median; for an even sample count, the mean of the two
  central values.
- `p50` — the nearest-rank 50th percentile; for an even sample count, the lower of the
  two central values.

For 200 samples they differ. Reporting both makes the convention visible instead of
leaving a reader to guess which one a number is.

**The mean is deliberately not reported.** A single 400 ms outlier from a garbage
collection pause moves a mean by more than it moves a median, and latency is a
distribution whose right tail is the part users experience.

## 10. Outlier handling

**No outliers are removed.**

Removing them is tempting and wrong here. The right tail of a latency distribution *is*
the interesting part: an outlier may be the exact scheduling pause, allocation stall or
GC cycle a user will hit. Discarding them produces a flattering number that describes a
system that does not exist.

The p99 and max are reported precisely so the tail is visible. If an outlier indicates a
real defect, the correct response is to fix it, not to filter it.

Run-to-run noise is handled separately, by the regression tolerance (§16), not by
filtering samples.

## 11. Cold versus warm runs

A run is **warm** when the process has already executed that benchmark's code path. The
protocol requires baselines to be captured warm (§15).

Cold and warm runs differ substantially, and the difference was measured. Capturing a
baseline on the very first run in a process recorded `config.load_settings` at p95
0.80 ms; the immediately following run on identical code recorded 1.53 ms — a **90 %
apparent regression that was entirely a cold-start artefact**.

Reported results are warm. A cold number is labelled as such wherever it appears.

## 12. Database state requirements

The live-database benchmark requires `BENCHMARK_DATABASE_URL` pointing at a scratch
PostgreSQL instance. Without it the result is `BLOCKED` with a reason.

**What it does not do:** time a connection refusal. The time to receive a refused
connection is a property of this host's network stack, not of the application, and
reporting it would be a fabricated database measurement.

**What Phase 1 does not yet have, stated plainly:** repository, transaction and
idempotency benchmarks against live PostgreSQL. The repositories exist and are covered
by unit tests against in-memory doubles; benchmarking those doubles would measure the
double. See §19.

## 13. Dataset and input requirements

All Phase 1 benchmarks use **fixed, in-source inputs** — there is no dataset.

- Configuration: a fixed 25-field environment, declared in the suite source
- Idempotency key: a fixed well-formed key
- Health probes: `StaticProbe` with constant values
- Clock: `FixedClock` at a fixed instant

Inputs are declared in code rather than read from the environment. A benchmark that
inherits the developer's shell has an unrecorded parameter, and two runs on two machines
stop being comparable.

Phase 0's benchmark corpus and evaluation slices belong to retrieval work and are not in
scope here.

## 14. Reproducibility requirements

Phase 0's **NFR-014** requires that "two runs on the same commit produce identical
aggregate metrics within tolerance", because "benchmark noise makes optimisation
meaningless". What this protocol guarantees:

**Guaranteed:**

- Identical inputs across runs — fixed in source (§13)
- A deterministic percentile function — nearest-rank, no interpolation (§8)
- No filtering of samples, so no run drops data another run kept (§10)
- Full environment capture on every result (§4)
- A stable, versioned result schema (`schema_version`), so a baseline from a future
  format is rejected rather than misread

**Not guaranteed, and this is measured rather than assumed.** Five consecutive warm runs
on the development host:

| Benchmark | min p95 | max p95 | Spread | Coefficient of variation |
|---|---|---|---|---|
| `config.load_settings` | 1.5764 ms | 1.7952 ms | 13.9 % | 4.8 % |
| `api.request_healthz` | 2.3011 ms | 3.0044 ms | 30.6 % | 10.9 % |

### 14.1 A negative result: more warm-up does not fix this

`config.load_settings` kept reporting regressions against a warm baseline, so three
hypotheses were tested — residual cold start, insufficient warm-up, and host noise —
by running it six times at three different warm-up counts:

| Warm-up | p95 samples (ms) | Mean | CV | Spread |
|---|---|---|---|---|
| 20 | 1.5497 · 1.4833 · 0.9909 · 1.8820 · 1.8777 · 1.8137 | 1.5996 | 21.5 % | 89.9 % |
| 100 | 1.5022 · 1.3274 · 1.2897 · 1.7840 · 1.8277 · 1.3157 | 1.5078 | 16.1 % | 41.7 % |
| 300 | 1.3311 · 1.3838 · 1.0553 · 1.0860 · 0.8084 · 0.8491 | 1.0856 | 21.9 % | 71.2 % |

**Warm-up does not help.** Tripling it changed the coefficient of variation by less than
noise and left it between 16 % and 22 %. The variance is not cold start; it is the host —
a shared Windows development machine with no CPU pinning, running background processes,
measuring itself.

The consequence is stated rather than smoothed over: **on this host, no Phase 1 benchmark
supports regression gating at fine resolution.** The 30 % threshold in §16 is already at
the edge of what the hardware can support for `config.load_settings`, whose spread alone
reaches 90 %. A verdict from a single run on a single benchmark should be treated as a
prompt to re-run, not as a finding.

Reducing this variance requires a quieter machine (dedicated host, pinned cores, no
concurrent load), not a larger number in the code.

## 15. Baseline capture

```bash
python -m benchmarks.run --save-baseline
```

Requirements for a trustworthy baseline:

- **Warm.** Capture on a second run, never the first (§11).
- **One machine.** A baseline is a record of one machine's performance.
- **Committed? No.** `.benchmarks/` is gitignored. Committing a baseline would invite
  cross-machine comparison, which is meaningless, and would produce diff noise on every
  run.
- **Re-capture when** the machine changes, the dependency set changes, or a benchmark is
  edited. A stale baseline produces confident nonsense.

## 16. Regression thresholds

**There is no Phase 1 performance target to regress against, and none is invented here.**

Phase 0 defines fourteen PERF requirements. **All fourteen describe search, retrieval,
generation or ingestion** — see §16.1. None applies to a configuration parser or a
health endpoint.

What the framework therefore has is a **sensitivity**: how much slower than its own
previous self a benchmark must get before a human is asked to look.

| Setting | Value | Class |
|---|---|---|
| `DEFAULT_REGRESSION_TOLERANCE` | 0.30 (30 %) | **MEASUREMENT METHODOLOGY — NOT A PERFORMANCE REQUIREMENT** |

Derived from the measured variance in §14, not chosen by taste. A 20 % threshold — the
obvious round number — produces false regressions on unchanged code, which is the
fastest way to teach a team to ignore a benchmark. 30 % sits above the observed noise
while remaining far below the 2–10× change a real regression produces.

**Stated honestly: a 30 % threshold will miss a genuine 25 % regression.** In-process
micro-benchmarks on a shared machine cannot do better. Finer resolution needs a quieter
machine and more iterations, not a smaller number here.

**And the converse: 30 % can still false-alarm.** §14.1 measures a coefficient of
variation of up to 21.5 % and a spread of 90 % for `config.load_settings` on this host.
A single regression verdict from that benchmark should be re-run before it is believed.
This is why §18 rule 1 says a verdict is a prompt to investigate.

### 16.1 Phase 0 targets, and why none applies yet

Reproduced verbatim from [`docs/phase0/02-requirements.md`](../phase0/02-requirements.md).
**None is a Phase 1 target**, because Phase 1 implements none of the functionality they
describe.

| ID | Requirement | T-1 target | T-3 target | Applicable in Phase 1? |
|---|---|---|---|---|
| PERF-001 | Search-only latency | p95 < 500 ms | p95 < 250 ms | **No** — no search |
| PERF-002 | Extractive answer latency | p95 < 900 ms | p95 < 400 ms | **No** — no answering |
| PERF-003 | Generated answer latency (TTFT) | p95 < 2.0 s | p95 < 1.2 s | **No** — no generation |
| PERF-004 | Generated answer latency (complete) | p95 < 8 s | p95 < 6 s | **No** — no generation |
| PERF-005 | Ingestion: upload → searchable | < 120 s | < 60 s | **No** — no ingestion |
| PERF-006 | Ingestion throughput | 2 docs/s | 10 docs/s | **No** — no ingestion |
| PERF-007 | Corpus embedding throughput | ≥ 1 chunk/s | ≥ 40 chunk/s | **No** — no embeddings |
| PERF-008 | Query throughput | 20 QPS sustained | 100 QPS sustained | **No** — no query path |
| PERF-009 | Peak burst tolerance | 3× sustained 10 min | — | **No** — no query path |
| PERF-010 | Retrieval share of end-to-end latency | < 40 % | < 30 % | **No** — no retrieval |
| PERF-011 | Availability (query API) | 99.5 % monthly | 99.9 % | **No** — no query API |
| PERF-012 | RPO / RTO | 24 h / 4 h | 15 min / 1 h | **No** — no deployment |
| PERF-013 | Degraded-mode latency | p95 < 500 ms | — | **No** — no degraded answer path |
| PERF-014 | Memory ceiling per instance | < 2 GB resident | < 4 GB | **No** — no deployed instance |

Two Phase 0 requirements *do* bear on this protocol, and both are honoured:

- **NFR-014** — deterministic, comparable benchmarks (§14).
- **OPS-015** — a report carrying p50/p95/p99 and error rate (§17).

**Ambiguity identified rather than resolved:** PERF-001…014 are stated per tier (T-1
laptop, T-3 production) but the harness in this protocol runs on a developer machine,
which is neither. Phase 0 defines no "development tier" SLO. Rather than invent one,
this protocol labels every result `in_process` and claims nothing about any tier. A
future phase that adds a deployed environment should define which tier its benchmark
reports against, or add a `DEPLOYED` scope alongside a documented SLO.

## 17. Reporting format

Human-readable table:

```
benchmark                                status    scope                  p50         p95         p99  units
---------------------------------------------------------------------------------------------
config.load_settings                     ok        in_process          1.2152      1.7050      2.4894  milliseconds
domain.validate_idempotency_key          ok        in_process          0.0013      0.0014      0.0016  milliseconds
api.request_readyz                       ok        in_process          2.3752      4.2069      6.3319  milliseconds
db.connect                               blocked   blocked                  -           -           -  BENCHMARK_DATABASE_URL is not set...
```

Machine-readable JSON in `.benchmarks/results.json`, one object per result:

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.0"`; a mismatch is rejected |
| `name` | string | Stable identifier, also the baseline key |
| `status` | enum | `ok` / `blocked` / `error` |
| `scope` | enum | `in_process` / `live_database` / `blocked` |
| `units` | string | `"milliseconds"` |
| `iterations` | int | Measured iterations |
| `warmup_iterations` | int | Discarded iterations |
| `timestamp` | string | ISO-8601 UTC |
| `environment` | object | Commit SHA, dirty flag, interpreter, hardware, package versions |
| `statistics` | object | `samples`, `min`, `median`, `p50`, `p90`, `p95`, `p99`, `max` |
| `blocked_reason` | string | Present only when `status=blocked` |
| `error` | string | Present only when `status=error` |
| `notes` | string | What the number does **not** include |

Comparison output adds `verdict` ∈ {`regression`, `improvement`, `unchanged`, `new`,
`skipped`, `incomparable`} and `relative_change`.

Exit codes: `0` success (BLOCKED is not a failure), `1` a benchmark errored, `2` a
regression was detected.

## 18. Interpretation rules

1. **A regression verdict is a prompt to investigate, not a failure.** Confirm it is real
   before acting. The exit code exists to draw attention, not to fail a build.
2. **Never compare across machines.** Two hosts differ by more than most regressions.
3. **Never compare across scopes or units.** The framework refuses both rather than
   producing a meaningless verdict.
4. **Never quote an `in_process` number as production latency.** It excludes everything in
   §19.
5. **A `BLOCKED` result is not a fast result.** Check the reason before drawing any
   conclusion from its absence.
6. **Prefer p95 over the mean.** The mean is not reported, deliberately.
7. **When a change makes a benchmark slower, ask whether the benchmark still measures what
   it was built to measure.** A faster number can mean a cache was added — or that the
   work was skipped.

## 19. Known limitations

Stated so nobody has to rediscover them.

1. **No network path.** API benchmarks use an in-process ASGI transport. Excluded: TCP,
   TLS, uvicorn's event loop, kernel networking, and the real cost of serving a request.
   Production latency will be **substantially higher**, by an amount this harness cannot
   estimate. These numbers measure application code only.
2. **Log output is discarded, log processing is not.** The application logs every
   request. `ReturnLoggerFactory` runs the whole structlog chain — context merge,
   redaction, timestamping, rendering — and discards only the final write. Filtering by
   level instead would skip the processor chain and understate the cost. Writing to a
   terminal, which is what would otherwise happen, would measure a terminal.
3. **Single-threaded, no concurrency.** No contention, no queueing, no back-pressure.
   This harness cannot observe behaviour under load; that is k6's job (OPS-015).
4. **No live-database measurements in Phase 1.** `db.connect` is BLOCKED without
   `BENCHMARK_DATABASE_URL`, and the Docker daemon is unavailable in this environment.
   Repository, transaction and idempotency benchmarks against real PostgreSQL do not yet
   exist.
5. **Sub-microsecond benchmarks are at the resolution floor.** `perf_counter_ns` has
   roughly microsecond-scale overhead on this platform; the ~1 µs domain results are
   near that floor and should be read as "very fast", not as a precise figure.
6. **Measured variance is high for API benchmarks** (§14, 10.9 % CV) and very high for
   `config.load_settings` (§14.1, up to 21.5 % CV and 90 % spread). Conclusions from a
   single run are weak, and increasing warm-up does not improve this.
7. **First-in-process measurements are cold** (§11) and should not be captured as a
   baseline.
8. **No memory or CPU profiling.** Resource ceilings such as PERF-014 are not addressed.
9. **The GIL is enabled** on CPython 3.14; recorded on every result so a future
   free-threaded comparison is possible.

## 20. Related

- [`pyproject.toml`](../../pyproject.toml) — `benchmarks` in ruff/mypy scope
- [`Makefile`](../../Makefile) — `make benchmark`, `make benchmark-save`
- [`requirements.lock`](../../requirements.lock) — the pinned environment
- Phase 0: `docs/phase0/02-requirements.md` (PERF, NFR-014, OPS-015),
  `docs/phase0/09-technology-evaluation.md` (k6 for load), `docs/phase0/03-scale-model.md`
- Next: `docs/00-overview/MEASUREMENT_TERMINOLOGY.md` — the authoritative terminology
  document. **Not yet written.** This protocol's §0 summarises it; the document remains a
  Phase 1 deliverable.