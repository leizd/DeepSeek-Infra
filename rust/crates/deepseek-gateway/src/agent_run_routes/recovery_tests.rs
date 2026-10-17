use super::*;
use axum::http::Request;
use tower::ServiceExt;

#[test]
fn recovery_selection_keeps_all_roles_aliases_and_literal_false_semantics() {
    for role in [
        "researcher",
        "coder",
        "reasoner",
        "critic",
        "synth",
        "leader",
        "synthesizer",
    ] {
        for value in [
            Value::Null,
            json!(true),
            json!(false),
            json!(0),
            json!("false"),
        ] {
            let input = json!({"agentId":format!(" {role} "),"resynthesize":value,"payload":{}});
            let selected = selection(Mode::Rerun, &input).unwrap();
            assert_eq!(selected.kind, AgentRunRecoveryKind::Rerun as i32);
            assert_eq!(selected.agent_id, role);
            assert_eq!(selected.resynthesize, Some(value != json!(false)));
        }
    }
    let resume = selection(Mode::Resume, &json!({"payload":{}})).unwrap();
    assert_eq!(resume.kind, AgentRunRecoveryKind::Resume as i32);
    assert!(resume.agent_id.is_empty() && resume.resynthesize.is_none());
}

#[test]
fn recovery_refuses_active_runs_and_resume_awaiting_confirmation() {
    for mode in [Mode::Resume, Mode::Rerun] {
        for status in ["created", "planning", "running"] {
            assert_eq!(
                check_status(mode, status).unwrap_err().status,
                StatusCode::CONFLICT
            );
        }
        for status in ["done", "failed", "cancelled", "orphaned"] {
            check_status(mode, status).unwrap();
        }
    }
    assert_eq!(
        check_status(Mode::Resume, "awaiting_plan")
            .unwrap_err()
            .status,
        StatusCode::CONFLICT
    );
    check_status(Mode::Rerun, "awaiting_plan").unwrap();
}

#[test]
fn recovery_rejects_missing_unknown_roles_and_recursive_private_bindings() {
    for value in [
        json!({}),
        json!({"agentId":" "}),
        json!({"agentId":7}),
        json!({"agentId":"unknown"}),
        json!({"agentId":"critic","payload":{"messages":[{"_nativeRecoveryContext":{}}]}}),
        json!({"agentId":"critic","payload":{"_nativeProviderCredentials":{}}}),
    ] {
        assert_eq!(
            selection(Mode::Rerun, &value).unwrap_err().status,
            StatusCode::BAD_REQUEST
        );
    }
    assert!(
        selection(
            Mode::Resume,
            &json!({"payload":{"_nativeRuntimePreparation":"caller"}})
        )
        .is_err()
    );
}

#[tokio::test]
async fn public_recovery_routes_exist_and_refuse_missing_control_authority() {
    for (path, body) in [
        ("resume", json!({"payload":{}})),
        ("rerun", json!({"agentId":"critic","payload":{}})),
    ] {
        let response = super::super::read_router(true, None)
            .oneshot(
                Request::post(format!(
                    "/api/agent-runs/run_gateway_recovery_fixture/{path}"
                ))
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }
}

#[test]
fn recovery_rpc_errors_preserve_conflict_not_found_and_unavailable_boundaries() {
    for code in [
        tonic::Code::FailedPrecondition,
        tonic::Code::Aborted,
        tonic::Code::AlreadyExists,
    ] {
        assert_eq!(
            recovery_error(tonic::Status::new(code, "private detail")).status,
            StatusCode::CONFLICT
        );
    }
    assert_eq!(
        recovery_error(tonic::Status::not_found("private detail")).status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        recovery_error(tonic::Status::invalid_argument("private detail")).status,
        StatusCode::BAD_REQUEST
    );
    for code in [
        tonic::Code::Unauthenticated,
        tonic::Code::PermissionDenied,
        tonic::Code::Unavailable,
    ] {
        let error = recovery_error(tonic::Status::new(code, "private detail"));
        assert_eq!(error.status, StatusCode::SERVICE_UNAVAILABLE);
        assert!(!error.message.contains("private detail"));
    }
}
