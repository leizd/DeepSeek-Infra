use crate::envelope::PREDICTIVE_PLANNING_PROOF_SCHEMA;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fmt::Write;

pub const PREDICTIVE_PROOF_CHECKS: &[&str] = &[
    "realCapacityChangesDriveForecast",
    "realMinioInventoryUnchangedByWhatIf",
    "predictiveProofBindsCapacityObservationSet",
    "predictiveProofBindsForecastRecord",
    "predictiveProofBindsForecastBacktest",
    "predictiveProofBindsFreshStateBundle",
    "predictiveProofBindsPreAndPostState",
    "predictiveProofRejectsSelfReportedZeroMutation",
    "capacityForecastUsesDurableObservations",
    "forecastBacktestErrorIsPersisted",
    "costModelUsesVersionedPriceCatalog",
    "optimizerNeverReducesMinCommittedCopies",
    "optimizerNeverReducesMinFailureDomains",
    "whatIfProducesNoStorageWrites",
    "whatIfProducesNoStorageDeletes",
    "whatIfDoesNotMutateAuthority",
    "whatIfDoesNotMutateActionJournal",
    "whatIfBindsObservedFleetSnapshot",
    "whatIfIncludesRunningEffects",
    "whatIfIncludesMaintenanceWindows",
    "optimizationProofBindsForecastDigest",
    "optimizationProofBindsPriceCatalogDigest",
    "optimizationProofBindsAuthorityHead",
    "optimizationProofRecomputesSafetyConstraints",
];

const STATE_DOMAINS: &[&str] = &["storage", "authority", "actionJournal", "policy", "target"];
const SHA256_LENGTH: usize = 64;

pub fn predictive_planning_proof_digest(value: &Value) -> Option<String> {
    let mut payload = value.as_object()?.clone();
    payload.remove("proofDigest");
    digest(&Value::Object(payload))
}

pub fn validate_predictive_planning_proof(value: &Value) -> Vec<String> {
    let Some(payload) = value.as_object() else {
        return vec!["predictive-proof-must-be-object".to_string()];
    };
    let mut errors = Vec::new();
    if text(payload.get("schema")) != PREDICTIVE_PLANNING_PROOF_SCHEMA {
        errors.push("predictive-proof-schema-mismatch".to_string());
    }
    for field in [
        "sourceSnapshotDigest",
        "authorityHeadDigest",
        "capacityObservationSetDigest",
        "forecastDigest",
        "forecastBacktestDigest",
        "priceCatalogDigest",
        "candidatePlanDigest",
        "freshStateBundleDigest",
        "whatIfDigest",
        "proofDigest",
    ] {
        if !is_sha256(payload.get(field)) {
            errors.push(format!("invalid-sha256:{field}"));
        }
    }
    if payload.get("proofDigest").and_then(Value::as_str)
        != predictive_planning_proof_digest(value).as_deref()
    {
        errors.push("predictive-proof-digest-mismatch".to_string());
    }
    validate_source_and_fresh_state(payload, &mut errors);
    validate_observations_and_forecast(payload, &mut errors);
    validate_backtests(payload, &mut errors);
    validate_catalog_and_plan(payload, &mut errors);
    validate_simulation(payload, &mut errors);
    errors
}

