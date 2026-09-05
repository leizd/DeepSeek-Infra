//! Read-only compatibility checks for the 30 unpromoted planning Evidence claims.
//! Presence-only legacy checks and the SLO strict-subset quirk are preserved here,
//! never substituted for typed predictive proofs or native control authorization.

use crate::legacy_values::{
    PythonInteger, is_plain_sha256, python_equal, python_integer, require_fields,
    value_or_empty_text,
};
use serde_json::{Value, json};
use std::collections::HashSet;

pub const LEGACY_PLANNING_PROOF_CHECKS: &[&str] = &[
    "absentAuthoritativeRiskIsClosedOrRetired",
    "supersededBackupRiskCannotRemainOpenForever",
    "policyDisabledRiskIsRetired",
    "unknownCoverageDoesNotImplicitlyClearRisk",
    "schedulerReservationDoesNotCountAsConsumedService",
    "preemptedActionReleasesFairnessReservation",
    "completedActionChargesObservedBytesExactlyOnce",
    "serviceConsumptionSurvivesRestart",
    "waveOneCannotStartBeforeWaveZeroVerified",
    "failedWavePausesDownstreamActions",
    "staleWaveRequiresReplan",
    "waveRevalidatesFreshRiskBeforeExecution",
    "waveRevalidatesAuthorityBeforeExecution",
    "waveRevalidatesBlastRadiusBeforeExecution",
    "fleetSloExposes1h24h7d30dWindows",
    "insufficientSloSamplesAreExplicit",
    "forecastWithInsufficientSamplesFailsClosed",
    "thirtyDayCapacityForecastProduced",
    "ninetyDayCapacityForecastProduced",
    "forecastProvidesP50AndP90Headroom",
    "overoptimisticForecastLowersConfidence",
    "unknownTargetPriceDoesNotBecomeZero",
    "egressCostIsIncluded",
    "storageCostIsIncluded",
    "optimizerRejectsUnsafeCheaperPlan",
    "candidatePlanIsDeterministicForSameInputs",
    "federationSnapshotContainsNoCredentials",
    "federationSnapshotIsDigestBound",
    "incompatibleFleetWireVersionFailsClosed",
    "federatedSimulationCannotMutateRemoteFleet",
];

fn required_fields(check_name: &str) -> Option<&'static [&'static str]> {
    Some(match check_name {
        "absentAuthoritativeRiskIsClosedOrRetired" => {
            &["status", "closureReason", "coverageComplete"]
        }
        "supersededBackupRiskCannotRemainOpenForever" => {
            &["status", "closureReason", "previousBackupId"]
        }
        "policyDisabledRiskIsRetired" => &["status", "closureReason"],
        "unknownCoverageDoesNotImplicitlyClearRisk" => &["status", "closureReason"],
        "schedulerReservationDoesNotCountAsConsumedService" => {
            &["reservationStatus", "actionsServed"]
        }
        "preemptedActionReleasesFairnessReservation" => &["reservationStatus", "releaseReason"],
        "completedActionChargesObservedBytesExactlyOnce" => &["actualBytes", "actionsServed"],
        "serviceConsumptionSurvivesRestart" => &["actionsServed", "virtualRuntime"],
        "waveOneCannotStartBeforeWaveZeroVerified" => &["admitted", "reason"],
        "failedWavePausesDownstreamActions" => &["scheduleStatus", "admitted"],
        "staleWaveRequiresReplan" => &["scheduleStatus", "reason"],
        "waveRevalidatesFreshRiskBeforeExecution" => &["revalidatedRisk"],
        "waveRevalidatesAuthorityBeforeExecution" => &["revalidatedAuthority"],
        "waveRevalidatesBlastRadiusBeforeExecution" => &["revalidatedBlastRadius"],
        "fleetSloExposes1h24h7d30dWindows" => &["windows"],
        "insufficientSloSamplesAreExplicit" => &["status"],
        "forecastWithInsufficientSamplesFailsClosed" => &["forecastStatus"],
        "thirtyDayCapacityForecastProduced" => &["horizonDays", "forecastStatus"],
        "ninetyDayCapacityForecastProduced" => &["horizonDays", "forecastStatus"],
        "forecastProvidesP50AndP90Headroom" => &["p50FreeBytes", "p90FreeBytes"],
        "overoptimisticForecastLowersConfidence" => &["overoptimistic", "confidence"],
        "unknownTargetPriceDoesNotBecomeZero" => &["status"],
        "egressCostIsIncluded" => &["egress"],
        "storageCostIsIncluded" => &["storage"],
        "optimizerRejectsUnsafeCheaperPlan" => &["accepted", "violations"],
        "candidatePlanIsDeterministicForSameInputs" => &["candidatePlanDigest", "repeatDigest"],
        "federationSnapshotContainsNoCredentials" => &["forbiddenKeys"],
        "federationSnapshotIsDigestBound" => &["snapshotDigest"],
        "incompatibleFleetWireVersionFailsClosed" => &["status"],
        "federatedSimulationCannotMutateRemoteFleet" => &["remoteMutations"],
        _ => return None,
    })
}

