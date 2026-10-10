//! Read-only replay of Wave Runner heartbeat and multi-epoch crash takeover Evidence.
//! These observations do not authorize a wave, settle an effect, or prove a provider mutation.

use crate::crash_recovery::validate_crash_recovery_proof;
use crate::legacy_values::{
    ParsedTimestamp, PythonInteger, parse_timestamp, python_integer, require_fields,
    value_or_empty_text,
};
use serde_json::{Map, Value};
use std::cmp::Ordering;

pub const WAVE_CRASH_PROOF_CHECKS: &[&str] = &[
    "longRunningWaveRenewsScheduleLease",
    "longRunningWaveRenewsWaveLease",
    "realProcessWaveSigkillTakeoverUsesHigherEpoch",
    "realProcessWaveSigkillDoesNotDuplicateEffect",
    "realProcessWaveSigkillSettlesExactlyOnce",
];

const WAVE_FIELDS: &[&str] = &[
    "scheduleId",
    "waveIndex",
    "scheduleEpochA",
    "scheduleEpochB",
    "waveEpochA",
    "waveEpochB",
    "waveActionEpochA",
    "waveActionEpochB",
    "workerAScheduleLeaseUntil",
    "workerAWaveLeaseUntil",
    "workerAWaveActionLeaseUntil",
    "firstRunnerLeaseUntil",
    "renewedRunnerLeaseUntil",
    "runnerLeaseObservations",
    "journalStateAtCrash",
    "runnerStateAtCrash",
    "runnerStateAtTakeoverClaim",
    "runnerStateAfterTakeover",
    "settlementEvents",
];