fn validate_source_and_fresh_state(payload: &Map<String, Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let source = object(payload.get("sourceSnapshot"), "source-snapshot", errors).unwrap_or(&empty);
    if source.is_empty() {
        errors.push("source-snapshot-empty".to_string());
    }
    let declared_source = text(payload.get("sourceSnapshotDigest"));
    let computed_source = risk_digest(source);
    if !source.is_empty() && text(source.get("riskDigest")) != computed_source {
        errors.push("source-snapshot-digest-mismatch".to_string());
    }
    if declared_source != computed_source {
        errors.push("source-snapshot-binding-mismatch".to_string());
    }

    let fresh = object(
        payload.get("freshStateBundle"),
        "fresh-state-bundle",
        errors,
    )
    .unwrap_or(&empty);
    let authority = text(payload.get("authorityHeadDigest"));
    for (field, valid) in [
        ("sourceSnapshotDigest", is_sha256_text(&declared_source)),
        ("authorityHeadDigest", is_sha256_text(&authority)),
        (
            "freshStateBundleDigest",
            is_sha256(payload.get("freshStateBundleDigest")),
        ),
    ] {
        if !valid {
            errors.push(format!("invalid-sha256:{field}"));
        }
    }
    if fresh.is_empty() {
        errors.push("fresh-state-bundle-empty".to_string());
        return;
    }

    for (digest_field, component_field) in [
        ("capacitySnapshotDigest", "capacitySnapshot"),
        ("runningEffectsDigest", "runningEffects"),
        ("budgetRevision", "budgets"),
        ("maintenanceDecisionDigest", "maintenanceDecisions"),
        ("blastSimulationDigest", "blastSimulation"),
    ] {
        if text(fresh.get(digest_field)) != digest_or_empty(fresh.get(component_field)) {
            errors.push(format!(
                "fresh-state-component-digest-mismatch:{digest_field}"
            ));
        }
    }
    if text(fresh.get("authorityHeadDigest")) != authority {
        errors.push("fresh-state-authority-binding-mismatch".to_string());
    }
    let authority_state =
        object(fresh.get("authorityState"), "authority-state", errors).unwrap_or(&empty);
    if !authority_state.is_empty() && text(authority_state.get("canonicalDigest")) != authority {
        errors.push("authority-head-state-mismatch".to_string());
    }
    if text(fresh.get("riskDigest")) != declared_source {
        errors.push("fresh-state-risk-binding-mismatch".to_string());
    }
    if fresh.get("riskSnapshot") != Some(&Value::Object(source.clone())) {
        errors.push("fresh-state-risk-snapshot-mismatch".to_string());
    }
    let binding = json!({
        "authorityHeadDigest": fresh.get("authorityHeadDigest").cloned().unwrap_or(Value::Null),
        "riskDigest": fresh.get("riskDigest").cloned().unwrap_or(Value::Null),
        "capacitySnapshotDigest": fresh.get("capacitySnapshotDigest").cloned().unwrap_or(Value::Null),
        "runningEffectsDigest": fresh.get("runningEffectsDigest").cloned().unwrap_or(Value::Null),
        "budgetRevision": fresh.get("budgetRevision").cloned().unwrap_or(Value::Null),
        "maintenanceDecisionDigest": fresh.get("maintenanceDecisionDigest").cloned().unwrap_or(Value::Null),
        "blastSimulationDigest": fresh.get("blastSimulationDigest").cloned().unwrap_or(Value::Null),
        "observedAt": fresh.get("observedAt").cloned().unwrap_or(Value::Null),
    });
    let expected = digest_or_empty(Some(&binding));
    if text(fresh.get("freshStateBundleDigest")) != expected {
        errors.push("fresh-state-bundle-digest-mismatch".to_string());
    }
    if text(payload.get("freshStateBundleDigest")) != expected {
        errors.push("fresh-state-top-level-binding-mismatch".to_string());
    }
}

