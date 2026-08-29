# ADR-0046: Predictive fleet planning and verified optimization

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Approved
- Date: 2026-08-29
- Target: `4.7.5`
- Approver: leizd

## Context

4.7.4 closed real-MinIO proof, crash takeover, and atomic admission, but three
correctness gaps remained: absent RiskSubjects could stay OPEN forever, fair
share was charged when an action was only assigned to a wave, and multi-wave
plans were not executed as a durable, revalidating state machine.

## Decision

1. Risk snapshots carry per-type coverage. Complete coverage plus an absent
   subject reconciles the observation; incomplete coverage never implicitly
   clears it.
2. Scheduler ledgers reserve on schedule and consume only verified terminal
   bytes/duration. PREEMPTED, STALE, and REPLAN release the reservation.
3. A durable wave executor admits Wave N only after predecessor verified
   success and fresh Authority/risk/budget/blast revalidation.
4. Fleet SLO percentiles are computed in explicit 1h/24h/7d/30d/lifetime
   windows; empty windows are INSUFFICIENT_DATA.
5. Forecasts, costs, and placement candidates are derived from durable
   observations and a versioned price catalog. Safety is a hard constraint.
6. What-If and federation remain side-effect-free and credential-free.

## Consequences

- Production scheduling no longer inflates virtual runtime by replanning.
- Ghost backup risks cannot permanently degrade Fleet Readiness.
- Optimization proofs bind forecast, catalog, authority, and plan digests
  without bumping the evidence-proof-v2 envelope.
- Cross-fleet mutation remains a 4.8.0 non-goal.
