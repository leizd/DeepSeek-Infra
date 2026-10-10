//! Real Rust binary + loopback gRPC + forced process termination; no Python runtime.
use deepseek_protocol::generated::deepseek::{
    action::v1::{
        AdmitCommandRequest, AdmitStatus, CommandKind, InstallAuthoritativeEpochRequest,
        QueryEffectRequest, worker_client::WorkerClient,
    },
    common::v1::{ActionFence, EffectState},
};
use serde_json::Value;
use std::{
    net::TcpListener,
    path::Path,
    process::{Child, Command, Stdio},
    time::Duration,
};

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn launch(root: &Path, port: u16, fixture: &Value, with_root: bool) -> Process {
    let mut command = Command::new(env!("CARGO_BIN_EXE_deepseek-worker"));
    command
        .env("DEEPSEEK_WORKER_LISTEN", format!("127.0.0.1:{port}"))
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
            fixture["signer_public_key"].as_str().unwrap(),
        )
        .env("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", "fleet-a")
        .env("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", "test")
        .env("DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN", "4")
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_NOW",
            fixture["now"].as_str().unwrap(),
        )
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if with_root {
        command.env("DEEPSEEK_WORKER_STATE_ROOT", root);
    } else {
        command.env_remove("DEEPSEEK_WORKER_STATE_ROOT");
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    Process(command.spawn().unwrap())
}

async fn connect(process: &mut Process, port: u16) -> WorkerClient<tonic::transport::Channel> {
    for _ in 0..100 {
        assert!(
            process.0.try_wait().unwrap().is_none(),
            "worker exited before listening"
        );
        if let Ok(client) = WorkerClient::connect(format!("http://127.0.0.1:{port}")).await {
            return client;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("worker did not listen within 5 seconds");
}

fn fixture() -> Value {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
    )))
    .unwrap()
}

#[tokio::test]
async fn acknowledged_epoch_survives_forced_child_termination_and_grpc_restart() {
    let root = tempfile::tempdir().unwrap();
    let fixture = fixture();
    let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    drop(reservation);
    let mut process = launch(root.path(), port, &fixture, true);
    let first_pid = process.0.id();
    let mut client = connect(&mut process, port).await;
    let fence = ActionFence {
        action_id: "act-1".into(),
        execution_epoch: 4,
    };
    let request = InstallAuthoritativeEpochRequest {
        fence: Some(fence.clone()),
        canonical_request: fixture["canonical_request"]
            .as_str()
            .unwrap()
            .as_bytes()
            .to_vec(),
    };
    let accepted = client
        .install_authoritative_epoch(request.clone())
        .await
        .unwrap()
        .into_inner();
    assert_eq!(accepted.status(), AdmitStatus::Admitted);
    assert_eq!(accepted.fence, Some(fence.clone()));
    assert!(accepted.error.is_none());
    assert!(process.0.try_wait().unwrap().is_none());
    process.0.kill().unwrap();
    assert!(!process.0.wait().unwrap().success());
    drop(client);
    let mut restarted = launch(root.path(), port, &fixture, true);
    assert_ne!(first_pid, restarted.0.id());
    let mut client = connect(&mut restarted, port).await;
    let admitted = client
        .admit_command(AdmitCommandRequest {
            kind: CommandKind::ExecuteBackup as i32,
            fence: Some(fence.clone()),
            live_epoch: u64::MAX,
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(admitted.status(), AdmitStatus::Admitted);
    let replayed = client
        .install_authoritative_epoch(request)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(replayed.status(), AdmitStatus::Rejected);
    assert_eq!(replayed.error.unwrap().code, "STALE_EXECUTION_EPOCH");
    let effect = client
        .query_effect(QueryEffectRequest { fence: Some(fence) })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(effect.state(), EffectState::Unknown);
    assert_eq!(effect.error.unwrap().code, "EFFECT_UNKNOWN");
    assert!(effect.receipt_digest.is_empty());
}

#[test]
fn configured_binary_without_durable_root_exits_instead_of_using_memory() {
    let root = tempfile::tempdir().unwrap();
    let mut process = launch(root.path(), 50052, &fixture(), false);
    for _ in 0..100 {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(!status.success());
            assert!(!root.path().join("rust-worker").exists());
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("worker did not reject missing durable root");
}
