//! A complete provider rejection is distinct from an unresolved provider effect.
use deepseek_protocol::generated::deepseek::agent::v1::AgentProviderFailureMetadata;
use serde_json::{Value, json};
use tonic::Status;

fn denied() -> Status {
    Status::failed_precondition("AGENT_PROVIDER_FAILURE_INVALID")
}

pub(crate) fn rejection_envelope(
    run: &str,
    node: &str,
    phase: i32,
    status: u16,
    response_sha256: &str,
) -> Result<Value, Status> {
    let role = match (phase, node) {
        (1, "leader") => "规划",
        (3, "synthesizer") => "综合",
        _ => return Err(denied()),
    };
    if run.is_empty()
        || !matches!(status, 400 | 401 | 402 | 422 | 429)
        || response_sha256.len() != 64
        || !response_sha256
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(denied());
    }
    // The raw provider error, credential, response body and fabricated usage
    // must never be projected into Go or the public failure event.
    Ok(json!({"runId":run,
        "publicEvent":{"type":"error","phase":node,"code":"AGENT_PROVIDER_REJECTED",
            "error":format!("上游已拒绝{role}请求（HTTP {status}）")},
        "providerFailure":{"schemaVersion":1,"httpStatus":status},
        "providerResponseSha256":response_sha256}))
}

/// Read ONLY the original immutable Rust outcome. The public error body must
/// exactly match its typed failure, rather than asserting failure from a label.
pub(crate) fn retained_failure(
    output: &Value,
    run: &str,
    node: &str,
    phase: i32,
) -> Result<Option<AgentProviderFailureMetadata>, Status> {
    let Some(value) = output.get("providerFailure") else {
        return if output["publicEvent"]["type"] == "error" {
            Err(denied())
        } else {
            Ok(None)
        };
    };
    let status = value["httpStatus"]
        .as_u64()
        .and_then(|n| u16::try_from(n).ok())
        .ok_or_else(denied)?;
    let response = output["providerResponseSha256"]
        .as_str()
        .ok_or_else(denied)?;
    if output != &rejection_envelope(run, node, phase, status, response)? {
        return Err(denied());
    }
    Ok(Some(AgentProviderFailureMetadata {
        schema_version: 1,
        http_status: status.into(),
        response_sha256: response.into(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_rejection_retains_only_complete_phase_specific_public_failure() {
        for (phase, node) in [(1, "leader"), (3, "synthesizer")] {
            for status in [400, 401, 402, 422, 429] {
                let body = rejection_envelope("run", node, phase, status, &"a".repeat(64)).unwrap();
                assert_eq!(body["publicEvent"]["type"], "error");
                assert_eq!(body["publicEvent"]["phase"], node);
                assert!(
                    body["publicEvent"]["error"]
                        .as_str()
                        .unwrap()
                        .contains(&status.to_string())
                );
                let metadata = retained_failure(&body, "run", node, phase)
                    .unwrap()
                    .unwrap();
                assert_eq!(metadata.schema_version, 1);
                assert_eq!(metadata.http_status, u32::from(status));
                assert_eq!(metadata.response_sha256, "a".repeat(64));
                assert!(body.get("usage").is_none());
                assert!(body.get("synthesisOutput").is_none());
                assert!(body["publicEvent"].get("providerFailure").is_none());
            }
        }
        assert!(
            retained_failure(
                &json!({"publicEvent":{"type":"content","text":"ok"}}),
                "run",
                "synthesizer",
                3
            )
            .unwrap()
            .is_none()
        );
        assert!(
            retained_failure(&json!({"publicEvent":{"type":"error"}}), "run", "leader", 1).is_err()
        );
    }

    #[test]
    fn terminal_failure_cannot_close_tasks_ambiguous_responses_or_rebound_scope() {
        for (run, node, phase, status, hash) in [
            ("", "leader", 1, 401, "a".repeat(64)),
            ("run", "coder", 2, 401, "a".repeat(64)),
            ("run", "synthesizer", 1, 401, "a".repeat(64)),
            ("run", "leader", 3, 401, "a".repeat(64)),
            ("run", "leader", 1, 200, "a".repeat(64)),
            ("run", "leader", 1, 307, "a".repeat(64)),
            ("run", "leader", 1, 404, "a".repeat(64)),
            ("run", "leader", 1, 503, "a".repeat(64)),
            ("run", "leader", 1, 401, "A".repeat(64)),
            ("run", "leader", 1, 401, "unconfirmed".into()),
        ] {
            assert!(rejection_envelope(run, node, phase, status, &hash).is_err());
        }
        let body = rejection_envelope("run", "leader", 1, 401, &"a".repeat(64)).unwrap();
        assert!(retained_failure(&body, "other", "leader", 1).is_err());
        assert!(retained_failure(&body, "run", "synthesizer", 3).is_err());
        for key in [
            "error",
            "apiKey",
            "providerBody",
            "usage",
            "synthesisOutput",
        ] {
            let mut altered = body.clone();
            altered[key] = json!("private or unproved");
            assert!(retained_failure(&altered, "run", "leader", 1).is_err());
        }
        for path in ["schemaVersion", "httpStatus", "unexpected"] {
            let mut altered = body.clone();
            altered["providerFailure"][path] = json!(503);
            assert!(retained_failure(&altered, "run", "leader", 1).is_err());
        }
        let mut altered = body.clone();
        altered["publicEvent"]["error"] = json!("private provider error");
        assert!(retained_failure(&altered, "run", "leader", 1).is_err());
    }
}