fn validate_observations_and_forecast(payload: &Map<String, Value>, errors: &mut Vec<String>) {
    let raw_observations = array(
        payload.get("capacityObservations"),
        "capacity-observations",
        errors,
    )
    .unwrap_or(&[]);
    let mut observations = Vec::new();
    for (index, raw) in raw_observations.iter().enumerate() {
        let Some(observation) = object(Some(raw), &format!("capacity-observation-{index}"), errors)
        else {
            continue;
        };
        if observation.is_empty() {
            continue;
        }
        observations.push(observation);
        let mut body = observation.clone();
        body.remove("observationDigest");
        if text(observation.get("observationDigest")) != digest_or_empty(Some(&Value::Object(body)))
        {
            errors.push(format!("observation-digest-mismatch:{index}"));
        }
        if text(observation.get("source")) != "minio-probe" {
            errors.push(format!("observation-not-production-probe:{index}"));
        }
        if matches!(
            text(observation.get("probeSource")).as_str(),
            "" | "caller" | "manual"
        ) {
            errors.push(format!("observation-probe-source-untrusted:{index}"));
        }
    }
    let used_values: BTreeSet<i64> = observations
        .iter()
        .map(|item| integer_or_zero(item.get("usedBytes")))
        .collect();
    if observations.len() < 3 || used_values.len() < 2 {
        errors.push("capacity-observations-did-not-change".to_string());
    }

    let empty = Map::new();
    let record = object(payload.get("forecastRecord"), "forecast-record", errors).unwrap_or(&empty);
    let forecast = object(record.get("forecast"), "forecast", errors).unwrap_or(&empty);
    if record.is_empty() {
        errors.push("forecast-record-empty".to_string());
    } else if forecast.is_empty() {
        errors.push("forecast-empty".to_string());
    }
    if observations.is_empty() || record.is_empty() || forecast.is_empty() {
        return;
    }
    let target_id = text(record.get("targetId"));
    let incarnation = text(record.get("targetIncarnation"));
    let revision = text(record.get("capacityRevision"));
    for (index, observation) in observations.iter().enumerate() {
        if text(observation.get("targetId")) != target_id
            || text(observation.get("targetIncarnation")) != incarnation
            || text(observation.get("capacityRevision")) != revision
        {
            errors.push(format!("observation-series-binding-mismatch:{index}"));
        }
    }
    let expected_set = observation_set_digest(&observations, record);
    for (field, value) in [
        ("payload", payload.get("capacityObservationSetDigest")),
        ("record", record.get("capacityObservationSetDigest")),
        ("forecast", forecast.get("capacityObservationSetDigest")),
    ] {
        if text(value) != expected_set {
            errors.push(format!("capacity-observation-set-digest-mismatch:{field}"));
        }
    }

    let mut forecast_body = forecast.clone();
    forecast_body.remove("forecastDigest");
    let expected_forecast = digest_or_empty(Some(&Value::Object(forecast_body)));
    for (field, value) in [
        ("payload", payload.get("forecastDigest")),
        ("record", record.get("forecastDigest")),
        ("forecast", forecast.get("forecastDigest")),
    ] {
        if text(value) != expected_forecast {
            errors.push(format!("forecast-digest-mismatch:{field}"));
        }
    }
    for field in [
        "targetId",
        "targetIncarnation",
        "capacityRevision",
        "horizonDays",
        "p50FreeBytes",
        "p90FreeBytes",
    ] {
        if record.get(field) != forecast.get(field) {
            errors.push(format!("forecast-record-field-mismatch:{field}"));
        }
    }
    if text(forecast.get("forecastStatus")) != "OK" {
        errors.push("forecast-record-not-ok".to_string());
    }
    if !matches!(text(record.get("status")).as_str(), "ACTIVE" | "DUE") {
        errors.push("forecast-record-not-current".to_string());
    }
    let binding = json!({
        "targetId": record.get("targetId").cloned().unwrap_or(Value::Null),
        "targetIncarnation": record.get("targetIncarnation").cloned().unwrap_or(Value::Null),
        "capacityRevision": record.get("capacityRevision").cloned().unwrap_or(Value::Null),
        "horizonDays": record.get("horizonDays").cloned().unwrap_or(Value::Null),
        "forecastedAt": record.get("forecastedAt").cloned().unwrap_or(Value::Null),
        "evaluationDueAt": record.get("evaluationDueAt").cloned().unwrap_or(Value::Null),
        "forecastDigest": record.get("forecastDigest").cloned().unwrap_or(Value::Null),
        "capacityObservationSetDigest": record.get("capacityObservationSetDigest").cloned().unwrap_or(Value::Null),
    });
    if text(record.get("forecastId")) != format!("forecast:{}", digest_or_empty(Some(&binding))) {
        errors.push("forecast-id-binding-mismatch".to_string());
    }
}

