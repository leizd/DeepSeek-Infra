use crate::envelope::EvidenceProofError;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

pub const FEDERATION_RUNTIME_PROOF_SCHEMA: &str = "federation-runtime-e2e-proof-v1";
pub const FEDERATION_RUNTIME_PROOF_CHECKS: &[&str] = &[
    "realTwoFleetFourMinioReplicationE2E",
    "realReceiverProcessSigkillResumesTransfer",
    "realReceiverRestartDoesNotDuplicateCommit",
    "revokedPeerCannotStartTransfer",
    "fleetProcessesUseDistinctStorageCredentials",
];

const PROOF_FIELDS: &[&str] = &[
    "schema",
    "validatedAt",
    "fleetProcesses",
    "storagePrincipalIsolation",
    "minioTopology",
    "transferRecovery",
    "failClosed",
    "dr",
    "proofDigest",
];
const PROCESS_FIELDS: &[&str] = &["fleetId", "pid", "rootFingerprint"];
const STORAGE_ISOLATION_FIELDS: &[&str] = &[
    "sourcePrincipalDigest",
    "receiverPrincipalDigest",
    "sourceToReceiverDeniedCode",
    "receiverToSourceDeniedCode",
];
const TOPOLOGY_FIELDS: &[&str] = &["endpoints", "containers", "targetBindings"];
const TARGET_BINDING_FIELDS: &[&str] = &[
    "fleetId",
    "role",
    "targetId",
    "endpoint",
    "providerObjectCount",
];
const RECOVERY_FIELDS: &[&str] = &[
    "transferId",
    "senderTransferId",
    "receiverTransferId",
    "interruptedComponentDigest",
    "interruptedBytesSent",
    "interruptedComponentBytes",
    "reconcileStatus",
    "reconcileState",
    "senderFinalState",
    "remoteCommittedEvents",
    "commitEffectDigest",
    "repeatedCommitEffectDigest",
    "localInventoryBeforeDigest",
    "localInventoryAfterDigest",
];
const DR_FIELDS: &[&str] = &[
    "schema",
    "transferId",
    "restorePath",
    "cleanupCompleted",
    "workspaceDigest",
];
const SECRET_FIELD_NAMES: &[&str] = &[
    "accesskey",
    "ageidentity",
    "ageprivateidentity",
    "credential",
    "credentialref",
    "credentialreference",
    "passphrase",
    "password",
    "privatekey",
    "privatekeyenvelope",
    "secret",
    "secretkey",
    "sessiontoken",
];
const SECRET_VALUE_MARKERS: &[&str] = &[
    "age-secret-key-",
    "begin private key",
    "begin openssh private key",
];

pub fn federation_runtime_proof_digest(proof: &Value) -> Result<String, EvidenceProofError> {
    let mut payload = proof
        .as_object()
        .cloned()
        .ok_or(EvidenceProofError::InvalidDocument)?;
    payload.remove("proofDigest");
    let bytes =
        canonical_bytes(&Value::Object(payload)).ok_or(EvidenceProofError::InvalidDocument)?;
    Ok(typed_sha256(&bytes))
}

pub fn validate_federation_runtime_proof(value: &Value) -> Vec<String> {
    if canonical_bytes(value).is_none() {
        return vec!["federation-runtime-proof-canonical-payload-invalid".to_string()];
    }
    let Some(proof) = value.as_object() else {
        return vec!["federation-runtime-proof-must-be-object".to_string()];
    };
    let mut errors = Vec::new();
    if !exact_fields(proof, PROOF_FIELDS) {
        errors.push("federation-runtime-proof-fields-invalid".to_string());
    }
    if string(proof, "schema") != Some(FEDERATION_RUNTIME_PROOF_SCHEMA) {
        errors.push("federation-runtime-proof-schema-invalid".to_string());
    }
    if contains_secret(value) {
        errors.push("federation-runtime-proof-contains-secret".to_string());
    }
    if !string(proof, "validatedAt").is_some_and(canonical_utc_timestamp_valid) {
        errors.push("federation-runtime-proof-timestamp-invalid".to_string());
    }

    validate_processes(proof.get("fleetProcesses"), &mut errors);
    validate_storage_isolation(proof.get("storagePrincipalIsolation"), &mut errors);
    validate_topology(proof.get("minioTopology"), &mut errors);
    let transfer_id = validate_recovery(proof.get("transferRecovery"), &mut errors);
    validate_fail_closed(proof.get("failClosed"), &mut errors);
    validate_dr(proof.get("dr"), transfer_id, &mut errors);

    let declared_digest = string(proof, "proofDigest");
    let computed_digest = federation_runtime_proof_digest(value).ok();
    if !declared_digest.is_some_and(is_typed_sha256)
        || computed_digest.as_deref() != declared_digest
    {
        errors.push("federation-runtime-proof-digest-mismatch".to_string());
    }
    deduplicate(errors)
}

