# 4.7.6 Todo — Production Predictive Control & Verifiable Simulation

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

- [x] Prepare 4.7.6 version surfaces and lock frozen protocols
- [x] Build fail-closed production fresh-state bundle
- [x] Remove caller-supplied Wave freshness and safety inputs
- [ ] Add fenced schedule/wave/action execution epochs and leases
- [ ] Run admitted actions through the production Action Journal
- [ ] Reconcile crash takeover without duplicate remote effects
- [ ] Persist terminal effect transfer telemetry
- [ ] Settle fair service exactly once from observed telemetry
- [ ] Add production capacity sampler to maintenance control loop
- [ ] Bind observations to target incarnation and capacity revision
- [ ] Persist 30/90-day Forecast Registry records
- [ ] Automatically backtest due forecasts from later observations
- [ ] Build optimizer present truth from authoritative sources
- [ ] Restrict What-If requests to hypothetical candidates
- [ ] Add write-deny simulation capability and attempted-write audit
- [ ] Measure and compare pre/post state digests for every mutation domain
- [ ] Add `predictive-planning-proof-v1` typed payload and validator
- [ ] Emit exact predictive proof artifact from the real runner
- [ ] Require report + autonomous proof + predictive proof in assembly
- [ ] Add real Three-MinIO predictive planning E2E
- [ ] Preserve read-only federation boundary
- [ ] Update runbook, ADR, architecture, and release notes
- [ ] Run CI-equivalent verification with 95.0% coverage margin
