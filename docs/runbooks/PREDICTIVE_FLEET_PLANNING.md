# Runbook: Predictive Fleet Planning & Verified Optimization (DeepSeek Infra 4.7.5)

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

DeepSeek Infra 4.7.5 predicts 30/90-day capacity and enumerates cheaper
placements without executing destructive decisions.

## Operator projection

- `GET /api/workspace/resilience/readiness` — source-backed readiness, windowed SLO, risk-debt score.
- `GET /api/workspace/resilience/waves` — durable wave state for the latest schedule.
- `GET /api/workspace/resilience/capacity-forecast` — 30/90-day P50/P90 from durable observations.
- `POST /api/workspace/resilience/whatif` — side-effect-free candidate simulation.
- `GET /api/workspace/resilience/federation` — credential-free remote-fleet thought experiment.

## Hard rules

1. Incomplete snapshot coverage must not clear OPEN risk.
2. Assigned-to-wave is not consumed fair share.
3. Wave N cannot start before Wave N-1 verified terminal success.
4. Insufficient forecast/SLO samples return `INSUFFICIENT_DATA`.
5. Unknown prices are `UNKNOWN_COST`, never zero.
6. Cheaper plans that reduce copies or failure domains are rejected.
7. What-If and federation never mutate storage, Authority, or the action journal.

## Non-goals

LLM-autonomous placement, automatic copy deletion, durability weakening,
primary promotion, policy mutation, real cross-fleet writes, Receipt v5,
Commit v5, and object-set-v2.
