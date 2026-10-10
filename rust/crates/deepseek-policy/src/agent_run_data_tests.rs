use super::*;
use rusqlite::Connection;

#[test]
fn reopened_store_retains_sanitized_immutable_bodies() {
    let root = tempfile::tempdir().unwrap();
    let payload = json!({"model":"deepseek-v4-pro","apiKey":"fixture-credential",
        "messages":[{"content":"preserved","tavilyApiKey":"fixture-search-credential"}],
        "nested":{"apiKey":"fixture-nested","extension":true}});
    let mut store = ArtifactStore::open_native(root.path(), "python_disabled").unwrap();
    let reference = store.put(&payload).unwrap();
    assert_eq!(reference.sha256.len(), 64);
    assert!(reference.length > 0);
    assert_eq!(store.put(&payload).unwrap(), reference);
    drop(store);
    let mut reopened = ArtifactStore::open_readonly(root.path()).unwrap();
    assert_eq!(
        reopened.get(&reference).unwrap(),
        json!({"model":"deepseek-v4-pro",
        "messages":[{"content":"preserved"}],"nested":{"extension":true}})
    );
    assert_eq!(reopened.put(&payload), Err(DataError::NotOwned));
    let connection = Connection::open(root.path().join("objects-v1.sqlite")).unwrap();
    let count: i64 = connection
        .query_row("SELECT count(*) FROM artifacts", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1);
    assert!(
        connection
            .execute("UPDATE artifacts SET bytes=bytes", [])
            .is_err()
    );
    assert!(connection.execute("DELETE FROM artifacts", []).is_err());
}

#[test]
fn legacy_modes_and_readonly_paths_never_create_a_store() {
    let root = tempfile::tempdir().unwrap();
    let absent = root.path().join("absent");
    for mode in ["", "shadow", "authoritative", "python"] {
        assert!(matches!(
            ArtifactStore::open_native(&absent, mode),
            Err(DataError::NotOwned)
        ));
        assert!(!absent.exists());
    }
    assert!(matches!(
        ArtifactStore::open_readonly(&absent),
        Err(DataError::Unavailable)
    ));
    assert!(!absent.exists());
}

#[cfg(unix)]
#[test]
fn new_store_permissions_keep_event_bodies_private() {
    use std::os::unix::fs::PermissionsExt;
    let root = tempfile::tempdir().unwrap();
    let directory = root.path().join("private-store");
    let mut store = ArtifactStore::open_native(&directory, "python_disabled").unwrap();
    store
        .put(&json!({"type":"content","text":"private body"}))
        .unwrap();
    assert_eq!(
        std::fs::metadata(&directory).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        std::fs::metadata(directory.join("objects-v1.sqlite"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

#[test]
fn mismatched_length_digest_and_missing_body_fail_closed() {
    let root = tempfile::tempdir().unwrap();
    let mut store = ArtifactStore::open_native(root.path(), "python_disabled").unwrap();
    let reference = store
        .put(&json!({"type":"content","text":"answer"}))
        .unwrap();
    let mut wrong_length = reference.clone();
    wrong_length.length += 1;
    assert_eq!(store.get(&wrong_length), Err(DataError::Corrupt));
    let absent = ArtifactReference {
        sha256: "0".repeat(64),
        length: 1,
    };
    assert_eq!(store.get(&absent), Err(DataError::Unavailable));
    let invalid = ArtifactReference {
        sha256: "../outside".into(),
        length: 1,
    };
    assert_eq!(store.get(&invalid), Err(DataError::Invalid));
    let connection = Connection::open(root.path().join("objects-v1.sqlite")).unwrap();
    connection
        .execute_batch("DROP TRIGGER artifacts_no_update;")
        .unwrap();
    let altered = serde_json::to_vec(&json!({"type":"content","text":"wrong!"})).unwrap();
    assert_eq!(altered.len() as u64, reference.length);
    connection
        .execute("UPDATE artifacts SET bytes=?1", [altered])
        .unwrap();
    assert_eq!(store.get(&reference), Err(DataError::Corrupt));
    assert_eq!(
        store.put(&json!({"type":"content","text":"answer"})),
        Err(DataError::Corrupt)
    );
}

#[test]
fn unrelated_sqlite_is_never_adopted() {
    let root = tempfile::tempdir().unwrap();
    let connection = Connection::open(root.path().join("objects-v1.sqlite")).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE personal_data(value TEXT); INSERT INTO personal_data VALUES('preserve');",
        )
        .unwrap();
    drop(connection);
    assert!(matches!(
        ArtifactStore::open_native(root.path(), "python_disabled"),
        Err(DataError::Corrupt)
    ));
    assert!(matches!(
        ArtifactStore::open_readonly(root.path()),
        Err(DataError::Corrupt)
    ));
    let connection = Connection::open(root.path().join("objects-v1.sqlite")).unwrap();
    let tables: i64 = connection
        .query_row(
            "SELECT count(*) FROM sqlite_schema WHERE type='table'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(tables, 1);
    let value: String = connection
        .query_row("SELECT value FROM personal_data", [], |r| r.get(0))
        .unwrap();
    assert_eq!(value, "preserve");
}

#[cfg(unix)]
#[test]
fn symlinks_are_refused_without_touching_the_target() {
    let root = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let link = root.path().join("link");
    std::os::unix::fs::symlink(outside.path(), &link).unwrap();
    assert!(matches!(
        ArtifactStore::open_native(&link, "python_disabled"),
        Err(DataError::Invalid)
    ));
    assert!(!outside.path().join("objects-v1.sqlite").exists());
}

#[test]
fn projection_replays_original_python_content_at_every_prefix() {
    let corpus: Value =
        serde_json::from_str(include_str!("../testdata/agent_run_content_v1.json")).unwrap();
    let mut count = 0;
    for case in corpus["cases"].as_array().unwrap() {
        let mut projection = ContentProjection::default();
        for (index, checkpoint) in case["checkpoints"].as_array().unwrap().iter().enumerate() {
            projection.apply(&checkpoint["event"], &checkpoint["plan"]);
            assert_eq!(
                projection.public_value(),
                checkpoint["expected"],
                "case {} prefix {}",
                case["name"],
                index + 1
            );
            count += 1;
        }
    }
    assert!(count >= 1000);
}
