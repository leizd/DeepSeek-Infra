use deepseek_storage::{
    CommitV4, ObjectInventoryEntry, ObjectSet, ReceiptV4, validate_committed_documents,
};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct Corpus {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    objects: Vec<ObjectInventoryEntry>,
    commitment: String,
    object_set_digest: String,
    receipt: ReceiptV4,
    receipt_digest: String,
    commit: CommitV4,
}

#[test]
fn rust_replays_python_4_8_0_storage_semantics() {
    let corpus: Corpus = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v2/storage/receipt_commit_vectors.json"
    )))
    .unwrap();
    assert_eq!(corpus.schema_version, 1);
    assert_eq!(corpus.source_version, "4.8.0");
    assert_eq!(
        corpus.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );

    let case = corpus.cases.into_iter().next().unwrap();
    assert_eq!(case.name, "two_objects_unsorted_input");
    let object_set = ObjectSet::try_new(case.objects).unwrap();
    assert_eq!(object_set.commitment(), case.commitment.as_bytes());
    assert_eq!(object_set.compute_digest(), case.object_set_digest);

    case.receipt.validate().unwrap();
    let receipt_bytes = case.receipt.canonical_bytes().unwrap();
    assert_eq!(case.receipt.digest().unwrap(), case.receipt_digest);
    assert_eq!(case.commit.compute_hash().unwrap(), case.commit.commit_hash);
    case.commit
        .validate_against_receipt(&case.receipt, &receipt_bytes)
        .unwrap();
    let commit_bytes = case.commit.canonical_bytes().unwrap();
    validate_committed_documents(&receipt_bytes, &commit_bytes).unwrap();
}