fn validate_backtests(payload: &Map<String, Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let record = object(payload.get("forecastRecord"), "forecast-record", errors).unwrap_or(&empty);
    let forecast = object(record.get("forecast"), "forecast", errors).unwrap_or(&empty);
    let raw_backtests = array(
        payload.get("forecastBacktests"),
        "forecast-backtests",
        errors,
    )
    .unwrap_or(&[]);
    if raw_backtests.is_empty() {
        errors.push("forecast-backtests-empty".to_string());
        return;
    }
    let observation_keys: BTreeSet<String> = payload
        .get("capacityObservations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
        .map(|item| text(item.get("observationKey")))
        .collect();
    let mut backtests = Vec::new();
    for (index, raw) in raw_backtests.iter().enumerate() {
        let Some(backtest) = object(Some(raw), &format!("forecast-backtest-{index}"), errors)
        else {
            continue;
        };
        if backtest.is_empty() {
            continue;
        }
        backtests.push(backtest);
        let p50 = integer(
            backtest.get("predictedP50FreeBytes"),
            &format!("backtest-{index}-p50"),
            errors,
        );
        let p90 = integer(
            backtest.get("predictedP90FreeBytes"),
            &format!("backtest-{index}-p90"),
            errors,
        );
        let actual = integer(
            backtest.get("actualFreeBytes"),
            &format!("backtest-{index}-actual"),
            errors,
        );
        if let (Some(p50), Some(p90), Some(actual)) = (p50, p90, actual) {
            let bias = p50 as f64 - actual as f64;
            let mae = bias.abs();
            let mape = (actual != 0).then(|| mae / (actual as f64).abs());
            let interval_hit = actual >= p90;
            for (field, expected) in [("mae", Some(mae)), ("mape", mape), ("bias", Some(bias))] {
                if !same_number(backtest.get(field), expected) {
                    errors.push(format!("forecast-backtest-metric-mismatch:{index}:{field}"));
                }
            }
            if backtest.get("intervalHit") != Some(&Value::Bool(interval_hit)) {
                errors.push(format!(
                    "forecast-backtest-metric-mismatch:{index}:intervalHit"
                ));
            }
        }
        if text(backtest.get("targetId")) != text(record.get("targetId"))
            || text(backtest.get("targetIncarnation")) != text(record.get("targetIncarnation"))
            || text(backtest.get("capacityRevision")) != text(record.get("capacityRevision"))
        {
            errors.push(format!("forecast-backtest-series-binding-mismatch:{index}"));
        }
        if !observation_keys.contains(&text(backtest.get("actualObservationKey"))) {
            errors.push(format!(
                "forecast-backtest-observation-binding-mismatch:{index}"
            ));
        }
        if !is_sha256(backtest.get("forecastDigest")) {
            errors.push(format!("forecast-backtest-invalid-forecast-digest:{index}"));
        }
    }

    let expected_set = backtest_set_digest(&backtests, record);
    if text(payload.get("forecastBacktestDigest")) != expected_set {
        errors.push("forecast-backtest-digest-mismatch".to_string());
    }
    if backtests.is_empty() || forecast.is_empty() {
        return;
    }
    let count = backtests.len() as f64;
    let mae = round_to(
        backtests
            .iter()
            .filter_map(|item| number(item.get("mae")))
            .sum::<f64>()
            / count,
        3,
    );
    let mape_values: Vec<f64> = backtests
        .iter()
        .filter_map(|item| item.get("mape"))
        .filter(|value| !value.is_null())
        .filter_map(|value| number(Some(value)))
        .collect();
    let mape = (!mape_values.is_empty()).then(|| {
        round_to(
            mape_values.iter().sum::<f64>() / mape_values.len() as f64,
            6,
        )
    });
    let bias = round_to(
        backtests
            .iter()
            .filter_map(|item| number(item.get("bias")))
            .sum::<f64>()
            / count,
        3,
    );
    let coverage = round_to(
        backtests
            .iter()
            .filter(|item| item.get("intervalHit") == Some(&Value::Bool(true)))
            .count() as f64
            / count,
        6,
    );
    let calibration_result = json!({
        "targetId": text(record.get("targetId")),
        "samples": backtests.len(),
        "mae": mae,
        "mape": mape,
        "bias": bias,
        "intervalCoverage": coverage,
        "overoptimistic": bias > 0.0,
    });
    let mut ordered = backtests.clone();
    ordered.sort_by(|left, right| {
        (text(left.get("evaluatedAt")), text(left.get("backtestKey"))).cmp(&(
            text(right.get("evaluatedAt")),
            text(right.get("backtestKey")),
        ))
    });
    let bindings: Vec<Value> = ordered
        .iter()
        .map(|item| {
            json!({
                "backtestKey": text(item.get("backtestKey")),
                "forecastDigest": text(item.get("forecastDigest")),
                "actualObservationKey": text(item.get("actualObservationKey")),
            })
        })
        .collect();
    let mut calibration_digest_body = calibration_result.as_object().cloned().unwrap_or_default();
    calibration_digest_body.insert("backtests".to_string(), Value::Array(bindings));
    let calibration_digest_value = Value::Object(calibration_digest_body);
    let calibration_digest = digest_or_empty(Some(&calibration_digest_value));
    let calibration =
        object(forecast.get("calibration"), "forecast-calibration", errors).unwrap_or(&empty);
    for (field, expected) in [
        ("samples", Some(backtests.len() as f64)),
        ("mae", Some(mae)),
        ("mape", mape),
        ("bias", Some(bias)),
        ("intervalCoverage", Some(coverage)),
    ] {
        if !same_number(calibration.get(field), expected) {
            errors.push(format!("forecast-calibration-mismatch:{field}"));
        }
    }
    if text(calibration.get("calibrationDigest")) != calibration_digest {
        errors.push("forecast-calibration-digest-mismatch".to_string());
    }
}

fn validate_catalog_and_plan(payload: &Map<String, Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let catalog = object(payload.get("priceCatalog"), "price-catalog", errors).unwrap_or(&empty);
    let catalog_version = integer_or_zero(catalog.get("priceCatalogVersion"));
    let targets = catalog
        .get("targets")
        .filter(|value| truthy(value))
        .cloned()
        .unwrap_or_else(|| json!({}));
    let expected_catalog = digest_or_empty(Some(&json!({
        "priceCatalogVersion": catalog_version,
        "targets": targets,
    })));
    if text(catalog.get("priceCatalogDigest")) != expected_catalog {
        errors.push("price-catalog-digest-mismatch:catalog".to_string());
    }
    if text(payload.get("priceCatalogDigest")) != expected_catalog {
        errors.push("price-catalog-digest-mismatch:payload".to_string());
    }

    let plan = object(payload.get("candidatePlan"), "candidate-plan", errors).unwrap_or(&empty);
    let baseline =
        object(plan.get("baseline"), "candidate-plan-baseline", errors).unwrap_or(&empty);
    let selected =
        object(plan.get("selected"), "candidate-plan-selected", errors).unwrap_or(&empty);
    let candidate = object(
        selected.get("candidate"),
        "candidate-plan-candidate",
        errors,
    )
    .unwrap_or(&empty);
    if plan.is_empty() || baseline.is_empty() || selected.is_empty() || candidate.is_empty() {
        return;
    }
    let expected_plan = digest_or_empty(Some(&json!({
        "selected": candidate,
        "baseline": baseline,
        "sourceSnapshotDigest": plan.get("sourceSnapshotDigest").cloned().unwrap_or(Value::Null),
        "authorityHeadDigest": plan.get("authorityHeadDigest").cloned().unwrap_or(Value::Null),
        "forecastDigest": plan.get("forecastDigest").cloned().unwrap_or(Value::Null),
        "priceCatalogDigest": plan.get("priceCatalogDigest").cloned().unwrap_or(Value::Null),
    })));
    if text(plan.get("candidatePlanDigest")) != expected_plan {
        errors.push("candidate-plan-digest-mismatch:plan".to_string());
    }
    if text(payload.get("candidatePlanDigest")) != expected_plan {
        errors.push("candidate-plan-digest-mismatch:payload".to_string());
    }
    for field in [
        "sourceSnapshotDigest",
        "authorityHeadDigest",
        "forecastDigest",
        "priceCatalogDigest",
    ] {
        if text(plan.get(field)) != text(payload.get(field)) {
            errors.push(format!("candidate-plan-input-binding-mismatch:{field}"));
        }
    }
    if text(plan.get("status")) != "OK" || selected.get("accepted") != Some(&Value::Bool(true)) {
        errors.push("candidate-plan-not-accepted".to_string());
    }
    let violations_are_empty = match selected.get("violations") {
        None | Some(Value::Null) => true,
        Some(Value::Array(items)) => items.is_empty(),
        _ => false,
    };
    if !violations_are_empty {
        errors.push("candidate-plan-declared-violations".to_string());
    }

    let candidate_copies = integer(
        candidate.get("committedCopies"),
        "candidate-committed-copies",
        errors,
    );
    let candidate_domains = integer(
        candidate.get("failureDomains"),
        "candidate-failure-domains",
        errors,
    );
    let min_copies = integer(
        baseline.get("minCommittedCopies"),
        "baseline-min-committed-copies",
        errors,
    );
    let min_domains = integer(
        baseline.get("minFailureDomains"),
        "baseline-min-failure-domains",
        errors,
    );
    let baseline_copies = integer(
        baseline.get("committedCopies"),
        "baseline-committed-copies",
        errors,
    );
    let baseline_domains = integer(
        baseline.get("failureDomains"),
        "baseline-failure-domains",
        errors,
    );
    if matches!((candidate_copies, min_copies), (Some(actual), Some(minimum)) if actual < minimum) {
        errors.push("unsafe-plan-min-committed-copies".to_string());
    }
    if matches!((candidate_domains, min_domains), (Some(actual), Some(minimum)) if actual < minimum)
    {
        errors.push("unsafe-plan-min-failure-domains".to_string());
    }
    if matches!((candidate_copies, baseline_copies), (Some(actual), Some(baseline)) if actual < baseline)
    {
        errors.push("unsafe-plan-baseline-committed-copies".to_string());
    }
    if matches!((candidate_domains, baseline_domains), (Some(actual), Some(baseline)) if actual < baseline)
    {
        errors.push("unsafe-plan-baseline-failure-domains".to_string());
    }
    let forecast_free = integer(
        candidate.get("forecastFreeBytes"),
        "candidate-forecast-free-bytes",
        errors,
    );
    let headroom = integer(
        baseline.get("forecastSafetyHeadroomBytes"),
        "baseline-forecast-headroom-bytes",
        errors,
    );
    if matches!((forecast_free, headroom), (Some(free), Some(required)) if free < required) {
        errors.push("unsafe-plan-forecast-headroom".to_string());
    }
    if candidate.get("breaksDrDependency") == Some(&Value::Bool(true)) {
        errors.push("unsafe-plan-breaks-dr-dependency".to_string());
    }
    if candidate.get("mutatesAuthority") == Some(&Value::Bool(true)) {
        errors.push("unsafe-plan-mutates-authority".to_string());
    }
}

fn validate_simulation(payload: &Map<String, Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let simulation = object(payload.get("simulation"), "simulation", errors).unwrap_or(&empty);
    let what_if = object(payload.get("whatIfResult"), "what-if-result", errors).unwrap_or(&empty);
    if simulation.is_empty() || what_if.is_empty() {
        return;
    }
    let mut what_if_body = what_if.clone();
    what_if_body.remove("whatIfDigest");
    let expected_what_if = digest_or_empty(Some(&Value::Object(what_if_body)));
    if text(what_if.get("whatIfDigest")) != expected_what_if {
        errors.push("what-if-digest-mismatch:result".to_string());
    }
    if text(payload.get("whatIfDigest")) != expected_what_if {
        errors.push("what-if-digest-mismatch:payload".to_string());
    }
    if what_if.get("simulation") != Some(&Value::Object(simulation.clone())) {
        errors.push("what-if-simulation-binding-mismatch".to_string());
    }
    if what_if.get("candidatePlan") != payload.get("candidatePlan") {
        errors.push("what-if-candidate-plan-binding-mismatch".to_string());
    }
    for field in [
        "sourceSnapshotDigest",
        "authorityHeadDigest",
        "forecastDigest",
        "priceCatalogDigest",
        "freshStateBundleDigest",
    ] {
        if text(what_if.get(field)) != text(payload.get(field)) {
            errors.push(format!("what-if-input-binding-mismatch:{field}"));
        }
    }

    let before = object(
        simulation.get("preStateDigests"),
        "simulation-pre-state-digests",
        errors,
    )
    .unwrap_or(&empty);
    let after = object(
        simulation.get("postStateDigests"),
        "simulation-post-state-digests",
        errors,
    )
    .unwrap_or(&empty);
    for (label, digests) in [("pre", before), ("post", after)] {
        for domain in STATE_DOMAINS {
            if !is_sha256(digests.get(*domain)) {
                errors.push(format!("simulation-{label}-state-digest-invalid:{domain}"));
            }
        }
    }
    let expected_before = digest_or_empty(Some(&Value::Object(before.clone())));
    let expected_after = digest_or_empty(Some(&Value::Object(after.clone())));
    if text(simulation.get("preStateDigest")) != expected_before {
        errors.push("simulation-pre-state-digest-mismatch".to_string());
    }
    if text(simulation.get("postStateDigest")) != expected_after {
        errors.push("simulation-post-state-digest-mismatch".to_string());
    }
    if before != after
        || expected_before != expected_after
        || simulation.get("storageInventoryBefore") != simulation.get("storageInventoryAfter")
        || simulation.get("stateUnchanged") != Some(&Value::Bool(true))
        || simulation.get("changedDomains") != Some(&Value::Array(Vec::new()))
    {
        errors.push("simulation-state-changed".to_string());
    }

    let attempted = array(
        simulation.get("attemptedWrites"),
        "simulation-attempted-writes",
        errors,
    )
    .unwrap_or(&[]);
    let blocked = array(
        simulation.get("blockedWrites"),
        "simulation-blocked-writes",
        errors,
    )
    .unwrap_or(&[]);
    let attempted_count = integer(
        simulation.get("attemptedMutationCount"),
        "simulation-attempted-mutation-count",
        errors,
    );
    let blocked_count = integer(
        simulation.get("blockedMutationCount"),
        "simulation-blocked-mutation-count",
        errors,
    );
    if attempted_count.is_some_and(|count| count != attempted.len() as i64) {
        errors.push("simulation-attempt-count-mismatch".to_string());
    }
    if blocked_count.is_some_and(|count| count != blocked.len() as i64) {
        errors.push("simulation-blocked-count-mismatch".to_string());
    }
    if !attempted.is_empty() || attempted_count != Some(0) {
        errors.push("simulation-attempted-mutation".to_string());
    }
    if !blocked.is_empty() || blocked_count != Some(0) {
        errors.push("simulation-blocked-mutation".to_string());
    }
    let changed_count = simulation
        .get("changedDomains")
        .and_then(Value::as_array)
        .map_or(1, Vec::len);
    let expected_side_effects = attempted.len() + changed_count;
    let side_effects = integer(
        what_if.get("sideEffectsObserved"),
        "what-if-side-effects-observed",
        errors,
    );
    if side_effects != Some(expected_side_effects as i64) {
        errors.push("what-if-side-effect-count-mismatch".to_string());
    }
    if text(what_if.get("status")) != "OK" {
        errors.push("what-if-result-not-ok".to_string());
    }
}

fn risk_digest(snapshot: &Map<String, Value>) -> String {
    let mut risks: Vec<Value> = snapshot
        .get("risks")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
        .map(|item| {
            let mut evidence: Vec<String> = item
                .get("evidence")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|value| text(Some(value)))
                .collect();
            evidence.sort();
            json!({
                "type": text(item.get("type")),
                "target": text(item.get("target")),
                "policyId": text(item.get("policyId")),
                "severity": text_or(item.get("severity"), "healthy"),
                "confidence": text_or(item.get("confidence"), "verified"),
                "evidence": evidence,
            })
        })
        .collect();
    risks.sort_by(|left, right| {
        let left_key = risk_sort_key(left);
        let right_key = risk_sort_key(right);
        right_key.cmp(&left_key)
    });
    digest_or_empty(Some(&json!({
        "riskSnapshotVersion": text_or(snapshot.get("riskSnapshotVersion"), "risk-snapshot-v1"),
        "overallRisk": text_or(snapshot.get("overallRisk"), "healthy"),
        "risks": risks,
    })))
}

