# ADR-006: Free-First Deployment Target and the Enterprise Migration Path

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-04 |
| **Requirements** | §2.7 constraints, PERF-011, PERF-012, NFR-012, NFR-014 |
| **Related** | ADR-001, ADR-002, ADR-004 |

## 1. Decision

Build and demonstrate on **free and self-hostable infrastructure only**, organised into three named
service tiers (T-1 laptop, T-2 free cloud VM, T-3 production-scale). Define **separate, honest SLOs per
tier**, publish the free tier's real limitations, and document the migration path to paid infrastructure
as an explicit engineering exercise rather than an assumption.

No paid service is enabled by default. Any paid adoption requires an ADR with the trigger that fired.

## 2. Context

Two constraints pull against each other:

- **The brief requires production-scale architecture**: millions of documents, high concurrency, strict
  reliability, DR, observability.
- **The brief requires free implementation**: open-source, self-hostable, free-tier, demonstrable.

These are compatible only if they are kept **separate and explicit**. The failure mode is to design for
scale and then discover the free tier cannot run it — or to design for free and quietly claim production
readiness.

A third constraint makes it harder: the analytical work in [§4](../phase0/03-scale-model.md) shows
that a **free 4-core VM cannot meet any reasonable generative-answer latency SLO** with a local LLM
(~60 s per answer), and **cannot embed a large corpus** (~1.3–4.5 chunks/s).

## 3. Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Paid-first for quality** | Violates the free-first constraint; also hides the engineering reasoning behind an API call |
| **Free-first with no tier distinction** — claim one SLO everywhere | Would require promising 99.9 % on a sleeping free VM. **That is a false commitment**, and it is the dishonest option |
| **Free-first with no performance claims at all** | Hides useful information. Honest limits with measurements are more useful than silence |
| **Use a paid LLM API secretly** | Cost at scale is the dominant line item; egress breaks the security model; lock-in |
| **Local models only, no hosted option** | Correct as a default; but removing the adapter entirely would be over-abstracting against ADR-004. Keep the adapter, disabled |

## 4. Why this option

1. **It is the only option that is simultaneously free and truthful.** Tiers let each environment have a
   SLO it can actually meet, with the reason stated.
2. **The free tier becomes a genuine product, not a demo.** Extractive answering is not a degraded mode
   of the product — for regulations, quoting the correct clause is frequently the *correct* answer.
3. **The cost analysis stays honest.** [§18](../phase0/17-cost-model.md) shows the dominant cost is
   generation, and the dominant cost lever is **answering well without generating**. Free-first forces
   the design toward the cost-efficient architecture, which happens to be the better one.
4. **Portability is enforced by economics.** If we only ever ran on one provider, ports would be
   decoration. Free-first with a known-future migration forces the seams to be real and tested.
5. **It is honest about the trade.** A reviewer learns something real from "the free tier cannot embed a
   large corpus because CPU throughput is 1.3 chunks/s, and here is the GPU path" — more than from a
   system that mysteriously needs a credit card.

## 5. Advantages

- $0 to run and to demonstrate; the entire stack is portable containers.
- No data egress by default → no third-party prompt logging, no lock-in, no egress cost.
- Strong forcing function for architectural discipline: every component must justify itself against a
  tight resource budget.
- The limitations are **documented with measurements**, which is a better portfolio artefact than an
  unqualified success claim.

## 6. Disadvantages (accepted deliberately)

| Disadvantage | Mitigation |
|---|---|
| **Free tiers have no SLA and change without notice** | Stated (~99 % on T-2). Container portability makes a provider swap a config change (RISK-017) |
| **Free tiers have durability limits** — ephemeral disks, no PITR | **RPO 24 h on T-2, stated plainly.** Not hidden, not promised away |
| **Generative answers are slow or unavailable on free tiers** | Extractive is the default; generation is opt-in "slow mode" with the measured expectation shown |
| **No high availability** — single host | Stated. The architecture supports replicas; the free tier does not use them |
| **Capacity is small** — ~2,000-document demo corpus | Sized deliberately to the hardware. The seed corpus is a deliverable so a reviewer's 15-minute path works |
| The demo may *look* slow to a casual viewer | Presentation fix: show the measured comparison, not just the slow case. A demonstrated limitation with a measurement is a strength |

