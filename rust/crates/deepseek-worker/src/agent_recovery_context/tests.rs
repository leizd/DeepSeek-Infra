use super::*;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentRunRecoveryScope, AgentRunRecoverySelection,
};
use serde_json::json;

fn context() -> AgentRunRecoveryContext {
    AgentRunRecoveryContext {
        schema_version: 1,
        run_id: "run-recovery".into(),
        generation: 1,
        metadata_epoch: 3,
        expected_index: 12,
        expected_runtime_revision: 2,
        expected_request: Some(AgentArtifactReference {
            sha256: "a".repeat(64),
            length: 30,
        }),
        selection: Some(AgentRunRecoverySelection {
            kind: 1,
            agent_id: String::new(),
            resynthesize: None,
        }),
        ..Default::default()
    }
}

fn persist(root: &Path, bytes: &[u8]) -> Value {
    let envelope = json!({"schema":SCHEMA,"metadataProto":URL_SAFE_NO_PAD.encode(bytes)});
    ArtifactStore::open_native(root, "python_disabled")
        .unwrap()
        .put(&envelope)
        .unwrap();
    json!({PRIVATE_FIELD:envelope})
}

fn snapshot(prepared: &PreparedRecovery) -> AgentRunSnapshot {
    AgentRunSnapshot {
        run_id: prepared.context.run_id.clone(),
        runtime_revision: 3,
        next_index: 14,
        request: Some(AgentArtifactReference {
            sha256: "b".repeat(64),
            length: 40,
        }),
        recovery_scope: Some(AgentRunRecoveryScope {
            generation: 1,
            start_index: 12,
            context_sha256: prepared.digest.clone(),
            context: Some(AgentArtifactReference {
                sha256: prepared.reference.sha256.clone(),
                length: prepared.reference.length,
            }),
        }),
        ..Default::default()
    }
}

#[test]
fn recovery_context_artifact_survives_reopen_and_binds_exact_runtime() {
    let root = tempfile::tempdir().unwrap();
    let context = context();
    let body = persist(root.path(), &context.encode_to_vec());
    let prepared = read(root.path(), &body).unwrap().unwrap();
    assert_eq!(prepared.context, context);
    assert_eq!(prepared.digest, digest(&context).unwrap());
    validate_binding(Some(&prepared), &snapshot(&prepared)).unwrap();
    assert!(read(root.path(), &json!({})).unwrap().is_none());
    validate_binding(None, &AgentRunSnapshot::default()).unwrap();
}

#[test]
fn recovery_context_requires_its_hash_checked_rust_artifact() {
    let root = tempfile::tempdir().unwrap();
    let other = tempfile::tempdir().unwrap();
    let body = persist(root.path(), &context().encode_to_vec());
    assert!(read(other.path(), &body).is_err());
    let mut changed = body.clone();
    changed[PRIVATE_FIELD]["metadataProto"] = URL_SAFE_NO_PAD
        .encode({
            let mut c = context();
            c.run_id = "foreign".into();
            c.encode_to_vec()
        })
        .into();
    assert!(read(root.path(), &changed).is_err());
}

#[test]
fn recovery_context_rejects_unknown_fields_duplicate_tags_and_nested_extensions() {
    let root = tempfile::tempdir().unwrap();
    let mut unknown = context().encode_to_vec();
    unknown.extend([0xa0, 0x06, 0x01]);
    let mut duplicate = context().encode_to_vec();
    duplicate.extend([0x08, 0x01]);
    let mut value = context();
    let mut artifact = value.expected_request.take().unwrap().encode_to_vec();
    value.selection = None;
    artifact.extend([0xa0, 0x06, 0x01]);
    assert!(artifact.len() < 128);
    let mut nested = value.encode_to_vec();
    nested.extend([0x3a, artifact.len() as u8]);
    nested.extend(artifact);
    for bytes in [unknown, duplicate, nested] {
        assert!(read(root.path(), &persist(root.path(), &bytes)).is_err());
    }
}

#[test]
fn recovery_context_rejects_malformed_envelopes_and_invalid_scope_values() {
    let root = tempfile::tempdir().unwrap();
    let body = persist(root.path(), &context().encode_to_vec());
    for envelope in [
        Value::Null,
        json!({"schema":"foreign","metadataProto":"AA"}),
        json!({"schema":SCHEMA,"metadataProto":"="}),
        json!({"schema":SCHEMA,"metadataProto":"AA","extra":true}),
        json!({"schema":SCHEMA,"metadataProto":"x".repeat(MAX_BYTES.div_ceil(3)*4+1)}),
    ] {
        assert!(read(root.path(), &json!({PRIVATE_FIELD:envelope})).is_err());
    }
    type Change = fn(&mut AgentRunRecoveryContext);
    let changes: &[Change] = &[
        |c| c.schema_version = 2,
        |c| c.run_id.clear(),
        |c| c.generation = 0,
        |c| c.generation = u64::MAX,
        |c| c.metadata_epoch = 0,
        |c| c.expected_index = 0,
        |c| c.expected_runtime_revision = 0,
        |c| c.expected_runtime_revision = u64::MAX,
    ];
    for change in changes {
        let mut value = context();
        change(&mut value);
        assert!(read(root.path(), &persist(root.path(), &value.encode_to_vec())).is_err());
    }
    assert!(read(root.path(), &body).is_ok());
    let huge = AgentRunRecoveryContext {
        run_id: "x".repeat(MAX_BYTES + 1),
        ..context()
    };
    assert!(digest(&huge).is_err());
}

#[test]
fn recovery_execution_cannot_omit_rebind_or_downgrade_its_context() {
    let root = tempfile::tempdir().unwrap();
    let body = persist(root.path(), &context().encode_to_vec());
    let prepared = read(root.path(), &body).unwrap().unwrap();
    let run = snapshot(&prepared);
    assert!(validate_binding(None, &run).is_err());
    type Change = fn(&mut AgentRunSnapshot);
    let changes: &[Change] = &[
        |r| r.recovery_scope = None,
        |r| r.run_id = "foreign".into(),
        |r| r.runtime_revision += 1,
        |r| r.recovery_scope.as_mut().unwrap().generation += 1,
        |r| r.recovery_scope.as_mut().unwrap().start_index += 1,
        |r| r.next_index = 12,
        |r| r.recovery_scope.as_mut().unwrap().context_sha256 = "f".repeat(64),
        |r| r.recovery_scope.as_mut().unwrap().context = None,
        |r| {
            r.recovery_scope
                .as_mut()
                .unwrap()
                .context
                .as_mut()
                .unwrap()
                .sha256 = "e".repeat(64)
        },
        |r| {
            r.recovery_scope
                .as_mut()
                .unwrap()
                .context
                .as_mut()
                .unwrap()
                .length += 1
        },
        |r| r.request = context().expected_request,
    ];
    for change in changes {
        let mut changed = run.clone();
        change(&mut changed);
        assert!(validate_binding(Some(&prepared), &changed).is_err());
    }
}
