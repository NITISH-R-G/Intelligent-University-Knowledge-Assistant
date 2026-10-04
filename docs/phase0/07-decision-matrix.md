# §8 Decision Matrix

**Purpose.** Make the topology decision reproducible and falsifiable, so that a reviewer can disagree
with a *weight* or a *score* and re-derive the result rather than arguing about taste.

**Method.** Multi-criteria decision analysis. Each criterion is scored 1–5 (5 = best) for each of the
four options in [§7](06-architecture-options.md). Weighted total = Σ(weightᵢ × scoreᵢ). Weights sum to
1.00. Scores are engineering judgement and are stated as such — the sensitivity analysis at §8.4 is
the honest part.

---

## 8.1 Criteria and weights

| Criterion | Weight | Why this weight |
|---|---:|---|
| **Scalability** | 0.15 | The brief requires a design that can reach 10⁶ documents / 10⁸+ chunks. High, but *not* the highest weight: the current requirement is a working, demonstrable system, not a scaled one. |
| **Reliability** | 0.15 | Equal to scalability. A knowledge system that is confidently wrong is worse than one that is unavailable — the whole correctness argument in §1.5 is a reliability argument. Fault isolation between the read path and the write path is the specific property being bought. |
| **Security** | 0.12 | High because tenant isolation is the highest-severity failure mode (THR-003) and it is *architectural*, not a control you bolt on. Not higher: security also depends heavily on controls (sandbox, RLS, output contract) that are similar across B/C/D. |
| **Performance** | 0.10 | Moderate. The query path is ~200 ms of retrieval in every option; the differentiation is ingestion throughput and latency under load, which is a real but not dominant concern at this scale. |
| **Cost (free-tier feasible)** | 0.13 | Above average because the free-tier constraint is a **hard project constraint**, not a preference. Note the scoring includes *operator attention*, which is the actual scarce resource. |
| **Operability** | 0.13 | Equal to cost. A single maintainer cannot responsibly operate 8 microservices. Operability is the constraint that decides this matrix as much as cost does. |
| **Maintainability** | 0.09 | Important for a portfolio project that must be *readable*; solo-maintainer change cost is high. |
| **Developer complexity** | 0.08 | Time-to-correctness. Weight is deliberately modest: complexity has a real cost, but it is not automatically bad — it buys capability. Distinguished from Operability deliberately. |
| **Migration path** | 0.05 | Lowest, because migration paths are *cheap to keep* (seams, ports, versions) and expensive to buy back later. A design that preserves seams should be credited, but not credited twice by also scoring Maintainability high. |

**Sum = 1.00** ✅ (verified: 0.15+0.15+0.12+0.10+0.13+0.13+0.09+0.08+0.05)

### Deliberate anti-manipulation choices

Stating these makes the matrix reviewable:

- **The matrix is not the sole arbiter.** §8.6 lists the qualitative factors that scored no better than
  "medium". B also wins because of the decomposition rules (§9.6), which are a design property, not a
  score.
- **Scalability is not weighted highest**, even though the brief emphasises scale — because the brief
  also emphasises demonstrability on free infrastructure, and those two pull against each other.
- **Cost and operability are weighted above performance.** A 2× faster system that needs an on-call
  rotation is worth less to this project than a slightly slower one that needs none.
- **Migration path is weighted lowest**, on the argument that it is cheap to preserve. If a reviewer
  believes otherwise, §8.4 Scenario 2 shows the result is unchanged anyway.

## 8.2 Scores

| Criterion | A | B | C | D | Scoring note (why not symmetric) |
|---|---:|---:|---:|---:|---|
| Scalability | 2 | 4 | 5 | 5 | A fails independent scaling of ingest vs serve. B is capped by single-node Postgres ~10⁸ chunks. |
| Reliability | 3 | **5** | 3 | 4 | C's partial-failure surface offsets its isolation benefit; D trades failure for staleness. |
| Performance | 4 | **5** | 4 | 4 | B wins on contention, not raw speed. All four are fine on the query path. |
| Security | 3 | 4 | 4 | 4 | B's parser sandbox is a genuine gain; C/D add more auth surface. |
| Cost | 5 | **5** | 2 | 2 | A and B both fit a free VM. C and D cost *attention*, not just money. |
| Operability | 3 | **4** | 2 | 1 | Two roles is learnable; 8 services is not, solo. |
| Maintainability | 5 | 4 | 3 | 3 | A is simplest to change; B's module/queue discipline costs a little. |
| Dev complexity | 5 | 4 | 2 | 1 | Concurrency reasoning in B; distributed state in C/D. |
| Migration path | 3 | **5** | 5 | 4 | B already contains the seams; A does not. |
| Free-tier feasibility | 5 | 5 | 2 | 2 | Memory/CPU budget of a free VM. |

## 8.3 Result

| Rank | Option | Weighted score | Margin |
|---:|---|---:|---|
| **1** | **B — Modular monolith + async workers** | **4.43** | **+0.88 over runner-up** |
| 2 | A — Synchronous modular monolith | 3.55 | −0.88 |
| 3 | C — Service-oriented | 3.28 | −1.15 |
| 4 | D — Event-driven services | 3.17 | −1.26 |