pub fn validate_wave_crash_recovery_proof(evidence: &Value) -> Vec<String> {
    let mut errors = validate_crash_recovery_proof(evidence);
    let Some(evidence) = evidence.as_object() else {
        return errors;
    };
    errors.extend(require_fields(evidence, WAVE_FIELDS));
    let [
        Some(schedule_epoch_a),
        Some(schedule_epoch_b),
        Some(wave_epoch_a),
        Some(wave_epoch_b),
        Some(wave_action_epoch_a),
        Some(wave_action_epoch_b),
        Some(journal_epoch_a),
        Some(journal_epoch_b),
        Some(wave_index),
        Some(worker_a_pid),
        Some(worker_b_pid),
    ] = [
        "scheduleEpochA",
        "scheduleEpochB",
        "waveEpochA",
        "waveEpochB",
        "waveActionEpochA",
        "waveActionEpochB",
        "epochA",
        "epochB",
        "waveIndex",
        "workerAPid",
        "workerBPid",
    ]
    .map(|field| python_integer(evidence.get(field)))
    else {
        errors.push("invalid-wave-takeover-numeric-fields".to_string());
        return errors;
    };
    if wave_index.is_negative() {
        errors.push("negative-wave-index".to_string());
    }
    if schedule_epoch_b.compare(&schedule_epoch_a) != Ordering::Greater {
        errors.push("schedule-execution-epoch-not-increased".to_string());
    }
    if wave_epoch_b.compare(&wave_epoch_a) != Ordering::Greater {
        errors.push("wave-execution-epoch-not-increased".to_string());
    }
    if wave_action_epoch_b.compare(&wave_action_epoch_a) != Ordering::Greater {
        errors.push("wave-action-execution-epoch-not-increased".to_string());
    }

    let raw_crash_state = evidence
        .get("runnerStateAtCrash")
        .and_then(Value::as_object);
    let raw_claim_state = evidence
        .get("runnerStateAtTakeoverClaim")
        .and_then(Value::as_object);
    let raw_takeover_state = evidence
        .get("runnerStateAfterTakeover")
        .and_then(Value::as_object);
    if raw_crash_state.is_none() {
        errors.push("runner-state-at-crash-must-be-object".to_string());
    }
    if raw_claim_state.is_none() {
        errors.push("runner-state-at-takeover-claim-must-be-object".to_string());
    }
    if raw_takeover_state.is_none() {
        errors.push("runner-state-after-takeover-must-be-object".to_string());
    }
    let empty = Map::new();
    let crash_state = raw_crash_state.unwrap_or(&empty);
    let claim_state = raw_claim_state.unwrap_or(&empty);
    let takeover_state = raw_takeover_state.unwrap_or(&empty);

    let crash_schedule = runner_record(crash_state, "crash", "schedule", &mut errors);
    let crash_wave = runner_record(crash_state, "crash", "wave", &mut errors);
    let crash_action = runner_record(crash_state, "crash", "waveAction", &mut errors);
    let claim_schedule = runner_record(claim_state, "takeover-claim", "schedule", &mut errors);
    let claim_wave = runner_record(claim_state, "takeover-claim", "wave", &mut errors);
    let claim_action = runner_record(claim_state, "takeover-claim", "waveAction", &mut errors);
    let takeover_schedule = runner_record(takeover_state, "takeover", "schedule", &mut errors);
    let takeover_wave = runner_record(takeover_state, "takeover", "wave", &mut errors);
    let takeover_action = runner_record(takeover_state, "takeover", "waveAction", &mut errors);
    let schedule_id = value_or_empty_text(evidence.get("scheduleId"));
    let action_id = value_or_empty_text(evidence.get("actionId"));
    for record in [
        &crash_schedule,
        &crash_wave,
        &crash_action,
        &claim_schedule,
        &claim_wave,
        &claim_action,
        &takeover_schedule,
        &takeover_wave,
        &takeover_action,
    ] {
        if value_or_empty_text(record.get("scheduleId")) != schedule_id {
            errors.push("runner-state-schedule-id-binding-mismatch".to_string());
        }
    }
    for record in [&crash_action, &claim_action, &takeover_action] {
        if value_or_empty_text(record.get("actionId")) != action_id {
            errors.push("runner-state-action-id-binding-mismatch".to_string());
        }
    }
    for record in [
        &crash_wave,
        &crash_action,
        &claim_wave,
        &claim_action,
        &takeover_wave,
        &takeover_action,
    ] {
        let Some(record_wave_index) = python_integer(record.get("waveIndex")) else {
            errors.push("runner-state-invalid-wave-index".to_string());
            continue;
        };
        if record_wave_index.compare(&wave_index) != Ordering::Equal {
            errors.push("runner-state-wave-index-binding-mismatch".to_string());
        }
    }

    let crash_schedule_epoch = runner_epoch(
        &crash_schedule,
        "crash-schedule",
        "scheduleExecutionEpoch",
        &mut errors,
    );
    let crash_wave_epoch =
        runner_epoch(&crash_wave, "crash-wave", "waveExecutionEpoch", &mut errors);
    let crash_action_epoch = runner_epoch(
        &crash_action,
        "crash-action",
        "actionExecutionEpoch",
        &mut errors,
    );
    let crash_action_schedule_epoch = runner_epoch(
        &crash_action,
        "crash-action",
        "scheduleExecutionEpoch",
        &mut errors,
    );
    let crash_action_wave_epoch = runner_epoch(
        &crash_action,
        "crash-action",
        "waveExecutionEpoch",
        &mut errors,
    );
    let claim_schedule_epoch = runner_epoch(
        &claim_schedule,
        "takeover-claim-schedule",
        "scheduleExecutionEpoch",
        &mut errors,
    );
    let claim_wave_epoch = runner_epoch(
        &claim_wave,
        "takeover-claim-wave",
        "waveExecutionEpoch",
        &mut errors,
    );
    let claim_action_epoch = runner_epoch(
        &claim_action,
        "takeover-claim-action",
        "actionExecutionEpoch",
        &mut errors,
    );
    let claim_action_schedule_epoch = runner_epoch(
        &claim_action,
        "takeover-claim-action",
        "scheduleExecutionEpoch",
        &mut errors,
    );
    let claim_action_wave_epoch = runner_epoch(
        &claim_action,
        "takeover-claim-action",
        "waveExecutionEpoch",
        &mut errors,
    );
    let takeover_schedule_epoch = runner_epoch(
        &takeover_schedule,
        "takeover-schedule",
        "scheduleExecutionEpoch",
        &mut errors,
    );
    let takeover_wave_epoch = runner_epoch(
        &takeover_wave,
        "takeover-wave",
        "waveExecutionEpoch",
        &mut errors,
    );
    let takeover_action_epoch = runner_epoch(
        &takeover_action,
        "takeover-action",
        "actionExecutionEpoch",
        &mut errors,
    );
    let takeover_action_schedule_epoch = runner_epoch(
        &takeover_action,
        "takeover-action",
        "scheduleExecutionEpoch",
        &mut errors,
    );
    let takeover_action_wave_epoch = runner_epoch(
        &takeover_action,
        "takeover-action",
        "waveExecutionEpoch",
        &mut errors,
    );
    let takeover_journal_epoch = runner_epoch(
        &takeover_action,
        "takeover-action",
        "journalExecutionEpoch",
        &mut errors,
    );
    if !same_int(crash_schedule_epoch.as_ref(), &schedule_epoch_a) {
        errors.push("runner-state-schedule-epoch-binding-mismatch".to_string());
    }
    if !same_int(crash_wave_epoch.as_ref(), &wave_epoch_a) {
        errors.push("runner-state-wave-epoch-binding-mismatch".to_string());
    }
    if !same_int(crash_action_epoch.as_ref(), &wave_action_epoch_a)
        || !same_int(crash_action_schedule_epoch.as_ref(), &schedule_epoch_a)
        || !same_int(crash_action_wave_epoch.as_ref(), &wave_epoch_a)
    {
        errors.push("runner-state-wave-action-epoch-binding-mismatch".to_string());
    }
    if !same_int(claim_schedule_epoch.as_ref(), &schedule_epoch_b) {
        errors.push("runner-state-takeover-claim-schedule-epoch-binding-mismatch".to_string());
    }
    if !same_int(claim_wave_epoch.as_ref(), &wave_epoch_b) {
        errors.push("runner-state-takeover-claim-wave-epoch-binding-mismatch".to_string());
    }
    if !same_int(claim_action_epoch.as_ref(), &wave_action_epoch_b)
        || !same_int(claim_action_schedule_epoch.as_ref(), &schedule_epoch_b)
        || !same_int(claim_action_wave_epoch.as_ref(), &wave_epoch_b)
    {
        errors.push("runner-state-takeover-claim-action-epoch-binding-mismatch".to_string());
    }
    if !same_int(takeover_schedule_epoch.as_ref(), &schedule_epoch_b) {
        errors.push("runner-state-takeover-schedule-epoch-binding-mismatch".to_string());
    }
    if !same_int(takeover_wave_epoch.as_ref(), &wave_epoch_b) {
        errors.push("runner-state-takeover-wave-epoch-binding-mismatch".to_string());
    }
    if !same_int(takeover_action_epoch.as_ref(), &wave_action_epoch_b)
        || !same_int(takeover_action_schedule_epoch.as_ref(), &schedule_epoch_b)
        || !same_int(takeover_action_wave_epoch.as_ref(), &wave_epoch_b)
        || !same_int(takeover_journal_epoch.as_ref(), &journal_epoch_b)
    {
        errors.push("runner-state-takeover-action-epoch-binding-mismatch".to_string());
    }
    if value_or_empty_text(crash_schedule.get("leaseUntil"))
        != value_or_empty_text(evidence.get("workerAScheduleLeaseUntil"))
    {
        errors.push("runner-state-schedule-lease-binding-mismatch".to_string());
    }
    if value_or_empty_text(crash_wave.get("leaseUntil"))
        != value_or_empty_text(evidence.get("workerAWaveLeaseUntil"))
    {
        errors.push("runner-state-wave-lease-binding-mismatch".to_string());
    }
    if value_or_empty_text(crash_action.get("leaseUntil"))
        != value_or_empty_text(evidence.get("workerAWaveActionLeaseUntil"))
    {
        errors.push("runner-state-wave-action-lease-binding-mismatch".to_string());
    }
    let expected_worker_a_owner = format!("crash-worker-a-{}", worker_a_pid.canonical_text());
    if [&crash_schedule, &crash_wave, &crash_action]
        .iter()
        .any(|record| value_or_empty_text(record.get("ownerInstanceId")) != expected_worker_a_owner)
    {
        errors.push("runner-state-worker-a-owner-binding-mismatch".to_string());
    }
    let expected_worker_b_owner = format!("takeover-worker-b-{}", worker_b_pid.canonical_text());
    if [&claim_schedule, &claim_wave, &claim_action]
        .iter()
        .any(|record| value_or_empty_text(record.get("ownerInstanceId")) != expected_worker_b_owner)
    {
        errors.push("runner-state-worker-b-owner-binding-mismatch".to_string());
    }
    if value_or_empty_text(crash_schedule.get("status")) != "RUNNING"
        || value_or_empty_text(crash_wave.get("status")) != "EXECUTING"
    {
        errors.push("runner-state-crash-not-active".to_string());
    }
    if value_or_empty_text(crash_action.get("status")) != "EXECUTING" {
        errors.push("runner-state-crash-action-not-executing".to_string());
    }
    if value_or_empty_text(claim_schedule.get("status")) != "RUNNING"
        || value_or_empty_text(claim_wave.get("status")) != "EXECUTING"
    {
        errors.push("runner-state-takeover-claim-not-active".to_string());
    }
    if value_or_empty_text(claim_action.get("status")) != "CLAIMED" {
        errors.push("runner-state-takeover-claim-action-not-claimed".to_string());
    }
    if value_or_empty_text(takeover_schedule.get("status")) != "COMPLETED"
        || value_or_empty_text(takeover_wave.get("status")) != "COMPLETED"
    {
        errors.push("runner-state-takeover-not-completed".to_string());
    }
    if value_or_empty_text(takeover_action.get("status")) != "VERIFIED_SUCCESS" {
        errors.push("runner-state-takeover-action-not-verified".to_string());
    }
    let takeover_handle = takeover_action
        .get("effectHandle")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if value_or_empty_text(takeover_handle.get("kind")) != "repair"
        || value_or_empty_text(takeover_handle.get("repairId"))
            != value_or_empty_text(evidence.get("repairId"))
    {
        errors.push("runner-state-takeover-effect-binding-mismatch".to_string());
    }

    let mut parsed_lease_times: [Option<i64>; 6] = [None; 6];
    const LEASE_FIELDS: [&str; 6] = [
        "workerALeaseUntil",
        "workerAScheduleLeaseUntil",
        "workerAWaveLeaseUntil",
        "workerAWaveActionLeaseUntil",
        "firstRunnerLeaseUntil",
        "renewedRunnerLeaseUntil",
    ];
    for (index, field) in LEASE_FIELDS.iter().enumerate() {
        match parse_timestamp(&value_or_empty_text(evidence.get(*field))) {
            ParsedTimestamp::Aware(instant) => parsed_lease_times[index] = Some(instant),
            _ => errors.push(format!("invalid-{field}")),
        }
    }
    let first_lease = parsed_lease_times[4];
    let renewed_lease = parsed_lease_times[5];
    let schedule_lease = parsed_lease_times[1];
    let wave_lease = parsed_lease_times[2];
    if let (Some(first), Some(renewed)) = (first_lease, renewed_lease) {
        if renewed <= first {
            errors.push("runner-lease-not-renewed".to_string());
        }
    }
    if let (Some(schedule), Some(wave)) = (schedule_lease, wave_lease) {
        if schedule != wave {
            errors.push("schedule-wave-lease-diverged".to_string());
        }
    }
    if let (Some(schedule), Some(renewed)) = (schedule_lease, renewed_lease) {
        if schedule != renewed {
            errors.push("crashed-runner-lease-not-last-observed-renewal".to_string());
        }
    }

    let mut observation_leases = Vec::new();
    let mut observation_lease_values = Vec::new();
    let mut observation_update_times = Vec::new();
    match evidence
        .get("runnerLeaseObservations")
        .and_then(Value::as_array)
    {
        None => errors.push("runner-lease-observations-must-be-list".to_string()),
        Some(raw_observations) => {
            for (index, raw_observation) in raw_observations.iter().enumerate() {
                let Some(raw_observation) = raw_observation.as_object() else {
                    errors.push("runner-lease-observation-not-object".to_string());
                    continue;
                };
                let raw_schedule = raw_observation.get("schedule").and_then(Value::as_object);
                let raw_wave = raw_observation.get("wave").and_then(Value::as_object);
                let (Some(raw_schedule), Some(raw_wave)) = (raw_schedule, raw_wave) else {
                    errors.push("runner-lease-observation-records-must-be-objects".to_string());
                    continue;
                };
                if value_or_empty_text(raw_schedule.get("scheduleId")) != schedule_id
                    || value_or_empty_text(raw_wave.get("scheduleId")) != schedule_id
                {
                    errors
                        .push("runner-lease-observation-schedule-id-binding-mismatch".to_string());
                }
                let (
                    Some(observation_wave_index),
                    Some(observation_schedule_epoch),
                    Some(observation_wave_epoch),
                ) = (
                    python_integer(raw_wave.get("waveIndex")),
                    python_integer(raw_schedule.get("scheduleExecutionEpoch")),
                    python_integer(raw_wave.get("waveExecutionEpoch")),
                )
                else {
                    errors.push("runner-lease-observation-invalid-numeric-fields".to_string());
                    continue;
                };
                if observation_wave_index.compare(&wave_index) != Ordering::Equal {
                    errors.push("runner-lease-observation-wave-index-binding-mismatch".to_string());
                }
                if observation_schedule_epoch.compare(&schedule_epoch_a) != Ordering::Equal
                    || observation_wave_epoch.compare(&wave_epoch_a) != Ordering::Equal
                {
                    errors.push("runner-lease-observation-epoch-binding-mismatch".to_string());
                }
                if value_or_empty_text(raw_schedule.get("ownerInstanceId"))
                    != expected_worker_a_owner
                    || value_or_empty_text(raw_wave.get("ownerInstanceId"))
                        != expected_worker_a_owner
                {
                    errors.push(
                        "runner-lease-observation-worker-a-owner-binding-mismatch".to_string(),
                    );
                }
                let wave_status = value_or_empty_text(raw_wave.get("status"));
                if value_or_empty_text(raw_schedule.get("status")) != "RUNNING"
                    || !matches!(
                        wave_status.as_str(),
                        "CLAIMING" | "RUNNING" | "EXECUTING" | "VERIFYING"
                    )
                {
                    errors.push("runner-lease-observation-not-active".to_string());
                }
                let schedule_observed_lease = parse_record_timestamp(
                    raw_schedule,
                    "leaseUntil",
                    &format!("runner-lease-observation-{index}-invalid-schedule-lease"),
                    &mut errors,
                );
                let wave_observed_lease = parse_record_timestamp(
                    raw_wave,
                    "leaseUntil",
                    &format!("runner-lease-observation-{index}-invalid-wave-lease"),
                    &mut errors,
                );
                let schedule_updated_at = parse_record_timestamp(
                    raw_schedule,
                    "updatedAt",
                    &format!("runner-lease-observation-{index}-invalid-schedule-updated-at"),
                    &mut errors,
                );
                let wave_updated_at = parse_record_timestamp(
                    raw_wave,
                    "updatedAt",
                    &format!("runner-lease-observation-{index}-invalid-wave-updated-at"),
                    &mut errors,
                );
                if let (Some(schedule_observed), Some(wave_observed)) =
                    (schedule_observed_lease, wave_observed_lease)
                {
                    if schedule_observed != wave_observed {
                        errors.push(
                            "runner-lease-observation-schedule-wave-lease-diverged".to_string(),
                        );
                    } else {
                        observation_leases.push(schedule_observed);
                        observation_lease_values
                            .push(value_or_empty_text(raw_schedule.get("leaseUntil")));
                    }
                }
                if let (Some(schedule_updated), Some(wave_updated)) =
                    (schedule_updated_at, wave_updated_at)
                {
                    observation_update_times.push(schedule_updated.max(wave_updated));
                    if let Some(schedule_observed) = schedule_observed_lease {
                        if schedule_observed <= schedule_updated {
                            errors.push(
                                "runner-lease-observation-schedule-lease-not-active".to_string(),
                            );
                        }
                    }
                    if let Some(wave_observed) = wave_observed_lease {
                        if wave_observed <= wave_updated {
                            errors
                                .push("runner-lease-observation-wave-lease-not-active".to_string());
                        }
                    }
                }
            }
            if raw_observations.len() < 2
                || observation_leases.len() < 2
                || observation_update_times.len() < 2
            {
                errors.push("runner-lease-observations-insufficient".to_string());
            }
            if observation_leases.windows(2).any(|pair| pair[1] <= pair[0]) {
                errors.push("runner-lease-observations-not-strictly-increasing".to_string());
            }
            if observation_update_times
                .windows(2)
                .any(|pair| pair[1] <= pair[0])
            {
                errors.push("runner-lease-observation-updates-not-strictly-increasing".to_string());
            }
        }
    }

    if let (Some(first), Some(last)) = (
        observation_lease_values.first(),
        observation_lease_values.last(),
    ) {
        if value_or_empty_text(evidence.get("firstRunnerLeaseUntil")) != *first {
            errors.push("first-runner-lease-not-bound-to-durable-observation".to_string());
        }
        if value_or_empty_text(evidence.get("renewedRunnerLeaseUntil")) != *last {
            errors.push("renewed-runner-lease-not-bound-to-durable-observation".to_string());
        }
        if value_or_empty_text(crash_schedule.get("leaseUntil")) != *last {
            errors.push("crash-schedule-lease-not-bound-to-last-observation".to_string());
        }
        if value_or_empty_text(crash_wave.get("leaseUntil")) != *last {
            errors.push("crash-wave-lease-not-bound-to-last-observation".to_string());
        }
    }

    let raw_journal_crash = evidence
        .get("journalStateAtCrash")
        .and_then(Value::as_object);
    if raw_journal_crash.is_none() {
        errors.push("journal-state-at-crash-must-be-object".to_string());
    }
    let journal_crash = raw_journal_crash.cloned().unwrap_or_default();
    if value_or_empty_text(journal_crash.get("actionId")) != action_id {
        errors.push("journal-state-at-crash-action-id-binding-mismatch".to_string());
    }
    let journal_crash_epoch = match python_integer(journal_crash.get("executionEpoch")) {
        Some(value) => Some(value),
        None => {
            errors.push("journal-state-at-crash-invalid-execution-epoch".to_string());
            None
        }
    };
    if !same_int(journal_crash_epoch.as_ref(), &journal_epoch_a) {
        errors.push("journal-state-at-crash-epoch-binding-mismatch".to_string());
    }
    if value_or_empty_text(journal_crash.get("ownerInstanceId")) != expected_worker_a_owner {
        errors.push("journal-state-at-crash-owner-binding-mismatch".to_string());
    }
    if value_or_empty_text(journal_crash.get("leaseUntil"))
        != value_or_empty_text(evidence.get("workerALeaseUntil"))
    {
        errors.push("journal-state-at-crash-lease-binding-mismatch".to_string());
    }
    if value_or_empty_text(journal_crash.get("state")) != "EXECUTING" {
        errors.push("journal-state-at-crash-not-executing".to_string());
    }
    let journal_crash_handle = journal_crash
        .get("effectHandle")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if value_or_empty_text(journal_crash_handle.get("kind")) != "repair"
        || value_or_empty_text(journal_crash_handle.get("repairId"))
            != value_or_empty_text(evidence.get("repairId"))
    {
        errors.push("journal-state-at-crash-effect-binding-mismatch".to_string());
    }

    let claim_records = [&claim_schedule, &claim_wave, &claim_action];
    let claim_leases: Vec<Option<i64>> = claim_records
        .iter()
        .map(|record| {
            parse_record_timestamp(
                record,
                "leaseUntil",
                "runner-state-takeover-claim-invalid-lease",
                &mut errors,
            )
        })
        .collect();
    if claim_leases.iter().all(Option::is_some) {
        let unique: std::collections::BTreeSet<_> =
            claim_leases.iter().filter_map(|value| *value).collect();
        if unique.len() != 1 {
            errors.push("runner-state-takeover-claim-lease-diverged".to_string());
        }
    }
    let claim_times: Vec<Option<i64>> = claim_records
        .iter()
        .map(|record| {
            parse_record_timestamp(
                record,
                "updatedAt",
                "runner-state-takeover-claim-invalid-updated-at",
                &mut errors,
            )
        })
        .collect();
    for (claim_lease, claim_time) in claim_leases.iter().zip(claim_times.iter()) {
        if let (Some(lease), Some(time)) = (claim_lease, claim_time) {
            if lease <= time {
                errors.push("runner-state-takeover-claim-lease-not-active".to_string());
            }
        }
    }

    let outer_lease_values = [
        parsed_lease_times[0],
        parsed_lease_times[1],
        parsed_lease_times[2],
        parsed_lease_times[3],
    ];
    let max_outer_lease = if outer_lease_values.iter().all(Option::is_some) {
        outer_lease_values.iter().flatten().copied().max()
    } else {
        None
    };
    if let Some(max_outer) = max_outer_lease {
        if claim_times
            .iter()
            .any(|claim_time| claim_time.is_some_and(|time| time <= max_outer))
        {
            errors.push("takeover-claim-occurred-before-all-worker-a-leases-expired".to_string());
        }
    }

    let terminal_times: Vec<Option<i64>> = [&takeover_schedule, &takeover_wave, &takeover_action]
        .into_iter()
        .map(|record| {
            parse_record_timestamp(
                record,
                "updatedAt",
                "runner-state-takeover-invalid-updated-at",
                &mut errors,
            )
        })
        .collect();
    let latest_claim_time = if claim_times.iter().all(Option::is_some) {
        claim_times.iter().flatten().copied().max()
    } else {
        None
    };
    let earliest_terminal_time = if terminal_times.iter().all(Option::is_some) {
        terminal_times.iter().flatten().copied().min()
    } else {
        None
    };
    if let (Some(latest_claim), Some(earliest_terminal)) =
        (latest_claim_time, earliest_terminal_time)
    {
        if latest_claim >= earliest_terminal {
            errors.push("takeover-claim-not-before-terminal-runner-state".to_string());
        }
    }

    let mut journal_takeover_at = None;
    if let Some(raw_journal_events) = evidence.get("journalEvents").and_then(Value::as_array) {
        for raw_event in raw_journal_events {
            let Some(raw_event) = raw_event.as_object() else {
                continue;
            };
            let event_handle = raw_event
                .get("effectHandle")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let Some(event_epoch) = python_integer(raw_event.get("executionEpoch")) else {
                continue;
            };
            if value_or_empty_text(raw_event.get("eventType")) == "ACTION_TAKEOVER"
                && value_or_empty_text(raw_event.get("state")) == "RECONCILING"
                && value_or_empty_text(raw_event.get("actionId")) == action_id
                && event_epoch.compare(&journal_epoch_b) == Ordering::Equal
                && value_or_empty_text(raw_event.get("ownerInstanceId")) == expected_worker_b_owner
                && value_or_empty_text(event_handle.get("kind")) == "repair"
                && value_or_empty_text(event_handle.get("repairId"))
                    == value_or_empty_text(evidence.get("repairId"))
            {
                journal_takeover_at = parse_record_timestamp(
                    raw_event,
                    "createdAt",
                    "runner-state-invalid-journal-takeover-created-at",
                    &mut errors,
                );
            }
        }
    }
    if journal_takeover_at.is_none() {
        errors.push("missing-action-bound-journal-takeover-event".to_string());
    }
    if let (Some(max_outer), Some(journal_takeover)) = (max_outer_lease, journal_takeover_at) {
        if journal_takeover <= max_outer {
            errors.push("journal-takeover-occurred-before-all-worker-a-leases-expired".to_string());
        }
    }

    let Some(settlement_events) = evidence.get("settlementEvents").and_then(Value::as_array) else {
        errors.push("settlement-events-must-be-list".to_string());
        return errors;
    };
    let mut consuming_indexes = Vec::new();
    let mut consumed_indexes = Vec::new();
    let mut settlement_times = Vec::new();
    let repair_id = value_or_empty_text(evidence.get("repairId"));
    for (index, raw_event) in settlement_events.iter().enumerate() {
        let Some(raw_event) = raw_event.as_object() else {
            errors.push("settlement-event-not-object".to_string());
            continue;
        };
        let status = value_or_empty_text(raw_event.get("toStatus"));
        if status != "CONSUMING" && status != "CONSUMED" {
            continue;
        }
        if status == "CONSUMING" {
            consuming_indexes.push(index);
        } else {
            consumed_indexes.push(index);
        }
        if value_or_empty_text(raw_event.get("actionId")) != action_id {
            errors.push("settlement-action-id-not-bound-to-action".to_string());
        }
        if let Some(settlement_time) = parse_record_timestamp(
            raw_event,
            "createdAt",
            "invalid-settlement-created-at",
            &mut errors,
        ) {
            settlement_times.push(settlement_time);
        }
        let Some(settlement_epoch) = python_integer(raw_event.get("executionEpoch")) else {
            errors.push("invalid-settlement-execution-epoch".to_string());
            continue;
        };
        if settlement_epoch.compare(&journal_epoch_b) != Ordering::Equal {
            errors.push("settlement-execution-epoch-not-bound-to-takeover".to_string());
        }
        let effect_handle = raw_event
            .get("effectHandle")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        if value_or_empty_text(effect_handle.get("kind")) != "repair"
            || value_or_empty_text(effect_handle.get("repairId")) != repair_id
        {
            errors.push("settlement-effect-handle-not-bound-to-repair".to_string());
        }
    }
    if consuming_indexes.len() != 1 {
        errors.push("settlement-consuming-count-not-exactly-one".to_string());
    }
    if consumed_indexes.len() != 1 {
        errors.push("settlement-consumed-count-not-exactly-one".to_string());
    }
    if let (Some(consuming), Some(consumed)) = (consuming_indexes.first(), consumed_indexes.first())
    {
        if consumed <= consuming {
            errors.push("settlement-consumed-before-consuming".to_string());
        }
    }
    if let Some(latest_claim) = latest_claim_time {
        if let Some(min_settlement) = settlement_times.iter().copied().min() {
            if latest_claim >= min_settlement {
                errors.push("takeover-claim-not-before-settlement".to_string());
            }
        }
    }
    errors
}