fn validate_processes(value: Option<&Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let processes = document(value, "fleet-processes", errors).unwrap_or(&empty);
    if !exact_fields(
        processes,
        &[
            "source",
            "receiverBefore",
            "receiverAfter",
            "receiverKillReturnCode",
        ],
    ) {
        errors.push("fleet-process-fields-invalid".to_string());
    }
    let source = document(processes.get("source"), "source-process", errors).unwrap_or(&empty);
    let before = document(
        processes.get("receiverBefore"),
        "receiver-before-process",
        errors,
    )
    .unwrap_or(&empty);
    let after = document(
        processes.get("receiverAfter"),
        "receiver-after-process",
        errors,
    )
    .unwrap_or(&empty);
    validate_process("source", source, errors);
    validate_process("receiver-before", before, errors);
    validate_process("receiver-after", after, errors);

    let pids = [
        processes
            .get("source")
            .and_then(Value::as_object)
            .and_then(|item| item.get("pid")),
        processes
            .get("receiverBefore")
            .and_then(Value::as_object)
            .and_then(|item| item.get("pid")),
        processes
            .get("receiverAfter")
            .and_then(Value::as_object)
            .and_then(|item| item.get("pid")),
    ];
    if pids[0] == pids[1] || pids[0] == pids[2] || pids[1] == pids[2] {
        errors.push("fleet-processes-not-independent".to_string());
    }
    if string(source, "fleetId") != Some("fleet-a")
        || string(before, "fleetId") != Some("fleet-b")
        || string(after, "fleetId") != Some("fleet-b")
    {
        errors.push("fleet-process-identity-invalid".to_string());
    }
    if string(source, "rootFingerprint") == string(before, "rootFingerprint")
        || string(before, "rootFingerprint") != string(after, "rootFingerprint")
    {
        errors.push("fleet-root-sovereignty-invalid".to_string());
    }
    if !processes
        .get("receiverKillReturnCode")
        .is_some_and(nonzero_integer)
    {
        errors.push("receiver-sigkill-exit-invalid".to_string());
    }
}

fn validate_process(label: &str, process: &Map<String, Value>, errors: &mut Vec<String>) {
    if !exact_fields(process, PROCESS_FIELDS) {
        errors.push(format!("{label}-process-fields-invalid"));
    }
    if !process.get("pid").is_some_and(positive_integer) {
        errors.push(format!("{label}-pid-invalid"));
    }
    if !string(process, "rootFingerprint").is_some_and(is_typed_sha256) {
        errors.push(format!("{label}-root-fingerprint-invalid"));
    }
}

fn validate_storage_isolation(value: Option<&Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let isolation = document(value, "storage-principal-isolation", errors).unwrap_or(&empty);
    if !exact_fields(isolation, STORAGE_ISOLATION_FIELDS) {
        errors.push("storage-principal-isolation-fields-invalid".to_string());
    }
    let source = string(isolation, "sourcePrincipalDigest");
    let receiver = string(isolation, "receiverPrincipalDigest");
    let denied = [
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
    ];
    if !source.is_some_and(is_typed_sha256)
        || !receiver.is_some_and(is_typed_sha256)
        || source == receiver
        || !string(isolation, "sourceToReceiverDeniedCode")
            .is_some_and(|value| denied.contains(&value))
        || !string(isolation, "receiverToSourceDeniedCode")
            .is_some_and(|value| denied.contains(&value))
    {
        errors.push("cross-fleet-storage-principal-isolation-invalid".to_string());
    }
}