fn risk_sort_key(value: &Value) -> (String, String, String, String) {
    value.as_object().map_or_else(Default::default, |risk| {
        (
            text(risk.get("severity")),
            text(risk.get("type")),
            text(risk.get("target")),
            text(risk.get("policyId")),
        )
    })
}

fn observation_set_digest(
    observations: &[&Map<String, Value>],
    record: &Map<String, Value>,
) -> String {
    let mut ordered = observations.to_vec();
    ordered.sort_by(|left, right| {
        (
            text(left.get("observedAt")),
            text(left.get("observationKey")),
        )
            .cmp(&(
                text(right.get("observedAt")),
                text(right.get("observationKey")),
            ))
    });
    let bindings: Vec<Value> = ordered
        .iter()
        .map(|item| {
            json!({
                "observationKey": text(item.get("observationKey")),
                "observationDigest": text(item.get("observationDigest")),
            })
        })
        .collect();
    digest_or_empty(Some(&json!({
        "targetId": text(record.get("targetId")),
        "targetIncarnation": text(record.get("targetIncarnation")),
        "capacityRevision": text(record.get("capacityRevision")),
        "observations": bindings,
    })))
}

fn backtest_set_digest(backtests: &[&Map<String, Value>], record: &Map<String, Value>) -> String {
    let mut ordered = backtests.to_vec();
    ordered.sort_by(|left, right| {
        (text(left.get("evaluatedAt")), text(left.get("backtestKey"))).cmp(&(
            text(right.get("evaluatedAt")),
            text(right.get("backtestKey")),
        ))
    });
    let values: Vec<Value> = ordered
        .into_iter()
        .map(|item| Value::Object(item.clone()))
        .collect();
    digest_or_empty(Some(&json!({
        "targetId": text(record.get("targetId")),
        "targetIncarnation": text(record.get("targetIncarnation")),
        "capacityRevision": text(record.get("capacityRevision")),
        "backtests": values,
    })))
}