pub fn validate_legacy_planning_check(check_name: &str, evidence: &Value) -> Vec<String> {
    let Some(fields) = required_fields(check_name) else {
        return vec![format!("unsupported-check:{check_name}")];
    };
    let Some(evidence) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = require_fields(evidence, fields);
    let text = |field| value_or_empty_text(evidence.get(field));
    match check_name {
        "absentAuthoritativeRiskIsClosedOrRetired"
        | "supersededBackupRiskCannotRemainOpenForever"
        | "policyDisabledRiskIsRetired" => {
            if matches!(
                text("status").as_str(),
                "OPEN" | "REOPENED" | "UNKNOWN_COVERAGE"
            ) {
                errors.push("risk-subject-still-open".to_string());
            }
            if evidence.get("coverageComplete") == Some(&Value::Bool(false)) {
                errors.push("complete-coverage-required-to-close".to_string());
            }
        }
        "unknownCoverageDoesNotImplicitlyClearRisk" => {
            if matches!(
                text("status").as_str(),
                "CLEARED" | "SUPERSEDED" | "RETIRED"
            ) {
                errors.push("incomplete-coverage-implicitly-cleared-risk".to_string());
            }
            if text("closureReason") != "UNKNOWN_COVERAGE" {
                errors.push("unknown-coverage-reason-missing".to_string());
            }
        }
        "schedulerReservationDoesNotCountAsConsumedService" => {
            if text("reservationStatus") != "RESERVED" {
                errors.push("schedule-did-not-reserve".to_string());
            }
            require_integer(
                evidence.get("actionsServed"),
                0,
                "reservation-counted-as-consumed",
                "invalid-actions-served",
                &mut errors,
            );
        }
        "preemptedActionReleasesFairnessReservation" => {
            if text("reservationStatus") != "RELEASED" {
                errors.push("preempted-reservation-not-released".to_string());
            }
            if text("releaseReason") != "PREEMPTED" {
                errors.push("preempted-release-reason-mismatch".to_string());
            }
        }
        "completedActionChargesObservedBytesExactlyOnce" => {
            require_integer(
                evidence.get("actionsServed"),
                1,
                "consumed-service-not-charged-once",
                "invalid-actions-served",
                &mut errors,
            );
        }
        "waveOneCannotStartBeforeWaveZeroVerified" => {
            if evidence.get("admitted") == Some(&Value::Bool(true)) {
                errors.push("wave-one-started-before-wave-zero-verified".to_string());
            }
            if text("reason") != "PREDECESSOR_WAVE_NOT_VERIFIED" {
                errors.push("missing-predecessor-gate".to_string());
            }
        }
        "staleWaveRequiresReplan" => {
            if text("scheduleStatus") != "PAUSED_REPLAN" {
                errors.push("stale-wave-did-not-pause-replan".to_string());
            }
        }
        "fleetSloExposes1h24h7d30dWindows" => {
            if incomplete_slo_windows(evidence.get("windows")) {
                errors.push("slo-windows-incomplete".to_string());
            }
        }
        "insufficientSloSamplesAreExplicit" => {
            if text("status") != "INSUFFICIENT_DATA" {
                errors.push("insufficient-slo-not-explicit".to_string());
            }
        }
        "forecastWithInsufficientSamplesFailsClosed" => {
            if text("forecastStatus") != "INSUFFICIENT_DATA" {
                errors.push("insufficient-forecast-not-fail-closed".to_string());
            }
        }
        "thirtyDayCapacityForecastProduced" | "ninetyDayCapacityForecastProduced" => {
            let expected = if check_name.starts_with("thirty") {
                30
            } else {
                90
            };
            require_integer(
                evidence.get("horizonDays"),
                expected,
                "forecast-horizon-mismatch",
                "invalid-forecast-horizon",
                &mut errors,
            );
        }
        "unknownTargetPriceDoesNotBecomeZero" => {
            if text("status") != "UNKNOWN_COST" {
                errors.push("unknown-price-was-not-unknown-cost".to_string());
            }
            let cost = evidence.get("monthlyCost").unwrap_or(&Value::Null);
            if matches!(cost, Value::Array(_) | Value::Object(_)) {
                // The Python set-membership expression raises TypeError for containers.
                // A marked exception vector requires native rejection, never acceptance.
                errors.push("invalid-monthly-cost-type".to_string());
            } else if python_equal(cost, &json!(0)) {
                errors.push("unknown-price-defaulted-to-zero".to_string());
            }
        }
        "optimizerRejectsUnsafeCheaperPlan" => {
            if evidence.get("accepted") == Some(&Value::Bool(true)) {
                errors.push("unsafe-candidate-was-accepted".to_string());
            }
            if evidence
                .get("violations")
                .and_then(Value::as_array)
                .is_none_or(Vec::is_empty)
            {
                errors.push("durability-violation-not-recorded".to_string());
            }
        }
        "candidatePlanIsDeterministicForSameInputs" => {
            if text("candidatePlanDigest") != text("repeatDigest") {
                errors.push("candidate-plan-not-deterministic".to_string());
            }
            if !is_plain_sha256(evidence.get("candidatePlanDigest")) {
                errors.push("invalid-sha256:candidatePlanDigest".to_string());
            }
        }
        "federationSnapshotContainsNoCredentials" => {
            if !evidence
                .get("forbiddenKeys")
                .and_then(Value::as_array)
                .is_some_and(Vec::is_empty)
            {
                errors.push("federation-snapshot-contains-credentials".to_string());
            }
        }
        "federationSnapshotIsDigestBound" => {
            if !is_plain_sha256(evidence.get("snapshotDigest")) {
                errors.push("invalid-sha256:snapshotDigest".to_string());
            }
        }
        "incompatibleFleetWireVersionFailsClosed" => {
            if text("status") != "INCOMPATIBLE" {
                errors.push("incompatible-wire-did-not-fail-closed".to_string());
            }
        }
        "federatedSimulationCannotMutateRemoteFleet" => {
            require_integer(
                evidence.get("remoteMutations"),
                0,
                "federated-simulation-mutated-remote",
                "invalid-remote-mutation-count",
                &mut errors,
            );
        }
        // These registered legacy claims only require non-missing fields. Production
        // safety still needs its dedicated native decision and typed proof validators.
        _ => {}
    }
    errors
}

fn require_integer(
    value: Option<&Value>,
    expected: usize,
    mismatch: &str,
    invalid: &str,
    errors: &mut Vec<String>,
) {
    match python_integer(value) {
        Some(actual) if actual != PythonInteger::from_usize(expected) => {
            errors.push(mismatch.to_string())
        }
        Some(_) => {}
        None => errors.push(invalid.to_string()),
    }
}

fn incomplete_slo_windows(value: Option<&Value>) -> bool {
    let Some(windows) = value.and_then(Value::as_array) else {
        return true;
    };
    if windows
        .iter()
        .any(|value| matches!(value, Value::Array(_) | Value::Object(_)))
    {
        // The reference raises TypeError while building its set. Reject deterministically.
        return true;
    }
    const EXPECTED: &[&str] = &["1h", "24h", "7d", "30d", "lifetime"];
    // Python uses a *proper subset* test, not a missing-required-elements test:
    // an incomparable set such as ["unexpected"] therefore passes the legacy check.
    let only_expected = windows
        .iter()
        .all(|value| value.as_str().is_some_and(|name| EXPECTED.contains(&name)));
    let unique: HashSet<_> = windows.iter().filter_map(Value::as_str).collect();
    only_expected && unique.len() < EXPECTED.len()
}