fn validate_topology(value: Option<&Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let topology = document(value, "minio-topology", errors).unwrap_or(&empty);
    if !exact_fields(topology, TOPOLOGY_FIELDS) {
        errors.push("minio-topology-fields-invalid".to_string());
    }

    let endpoints = string_array(topology.get("endpoints"), 4);
    if endpoints.is_none()
        || endpoints
            .as_ref()
            .is_some_and(|items| unique_strings(items).len() != 4)
    {
        errors.push("four-minio-endpoints-invalid".to_string());
    }
    let endpoints = endpoints.unwrap_or_default();
    for endpoint in &endpoints {
        if !endpoint_valid(endpoint) {
            errors.push("minio-endpoint-invalid".to_string());
        }
    }

    let containers = string_array(topology.get("containers"), 4);
    if containers.is_none()
        || containers.as_ref().is_some_and(|items| {
            items.iter().any(|item| item.is_empty()) || unique_strings(items).len() != 4
        })
    {
        errors.push("four-minio-containers-invalid".to_string());
    }

    let bindings = topology.get("targetBindings").and_then(Value::as_array);
    if bindings.is_none_or(|items| items.len() != 4) {
        errors.push("four-minio-target-bindings-invalid".to_string());
    }
    let bindings = bindings.filter(|items| items.len() == 4);
    let mut roles = BTreeSet::new();
    let mut target_ids = BTreeSet::new();
    let mut binding_endpoints = BTreeSet::new();
    let mut role_fleets = BTreeMap::new();
    for binding_value in bindings.into_iter().flatten() {
        let Some(binding) = document(Some(binding_value), "minio-target-binding", errors) else {
            errors.push("minio-target-binding-fields-invalid".to_string());
            continue;
        };
        if !exact_fields(binding, TARGET_BINDING_FIELDS) {
            errors.push("minio-target-binding-fields-invalid".to_string());
            continue;
        }
        let role = string(binding, "role");
        let fleet_id = string(binding, "fleetId");
        let target_id = string(binding, "targetId");
        let endpoint = string(binding, "endpoint");
        if [role, fleet_id, target_id, endpoint]
            .iter()
            .any(|value| value.is_none_or(str::is_empty))
        {
            errors.push("minio-target-binding-value-invalid".to_string());
            continue;
        }
        let (role, fleet_id, target_id, endpoint) = (
            role.unwrap_or_default(),
            fleet_id.unwrap_or_default(),
            target_id.unwrap_or_default(),
            endpoint.unwrap_or_default(),
        );
        roles.insert(role.to_string());
        target_ids.insert(target_id.to_string());
        binding_endpoints.insert(endpoint.to_string());
        role_fleets.insert(role.to_string(), fleet_id.to_string());
        if !binding
            .get("providerObjectCount")
            .is_some_and(positive_integer)
        {
            errors.push("minio-provider-object-count-invalid".to_string());
        }
    }

    let expected_roles = ["A1", "A2", "B1", "B2"]
        .into_iter()
        .map(str::to_string)
        .collect::<BTreeSet<_>>();
    let expected_role_fleets = [
        ("A1".to_string(), "fleet-a".to_string()),
        ("A2".to_string(), "fleet-a".to_string()),
        ("B1".to_string(), "fleet-b".to_string()),
        ("B2".to_string(), "fleet-b".to_string()),
    ]
    .into_iter()
    .collect::<BTreeMap<_, _>>();
    if roles != expected_roles
        || role_fleets != expected_role_fleets
        || target_ids.len() != 4
        || binding_endpoints != unique_strings(&endpoints)
    {
        errors.push("four-minio-role-binding-invalid".to_string());
    }
}

