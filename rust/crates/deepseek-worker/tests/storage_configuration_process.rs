//! Invalid deployment configuration must fail before opening any durable store.
#![cfg(feature = "s3")]
use std::process::{Command, Stdio};

#[test]
fn incomplete_storage_configuration_exits_without_creating_state_or_disclosing_secrets() {
    let root = tempfile::tempdir().unwrap();
    let state = root.path().join("must-not-be-created");
    let mut command = Command::new(env!("CARGO_BIN_EXE_deepseek-worker"));
    command.env_clear();
    // Preserve only loader paths and the offline instrumentation destination.
    // Dropping LLVM_PROFILE_FILE makes an instrumented child write into source.
    for name in [
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "LLVM_PROFILE_FILE",
    ] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
        .env("DEEPSEEK_WORKER_STATE_ROOT", &state)
        .env("DEEPSEEK_WORKER_S3_SECRET_KEY", "sensitive-fixture-value")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let output = command.output().unwrap();
    assert!(!output.status.success());
    assert!(!state.exists());
    let diagnostics = String::from_utf8(output.stderr).unwrap();
    assert!(diagnostics.contains("DEEPSEEK_WORKER_S3_ENDPOINT"));
    assert!(!diagnostics.contains("sensitive-fixture-value"));
    assert!(output.stdout.is_empty());
}