fn runner_record(
    snapshot: &Map<String, Value>,
    phase: &str,
    record_name: &str,
    errors: &mut Vec<String>,
) -> Map<String, Value> {
    match snapshot.get(record_name).and_then(Value::as_object) {
        Some(record) => record.clone(),
        None => {
            errors.push(format!("runner-state-{phase}-{record_name}-must-be-object"));
            Map::new()
        }
    }
}

fn runner_epoch(
    record: &Map<String, Value>,
    phase: &str,
    field: &str,
    errors: &mut Vec<String>,
) -> Option<PythonInteger> {
    match python_integer(record.get(field)) {
        Some(value) => Some(value),
        None => {
            errors.push(format!("runner-state-{phase}-invalid-{field}"));
            None
        }
    }
}

fn parse_record_timestamp(
    record: &Map<String, Value>,
    field: &str,
    error: &str,
    errors: &mut Vec<String>,
) -> Option<i64> {
    match parse_timestamp(&value_or_empty_text(record.get(field))) {
        ParsedTimestamp::Aware(instant) => Some(instant),
        _ => {
            errors.push(error.to_string());
            None
        }
    }
}

fn same_int(left: Option<&PythonInteger>, right: &PythonInteger) -> bool {
    left.is_some_and(|value| value.compare(right) == Ordering::Equal)
}