fn validate_recovery<'a>(value: Option<&'a Value>, errors: &mut Vec<String>) -> Option<&'a Value> {
    let empty = Map::new();
    let recovery_document = document(value, "transfer-recovery", errors);
    let recovery = recovery_document.unwrap_or(&empty);
    if !exact_fields(recovery, RECOVERY_FIELDS) {
        errors.push("transfer-recovery-fields-invalid".to_string());
    }
    let transfer_id = recovery_document.and_then(|document| document.get("transferId"));
    if !transfer_id
        .and_then(Value::as_str)
        .is_some_and(is_typed_sha256)
        || recovery.get("senderTransferId") != transfer_id
        || recovery.get("receiverTransferId") != transfer_id
    {
        errors.push("runtime-transfer-binding-invalid".to_string());
    }
    if !recovery
        .get("interruptedComponentDigest")
        .and_then(Value::as_str)
        .is_some_and(is_plain_sha256)
    {
        errors.push("interrupted-component-digest-invalid".to_string());
    }
    let sent = recovery.get("interruptedBytesSent").and_then(Value::as_u64);
    let total = recovery
        .get("interruptedComponentBytes")
        .and_then(Value::as_u64);
    if !matches!((sent, total), (Some(sent), Some(total)) if sent > 0 && sent < total) {
        errors.push("interrupted-transfer-bytes-invalid".to_string());
    }
    if string(recovery, "reconcileStatus") != Some("RESUME")
        || string(recovery, "reconcileState") == Some("REMOTE_COMMITTED")
    {
        errors.push("receiver-reconcile-state-invalid".to_string());
    }
    if string(recovery, "senderFinalState") != Some("SUCCEEDED") {
        errors.push("sender-terminal-state-invalid".to_string());
    }
    match recovery
        .get("remoteCommittedEvents")
        .and_then(Value::as_array)
    {
        Some(events) if events.len() == 1 => {
            let empty_event = Map::new();
            let event =
                document(events.first(), "remote-commit-event", errors).unwrap_or(&empty_event);
            if event.get("transferId") != transfer_id
                || string(event, "nextState") != Some("REMOTE_COMMITTED")
            {
                errors.push("remote-commit-event-binding-invalid".to_string());
            }
        }
        _ => errors.push("remote-commit-event-count-invalid".to_string()),
    }
    for field in [
        "commitEffectDigest",
        "repeatedCommitEffectDigest",
        "localInventoryBeforeDigest",
        "localInventoryAfterDigest",
    ] {
        if !string(recovery, field).is_some_and(is_typed_sha256) {
            errors.push(format!("{field}-invalid"));
        }
    }
    if recovery.get("commitEffectDigest") != recovery.get("repeatedCommitEffectDigest") {
        errors.push("remote-commit-effect-duplicated".to_string());
    }
    if recovery.get("localInventoryBeforeDigest") != recovery.get("localInventoryAfterDigest") {
        errors.push("local-inventory-regressed".to_string());
    }
    transfer_id
}

fn validate_fail_closed(value: Option<&Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let failures = document(value, "fail-closed", errors).unwrap_or(&empty);
    if !exact_fields(
        failures,
        &[
            "replayedIngressGrant",
            "tamperedReplicaAttestation",
            "revokedPeer",
        ],
    ) || string(failures, "replayedIngressGrant")
        != Some("FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY")
        || string(failures, "tamperedReplicaAttestation")
            != Some("FEDERATION_DOCUMENT_SIGNATURE_INVALID")
        || string(failures, "revokedPeer") != Some("FEDERATION_PEER_REVOKED")
    {
        errors.push("runtime-fail-closed-evidence-invalid".to_string());
    }
}

fn validate_dr(value: Option<&Value>, transfer_id: Option<&Value>, errors: &mut Vec<String>) {
    let empty = Map::new();
    let dr = document(value, "runtime-dr", errors).unwrap_or(&empty);
    if !exact_fields(dr, DR_FIELDS) {
        errors.push("runtime-dr-fields-invalid".to_string());
    }
    if string(dr, "schema") != Some("federated-dr-drill-attestation-v1")
        || dr.get("transferId") != transfer_id
        || string(dr, "restorePath") != Some("backup-recovery-drill-production-v1")
        || dr.get("cleanupCompleted") != Some(&Value::Bool(true))
        || !string(dr, "workspaceDigest").is_some_and(is_typed_sha256)
    {
        errors.push("runtime-dr-evidence-invalid".to_string());
    }
}

fn document<'a>(
    value: Option<&'a Value>,
    label: &str,
    errors: &mut Vec<String>,
) -> Option<&'a Map<String, Value>> {
    match value.and_then(Value::as_object) {
        Some(value) => Some(value),
        None => {
            errors.push(format!("{label}-must-be-object"));
            None
        }
    }
}

fn exact_fields(value: &Map<String, Value>, fields: &[&str]) -> bool {
    value.len() == fields.len() && fields.iter().all(|field| value.contains_key(*field))
}

fn string<'a>(value: &'a Map<String, Value>, field: &str) -> Option<&'a str> {
    value.get(field).and_then(Value::as_str)
}

fn string_array(value: Option<&Value>, length: usize) -> Option<Vec<&str>> {
    let values = value?.as_array()?;
    if values.len() != length {
        return None;
    }
    values.iter().map(Value::as_str).collect()
}