fn object<'a>(
    value: Option<&'a Value>,
    field: &str,
    errors: &mut Vec<String>,
) -> Option<&'a Map<String, Value>> {
    match value.and_then(Value::as_object) {
        Some(value) => Some(value),
        None => {
            errors.push(format!("{field}-must-be-object"));
            None
        }
    }
}

fn array<'a>(
    value: Option<&'a Value>,
    field: &str,
    errors: &mut Vec<String>,
) -> Option<&'a [Value]> {
    match value.and_then(Value::as_array) {
        Some(value) => Some(value),
        None => {
            errors.push(format!("{field}-must-be-list"));
            None
        }
    }
}

fn integer(value: Option<&Value>, field: &str, errors: &mut Vec<String>) -> Option<i64> {
    let parsed = match value {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_u64().and_then(|value| i64::try_from(value).ok()))
            .or_else(|| {
                number.as_f64().and_then(|value| {
                    (value.is_finite() && value >= i64::MIN as f64 && value <= i64::MAX as f64)
                        .then_some(value as i64)
                })
            }),
        Some(Value::String(value)) => value.parse::<i64>().ok(),
        Some(Value::Bool(_)) | None | Some(Value::Null | Value::Array(_) | Value::Object(_)) => {
            None
        }
    };
    if parsed.is_none() {
        errors.push(format!("{field}-must-be-integer"));
    }
    parsed
}