The margin over A (0.88 on a 1–5 scale, ~25 % relative) is large enough that the conclusion is not a
artefact of small weight perturbations. A is the credible alternative and its rejection is on
*requirements*, not points: it fails PERF-005, PERF-006, NFR-007, OPS-005 and SEC-007.

## 8.4 Sensitivity analysis

A weighted matrix is only meaningful if the result survives plausible disagreement. Three scenarios:

### Scenario 1 — "Scalability must dominate"

Rationale a reviewer might give: *"This is meant to reach millions of users. Weight scalability at 0.30
and cut complexity."*

| Criterion | Weight |
|---|---:|
| Scalability | 0.30 |
| Reliability | 0.13 |
| Security | 0.12 |
| Performance | 0.10 |
| Cost | 0.13 |
| Operability | 0.13 |
| Maintainability | 0.03 |
| Dev complexity | 0.02 |
| Migration path | 0.05 |
| **Sum** | **1.01** (normalised: B 4.41, A 3.15, C 3.63, D 3.56) |

**Result: B still wins (4.41).** C closes most of the gap (3.15 → 3.63) but does not overtake, because
B scores well on *every* other criterion including the one being up-weighted.

### Scenario 2 — "Operability and cost must dominate"

Rationale: *"One person maintains this. Optimise for things one person can run."*

| Criterion | Weight |
|---|---:|
| Cost | 0.22 |
| Operability | 0.20 |
| Dev complexity | 0.18 |
| Security | 0.12 |
| Performance | 0.10 |
| Scalability | 0.05 |
| Reliability | 0.08 |
| Maintainability | 0.05 |
| **Sum** | **1.00** |

**Result: B 4.65, A 4.10, C 2.97, D 2.62.** B's margin over A *widens* to 0.55. Notably **A becomes a
stronger contender** — which is the correct signal: if the project scope shrank to a read-only corpus,
A would be the right answer.

### Scenario 3 — "Everything is equal on cost and ops; pick the best design"

If Cost and Operability are dropped entirely (weights redistributed proportionally), the ordering is
unchanged: B > A > C > D.

### What the sensitivity analysis establishes

1. **B is robust.** It wins under all three weightings. It is not a knife-edge result.
2. **A is the genuine alternative**, and its relative strength grows as operability is weighted up.
   The specific requirements that eliminate A (async ingestion, backpressure, DLQ, parser isolation)
   are what decide this, and those requirements come from §3, not from taste.
3. **C never wins.** It would need either (a) 5× more weight on scalability than is defensible for the
   current workload, or (b) an organisational change (a second team). Both are stated as *triggers*
   in §7.7, not as reasons to pre-build.

## 8.5 Confidence

| Aspect | Confidence | Basis |
|---|---|---|
| The weights reflect this project's constraints | **High** | Traced to explicit constraints (§2.7) and to the single-maintainer fact |
| The 1–5 scores reflect reality | **Medium** | Engineering judgement, partly experience-based; the rankings within each column are robust even if absolute values are not |
| B dominates | **High** | Survives all three re-weightings |
| A would be acceptable if scope shrank | **Medium-High** | True, and recorded as a trigger to revisit |
| The matrix is the right method | **Medium** | Standard MCDA, but it cannot express non-compensatory constraints — see §8.6 |

## 8.6 What the matrix cannot express

Recorded because pretending a scoring matrix is complete reasoning would be dishonest.

1. **Non-compensatory constraints.** A matrix can average away a fatal flaw. Option A's single failure
   of SEC-007 (parser isolation) is not "worth 0.3 points" — it is disqualifying. **We therefore apply
   the matrix as a filter, then a veto test:**

   | Veto | A | B | C | D |
   |---|:--:|:--:|:--:|:--:|
   | Meets every MUST requirement? | ❌ | ✅ | ✅ | ⚠️ eventual consistency breaks FR-009 |
   | Can run on free infrastructure within memory/CPU budget? | ✅ | ✅ | ❌ | ❌ |
   | Maintainable by one engineer? | ✅ | ✅ | ❌ | ❌ |
   | Strong consistency available for version switch (FR-009, FR-012)? | ✅ | ✅ | ⚠️ | ❌ |

   **A is eliminated by the veto test** (fails MUST requirements), **D is eliminated** (fails FR-009,
   which is the project's core correctness promise), and **C is eliminated** (cost/operability veto).
   B survives the veto test and the matrix.

2. **Interdependencies.** The matrix treats criteria as independent; they are not. Operability and cost
   are strongly correlated; scalability and developer complexity are inversely correlated. Independent
   scoring slightly *understates* the disadvantage of C and D.

3. **Second-order effects.** The outbox pattern from D and the service boundaries from C will be adopted
   inside B regardless of the matrix outcome. The matrix does not credit that.

4. **Time horizons.** Options were scored for the whole project horizon. C/D score worse early and
   would score better in year three — which is why §7.7 states the *trigger conditions* rather than
   arguing that C/D are wrong forever.