fn unique_strings(values: &[&str]) -> BTreeSet<String> {
    values.iter().map(|value| (*value).to_string()).collect()
}

fn positive_integer(value: &Value) -> bool {
    value.as_u64().is_some_and(|value| value > 0)
}

fn nonzero_integer(value: &Value) -> bool {
    value.as_i64().is_some_and(|value| value != 0) || value.as_u64().is_some_and(|value| value != 0)
}

fn endpoint_valid(endpoint: &str) -> bool {
    let Some(authority_and_path) = endpoint
        .strip_prefix("http://")
        .or_else(|| endpoint.strip_prefix("https://"))
    else {
        return false;
    };
    let authority = authority_and_path
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default();
    if authority.is_empty() || authority.contains('@') || authority.chars().any(char::is_whitespace)
    {
        return false;
    }
    let (host, port) = if let Some(bracketed) = authority.strip_prefix('[') {
        let Some((host, suffix)) = bracketed.split_once(']') else {
            return false;
        };
        let Some(port) = suffix.strip_prefix(':') else {
            return false;
        };
        (host, port)
    } else {
        let Some((host, port)) = authority.rsplit_once(':') else {
            return false;
        };
        if host.contains(':') {
            return false;
        }
        (host, port)
    };
    !host.is_empty() && !port.is_empty() && port.parse::<u16>().is_ok()
}

fn canonical_utc_timestamp_valid(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'Z'
    {
        return false;
    }
    let Some(year) = decimal(bytes, 0, 4) else {
        return false;
    };
    let Some(month) = decimal(bytes, 5, 2) else {
        return false;
    };
    let Some(day) = decimal(bytes, 8, 2) else {
        return false;
    };
    let Some(hour) = decimal(bytes, 11, 2) else {
        return false;
    };
    let Some(minute) = decimal(bytes, 14, 2) else {
        return false;
    };
    let Some(second) = decimal(bytes, 17, 2) else {
        return false;
    };
    if year == 0 || !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return false;
    }
    let leap_year = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = match month {
        2 if leap_year => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    (1..=days_in_month).contains(&day)
}

fn decimal(bytes: &[u8], start: usize, length: usize) -> Option<u32> {
    bytes
        .get(start..start + length)?
        .iter()
        .try_fold(0_u32, |value, byte| {
            byte.is_ascii_digit()
                .then(|| value * 10 + u32::from(*byte - b'0'))
        })
}

fn canonical_bytes(value: &Value) -> Option<Vec<u8>> {
    let mut normalized = value.clone();
    normalized.sort_all_objects();
    serde_json::to_vec(&normalized).ok()
}

fn typed_sha256(value: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(value)))
}

fn is_plain_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_typed_sha256(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(is_plain_sha256)
}

fn contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(items) => items.iter().any(|(key, item)| {
            let normalized_key: String = key
                .chars()
                .flat_map(char::to_lowercase)
                .filter(char::is_ascii_alphanumeric)
                .collect();
            SECRET_FIELD_NAMES.contains(&normalized_key.as_str()) || contains_secret(item)
        }),
        Value::Array(items) => items.iter().any(contains_secret),
        Value::String(item) => {
            let lowered = item.to_lowercase();
            SECRET_VALUE_MARKERS
                .iter()
                .any(|marker| lowered.contains(marker))
        }
        _ => false,
    }
}

fn deduplicate(errors: Vec<String>) -> Vec<String> {
    let mut seen = BTreeSet::new();
    errors
        .into_iter()
        .filter(|error| seen.insert(error.clone()))
        .collect()
}

mod hex {
    use std::fmt::Write;

    pub fn encode(data: impl AsRef<[u8]>) -> String {
        let mut encoded = String::with_capacity(data.as_ref().len() * 2);
        for byte in data.as_ref() {
            let _ = write!(encoded, "{byte:02x}");
        }
        encoded
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoints_require_an_explicit_http_port() {
        assert!(endpoint_valid("http://127.0.0.1:9000"));
        assert!(endpoint_valid("https://[::1]:9000/path"));
        assert!(!endpoint_valid("http://127.0.0.1"));
        assert!(!endpoint_valid("file://127.0.0.1:9000"));
        assert!(!endpoint_valid("http://user:secret@127.0.0.1:9000"));
    }
}