fn integer_or_zero(value: Option<&Value>) -> i64 {
    let mut ignored = Vec::new();
    integer(value.filter(|value| truthy(value)), "ignored", &mut ignored).unwrap_or(0)
}

fn number(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Number(value)) => value.as_f64().filter(|value| value.is_finite()),
        _ => None,
    }
}

fn same_number(actual: Option<&Value>, expected: Option<f64>) -> bool {
    match expected {
        None => actual.is_none_or(Value::is_null),
        Some(expected) => number(actual).is_some_and(|actual| {
            (actual - expected).abs() <= 1e-9_f64.max(1e-9 * actual.abs().max(expected.abs()))
        }),
    }
}

fn round_to(value: f64, places: u32) -> f64 {
    let factor = 10_f64.powi(places as i32);
    (value * factor).round() / factor
}

fn digest_or_empty(value: Option<&Value>) -> String {
    digest(value.unwrap_or(&Value::Null)).unwrap_or_default()
}

fn digest(value: &Value) -> Option<String> {
    let mut canonical = value.clone();
    canonical.sort_all_objects();
    let bytes = serde_json::to_vec(&canonical).ok()?;
    let hash = Sha256::digest(bytes);
    let mut encoded = String::with_capacity(SHA256_LENGTH);
    for byte in hash {
        let _ = write!(encoded, "{byte:02x}");
    }
    Some(encoded)
}

