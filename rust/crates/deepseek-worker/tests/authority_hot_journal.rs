//! Crash fault injection into SQLite, not manufactured provider execution evidence.
use deepseek_protocol::ActionFence;
use deepseek_worker::{Worker, WorkerAuthorityConfig};
use serde_json::Value;
use std::{
    path::Path,
    process::{Command, Stdio},
    time::Duration,
};

#[test]
fn interrupted_transaction_child() {
    let Some(directory) = std::env::var_os("DEEPSEEK_TEST_HOT_JOURNAL_ROOT") else {
        return;
    };
    let directory = Path::new(&directory);
    let connection =
        rusqlite::Connection::open(directory.join("rust-worker/authority.sqlite3")).unwrap();
    // Force dirty-page spill so the killed process leaves a genuinely hot journal.
    connection.execute_batch("PRAGMA cache_size=1; BEGIN IMMEDIATE; UPDATE worker_authority SET fencing_token=5 WHERE id=1; CREATE TABLE uncommitted_padding (data BLOB); INSERT INTO uncommitted_padding VALUES (zeroblob(262144))").unwrap();
    std::fs::write(directory.join("ready"), b"uncommitted transaction").unwrap();
    loop {
        std::thread::sleep(Duration::from_secs(1));
    }
}

#[test]
fn killed_uncommitted_writer_recovers_committed_epoch_and_owner() {
    let directory = tempfile::tempdir().unwrap();
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
    )))
    .unwrap();
    let config = WorkerAuthorityConfig {
        signer_public_key: fixture["signer_public_key"].as_str().unwrap().into(),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        fencing_token: 4,
        now: Some(fixture["now"].as_str().unwrap().into()),
    };
    let fence = ActionFence {
        action_id: "act-1".into(),
        execution_epoch: 4,
    };
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    worker
        .install_signed_epoch(
            &fence,
            fixture["canonical_request"].as_str().unwrap().as_bytes(),
        )
        .unwrap();
    drop(worker);
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "interrupted_transaction_child", "--nocapture"])
        .env("DEEPSEEK_TEST_HOT_JOURNAL_ROOT", directory.path())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().unwrap();
    let mut ready = false;
    for _ in 0..100 {
        if directory.path().join("ready").exists() {
            ready = true;
            break;
        }
        if child.try_wait().unwrap().is_some() {
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    let status = child.wait().unwrap();
    assert!(ready, "child failed to reach uncommitted transaction");
    assert!(!status.success());
    assert!(
        directory
            .path()
            .join("rust-worker/authority.sqlite3-journal")
            .exists()
    );
    let recovered = Worker::open_with_authority(config, directory.path()).unwrap();
    assert_eq!(recovered.admit(&fence), Ok(()));
}
