use super::*;
use prost::Message;

struct Prepared {
    root: tempfile::TempDir,
    draft: ArtifactReference,
    response: PrepareAgentCredentialsResponse,
    digest: String,
}

fn proto(reference: ArtifactReference) -> AgentArtifactReference {
    AgentArtifactReference {
        sha256: reference.sha256,
        length: reference.length,
    }
}

fn prepared(credentials: bool) -> Prepared {
    let root = tempfile::tempdir().unwrap();
    let mut store = ArtifactStore::open_native(root.path(), "python_disabled").unwrap();
    let body = json!({"payload":{"agentMode":true,"temperature":1.0,"zero":-0.0,
        "large":u64::MAX,"fraction":0.1,"messages":[{"role":"user","content":"保留正文"}]},
        "agentPreset":"research","_nativeRuntimePreparation":"isolated-chain-fixture"});
    let draft = store.put(&body).unwrap();
    let bytes = AgentRunRecoveryContext {
        schema_version: 1,
        run_id: "run-chain".into(),
        metadata_epoch: 2,
        expected_index: 7,
        expected_runtime_revision: 2,
        ..Default::default()
    }
    .encode_to_vec();
    let digest = format!("{:x}", Sha256::digest(&bytes));
    let context = json!({"schema":"native-agent-recovery-context-v1",
        "metadataProto":URL_SAFE_NO_PAD.encode(&bytes)});
    let context_ref = store.put(&context).unwrap();
    let mut base = store.get(&draft).unwrap();
    base.as_object_mut()
        .unwrap()
        .insert("_nativeRecoveryContext".into(), context);
    let base_ref = store.put(&base).unwrap();
    let scope = AgentCredentialScope {
        fleet_id: "fixture-fleet".into(),
        environment: "test".into(),
        run_id: "run-chain".into(),
        metadata_epoch: 2,
        runtime_revision: 3,
        request_sha256: base_ref.sha256.clone(),
        request_length: base_ref.length,
    };
    let object = credentials.then(|| AgentCredentialObjectReference {
        id: "c".repeat(64),
        sha256: "d".repeat(64),
        length: 72,
    });
    if let Some(object) = &object {
        base.as_object_mut().unwrap().insert(
            crate::agent_credential_client::PRIVATE_FIELD.into(),
            json!({"scope":{"fleetId":scope.fleet_id,"environment":scope.environment,
                "runId":scope.run_id,"metadataEpoch":scope.metadata_epoch,
                "runtimeRevision":scope.runtime_revision,"requestSha256":scope.request_sha256,
                "requestLength":scope.request_length},
                "object":{"id":object.id,"sha256":object.sha256,"length":object.length}}),
        );
    }
    let final_ref = store.put(&base).unwrap();
    Prepared {
        root,
        draft,
        response: PrepareAgentCredentialsResponse {
            scope: Some(scope),
            object,
            prepared_request: Some(proto(final_ref)),
            recovery_context: Some(proto(context_ref)),
            recovery_context_sha256: digest.clone(),
            recovery_event_bodies: vec![proto(base_ref)],
        },
        digest,
    }
}

#[tokio::test]
async fn prepared_recovery_chain_accepts_stored_numbers_and_both_credential_states() {
    for credentials in [false, true] {
        let fixture = prepared(credentials);
        verify_prepared(
            fixture.root.path(),
            &fixture.draft,
            &fixture.response,
            &fixture.digest,
        )
        .await
        .unwrap();
    }
}

#[tokio::test]
async fn prepared_recovery_chain_rejects_independently_valid_foreign_or_partial_bodies() {
    for changed in [
        "context-digest",
        "context-envelope",
        "context-bytes",
        "base-payload",
        "base-context",
        "missing-final-private",
        "foreign-private-scope",
        "foreign-object",
        "unbound-final-payload",
        "missing-draft",
        "missing-final",
        "wrong-base-length",
        "missing-object-metadata",
    ] {
        let mut fixture = prepared(true);
        let mut store = ArtifactStore::open_native(fixture.root.path(), "python_disabled").unwrap();
        let scope = fixture.response.scope.as_ref().unwrap();
        let base_ref = ArtifactReference {
            sha256: scope.request_sha256.clone(),
            length: scope.request_length,
        };
        match changed {
            "context-digest" => fixture.digest = "0".repeat(64),
            "context-envelope" | "context-bytes" => {
                let mut body = store
                    .get(&reference(fixture.response.recovery_context.as_ref()).unwrap())
                    .unwrap();
                if changed == "context-envelope" {
                    body["unexpected"] = json!(true);
                } else {
                    body["metadataProto"] = json!(URL_SAFE_NO_PAD.encode([8_u8, 99]));
                }
                fixture.response.recovery_context = Some(proto(store.put(&body).unwrap()));
            }
            "base-payload" | "base-context" => {
                let mut body = store.get(&base_ref).unwrap();
                if changed == "base-payload" {
                    body["payload"]["temperature"] = json!(1.25);
                } else {
                    body["_nativeRecoveryContext"]["metadataProto"] = json!("CGM");
                }
                let changed_ref = store.put(&body).unwrap();
                let scope = fixture.response.scope.as_mut().unwrap();
                scope.request_sha256 = changed_ref.sha256;
                scope.request_length = changed_ref.length;
            }
            "missing-draft" => fixture.draft.sha256 = "f".repeat(64),
            "missing-final" => fixture.response.prepared_request = None,
            "wrong-base-length" => fixture.response.scope.as_mut().unwrap().request_length += 1,
            "missing-object-metadata" => fixture.response.object = None,
            _ => {
                let mut body = store
                    .get(&reference(fixture.response.prepared_request.as_ref()).unwrap())
                    .unwrap();
                let field = crate::agent_credential_client::PRIVATE_FIELD;
                match changed {
                    "missing-final-private" => {
                        body.as_object_mut().unwrap().remove(field);
                    }
                    "foreign-private-scope" => body[field]["scope"]["runId"] = json!("foreign"),
                    "foreign-object" => body[field]["object"]["sha256"] = json!("e".repeat(64)),
                    _ => body["payload"]["temperature"] = json!(1.25),
                }
                fixture.response.prepared_request = Some(proto(store.put(&body).unwrap()));
            }
        }
        drop(store);
        assert!(
            verify_prepared(
                fixture.root.path(),
                &fixture.draft,
                &fixture.response,
                &fixture.digest,
            )
            .await
            .is_err(),
            "{changed}"
        );
    }
}