## 7. Tradeoffs

We traded **capability, latency and availability** on the demonstration tier for **zero cost, full
portability, no data egress and an honest, testable architecture**. We also traded the risk that a
reviewer dismisses the system as underpowered for the risk that they dismiss the engineering as
unsophisticated. The second risk is the more damaging one for this project, and the mitigation is
measurement, not marketing.

## 8. Operational consequences

- One container set, three configurations. **The same image runs in every tier.**
- Feature flags differ by tier: generation off by default on T-2; reranking off everywhere except GPU.
- **Monitoring must run off-host for alerting** — a dead host cannot report that it is dead. This is a
  real subtlety of free single-host deployments.
- The T-2 runbook must include "what to check if the free provider changes its terms", because it will.
- Backup verification happens off-host; RPO is published.

## 9. Scaling consequences

The migration triggers are written in advance ([§7.7](../phase0/06-architecture-options.md#77-what-would-change-the-answer)):
> 100 QPS sustained · GPU needed for embedding or generation · corpus beyond ~10⁵ chunks on free CPU ·
> multi-region requirement · regulatory isolation requirement.

Each trigger names the specific action, so migration is a response to an event rather than a
re-architecture.

## 10. Security consequences

| Consequence | Detail |
|---|---|
| **No third-party prompt exposure by default** | The strongest security posture available, and a direct consequence of free-first. Institutional document text never leaves our infrastructure |
| Fewer external integrations = smaller attack surface | No third-party API credentials to leak, no webhooks, no external SaaS |
| Optional hosted adapters are a controlled egress point | Opt-in per tenant, data-classification checked, default off, audited |
| Free tiers often imply shared/public infrastructure | Object store must be private with signed URLs regardless of the provider's defaults |

## 11. Cost consequences

| Tier | Monthly | Dominant item |
|---|---|---|
| T-1 | **$0** | Human time |
| T-2 | **$0** | Human time; hosting is free but **no SLA** |
| T-3 | $4k–18k (`ASM`) | LLM inference (~50–70 % of total) |

**The cost control with the highest leverage is answering extractively**, which is an architectural
decision made for correctness reasons and which happens to be the primary financial control. That
alignment — correctness, latency and cost all pointing the same way — is unusual and worth preserving.

## 12. Migration / replacement strategy

| From | To | Work required |
|---|---|---|
| T-1 laptop | T-2 free VM | Provisioning + `docker compose` up; no code change |
| T-2 free VM | T-3 managed | Postgres → managed instance; MinIO → object storage (`ObjectStore`); add GPU worker; enable reranking + generation flags; replicas |
| T-3 self-hosted models | Hosted model APIs | Flip `LLMProvider`/`EmbeddingProvider` configuration; re-run the benchmark to confirm quality |
| Any tier | Multi-region | The new architecture in `18-future/multi-region`; consistency trade-offs documented there |

**No migration requires a change to the domain layer.** That is the architectural test of whether the
ports in ADR-004 are real, and it is the reason this ADR and ADR-004 were written together.

## 13. Consequences if this is wrong

| If wrong | Signal | Response |
|---|---|---|
| The free tier cannot host a credible demo | Sustained outages, or capacity too small for a meaningful corpus | Move the demo to T-1 (laptop-tier demo is always available as the floor); document the free-tier constraints as the finding |
| Free-tier terms change | Provider announces a change | Container portability → config change; contingency documented |
| Reviewers consider the free tier unrepresentative | Feedback | Show the T-3 architecture design and measured capacity model; the point of the tiers is to make both visible |
| A paid service would materially improve the demo | Measured quality gap | Add the adapter behind a flag; document cost and lock-in; **default stays off** |