fn is_sha256(value: Option<&Value>) -> bool {
    is_sha256_text(&text(value))
}

fn is_sha256_text(value: &str) -> bool {
    value.len() == SHA256_LENGTH
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn text_or(value: Option<&Value>, fallback: &str) -> String {
    let value = text(value);
    if value.is_empty() {
        fallback.to_string()
    } else {
        value
    }
}

fn text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null | Value::Bool(false)) => String::new(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::String(value)) => value.clone(),
        Some(Value::Number(value)) => {
            if value.as_f64() == Some(0.0) {
                String::new()
            } else {
                value.to_string()
            }
        }
        Some(value @ (Value::Array(_) | Value::Object(_))) => {
            if truthy(value) {
                serde_json::to_string(value).unwrap_or_default()
            } else {
                String::new()
            }
        }
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null | Value::Bool(false) => false,
        Value::Bool(true) => true,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_numeric_and_text_coercions_are_bounded() {
        let mut errors = Vec::new();
        assert_eq!(integer(Some(&json!("7")), "value", &mut errors), Some(7));
        assert!(errors.is_empty());
        assert_eq!(integer(Some(&json!(true)), "value", &mut errors), None);
        assert_eq!(errors, ["value-must-be-integer"]);
        assert!(same_number(Some(&json!(1.0000000001)), Some(1.0)));
        assert!(!same_number(Some(&json!(true)), Some(1.0)));
    }

    #[test]
    fn digests_sort_nested_objects() {
        assert_eq!(
            digest(&json!({"b": 2, "a": {"d": 4, "c": 3}})),
            digest(&json!({"a": {"c": 3, "d": 4}, "b": 2}))
        );
    }
}
