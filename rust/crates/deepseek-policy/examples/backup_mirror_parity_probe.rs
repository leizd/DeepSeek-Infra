//! Sealed frontend mirror parity probe, Rust side.
//!
//! This probe is driven by its Python partner
//! (`tasks/native-runtime/backup_mirror_parity_probe.py --rust-example <this>`), which
//! owns the fixture: it generates the recipient identity, writes the envelope to a file
//! and passes both in. That is deliberate — a probe where each side invents its own
//! fixture proves the two fixtures agree, not that the implementations do.
//!
//! Two modes:
//!
//! * `--read <root>` prints a canonical report for every profile in the mirror root:
//!   the `list_mirrors` value, each profile's `mirror_status`, and the resolved
//!   ciphertext's SHA-256. The Python side runs the oracle over the *same* directory and
//!   compares.
//! * `--write ...` seals a generation with fixed inputs and prints the metadata it
//!   published. The Python side then (a) compares its own byte-for-byte metadata and
//!   `HEAD.json` for the same inputs, and (b) reads the Rust-written directory back with
//!   the oracle and decrypts the ciphertext with the recipient identity.
//!
//! Usage::
//!
//!     python tasks/native-runtime/backup_mirror_parity_probe.py \
//!         --rust-example rust/target/debug/examples/backup_mirror_parity_probe.exe

use std::path::PathBuf;
use std::process::ExitCode;

use deepseek_policy::backup_mirror::{MirrorStore, PutRequest, RecipientGroups, sha256_file};
use serde_json::{Map, Value, json};

fn argument(args: &[String], flag: &str) -> Option<String> {
    let position = args.iter().position(|value| value == flag)?;
    args.get(position + 1).cloned()
}

fn value_of(metadata: &Value, key: &str) -> Value {
    metadata.get(key).cloned().unwrap_or(Value::Null)
}

/// Mask the three genuinely volatile parts of a mirror file so the two sides' bytes can
/// be compared: the random generation id, the write clock, and every SHA-256 (age
/// ciphertext is randomized by contract, so the *ciphertext* hash cannot agree even when
/// the sealing is identical — the plaintext hash can, and that is compared separately).
fn mask(text: &str, generation: &str, created: &str) -> String {
    let mut masked = text.replace(generation, "<generation>");
    if !created.is_empty() {
        masked = masked.replace(created, "<created>");
    }
    let mut out = String::with_capacity(masked.len());
    let bytes: Vec<char> = masked.chars().collect();
    let mut index = 0;
    while index < bytes.len() {
        let run = bytes[index..]
            .iter()
            .take_while(|character| {
                character.is_ascii_hexdigit() && !character.is_ascii_uppercase()
            })
            .count();
        if run >= 64 {
            out.push_str("<sha256>");
            index += run;
            continue;
        }
        out.push(bytes[index]);
        index += 1;
    }
    out
}

fn report_read(root: &PathBuf, out: &mut Map<String, Value>) {
    let store = MirrorStore::new(root);
    let mirrors = store.list().unwrap_or_default();
    let listed: Vec<Value> = mirrors
        .iter()
        .map(|metadata| {
            let profile = metadata
                .get("profileId")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            json!({
                "profileId": profile,
                "generationId": value_of(metadata, "generationId"),
                "parentGenerationId": value_of(metadata, "parentGenerationId"),
                "sourceEpoch": value_of(metadata, "sourceEpoch"),
                "clientSequence": value_of(metadata, "clientSequence"),
                "clientReplicaId": value_of(metadata, "clientReplicaId"),
                "envelopeDigest": value_of(metadata, "envelopeDigest"),
                "recipientSetDigest": value_of(metadata, "recipientSetDigest"),
                "recipientVariantCount": metadata
                    .get("recipientVariants")
                    .and_then(Value::as_array)
                    .map(Vec::len)
                    .unwrap_or(0),
                "recipientVariants": metadata.get("recipientVariants").cloned().unwrap_or(Value::Null),
                "conversations": value_of(metadata, "conversations"),
                "conflicts": value_of(metadata, "conflicts"),
                "createdAt": value_of(metadata, "createdAt"),
                "acknowledgedAt": value_of(metadata, "acknowledgedAt"),
                "creationVerified": value_of(metadata, "creationVerified"),
                "ciphertextSha256": value_of(metadata, "ciphertextSha256"),
                "schemaVersion": value_of(metadata, "schemaVersion"),
            })
        })
        .collect();
    out.insert("mirrors".into(), Value::Array(listed));

    // `mirror_status` for each profile, without an expected epoch or recipient set: the
    // probe's recipient is passed separately for the recipient-mismatch case.
    let mut statuses = Map::new();
    if let Ok(read) = std::fs::read_dir(root) {
        let mut names: Vec<String> = read
            .flatten()
            .filter(|entry| entry.path().is_dir())
            .map(|entry| entry.file_name().to_string_lossy().to_string())
            .collect();
        names.sort();
        for name in names {
            if name == "previous" {
                continue;
            }
            let status = store
                .status(Some(&name), None, None, None, false, None)
                .unwrap_or_else(|error| json!({"error": error.message, "code": error.code}));
            statuses.insert(name, status);
        }
    }
    out.insert("statuses".into(), Value::Object(statuses));

    // The resolved ciphertext, by digest. This is what a restore copies.
    let mut files = Map::new();
    let profiles: Vec<String> = out
        .get("mirrors")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|item| {
                    item.get("profileId")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string()
                })
                .collect()
        })
        .unwrap_or_default();
    for profile in profiles {
        let entry = match store.files(&profile, None) {
            Ok(files) => json!({
                "ciphertextSha256": sha256_file(&files.ciphertext).unwrap_or_default(),
                "metadataSha256": sha256_file(&files.metadata_path).unwrap_or_default(),
                "schemaVersion": files.metadata.get("schemaVersion").cloned().unwrap_or(Value::Null),
            }),
            Err(error) => {
                json!({"error": error.message, "code": error.code, "status": error.status})
            }
        };
        files.insert(profile, entry);
    }
    out.insert("files".into(), Value::Object(files));

    // The head bytes, so the byte-level claim covers HEAD.json as well as metadata.json.
    let mut heads = Map::new();
    if let Ok(read) = std::fs::read_dir(root) {
        let mut names: Vec<String> = read
            .flatten()
            .filter(|entry| entry.path().is_dir())
            .map(|entry| entry.file_name().to_string_lossy().to_string())
            .collect();
        names.sort();
        for name in names {
            let path = store.head_path(&name);
            let raw = std::fs::read_to_string(&path).unwrap_or_default();
            let mut parsed: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
            // The generation id and the update time are the only volatile fields; mask
            // them so the two sides' HEAD files are comparable.
            if let Value::Object(fields) = &mut parsed {
                fields.insert("generationId".into(), json!("<generation>"));
                fields.insert("updatedAt".into(), json!("<updated>"));
            }
            heads.insert(
                name,
                json!({
                    "normalised": parsed,
                    "rawSha256": sha256_file(&path).unwrap_or_default(),
                }),
            );
        }
    }
    out.insert("heads".into(), Value::Object(heads));
}

fn report_write(args: &[String], out: &mut Map<String, Value>) -> Result<(), String> {
    let root = argument(args, "--root").ok_or("--root is required")?;
    let profile = argument(args, "--profile").ok_or("--profile is required")?;
    let epoch = argument(args, "--epoch").ok_or("--epoch is required")?;
    let sequence: i64 = argument(args, "--sequence")
        .unwrap_or_else(|| "0".to_string())
        .parse()
        .map_err(|_| "--sequence must be an integer")?;
    let recipient = argument(args, "--recipient").ok_or("--recipient is required")?;
    let envelope_path = argument(args, "--envelope").ok_or("--envelope is required")?;
    let acknowledged_at = argument(args, "--acknowledged-at");
    let now = argument(args, "--now").ok_or("--now is required")?;
    let replica = argument(args, "--replica").unwrap_or_else(|| "probe-replica".to_string());

    let envelope: Value = serde_json::from_str(
        &std::fs::read_to_string(&envelope_path).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    let parsed_now =
        deepseek_policy::backup_mirror::parse_iso(&now, "now").map_err(|error| error.message)?;
    let store = MirrorStore::new(&root);
    let outcome = store
        .put(
            PutRequest {
                profile_id: profile.clone(),
                envelope,
                source_epoch: epoch,
                recipients: RecipientGroups::Explicit(vec![recipient.clone()]),
                acknowledged_at,
                client_replica_id: replica,
                client_sequence: sequence,
                expected_head_generation_id: None,
                now: Some(parsed_now),
            },
            false,
        )
        .map_err(|error| error.message)?;

    let metadata = outcome.metadata;
    let generation = metadata
        .get("generationId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let generation_dir = store.generation_dir(&profile, &generation);
    let mut normalised = metadata.clone();
    if let Value::Object(fields) = &mut normalised {
        fields.insert("generationId".into(), json!("<generation>"));
        fields.insert("createdAt".into(), json!("<created>"));
    }
    out.insert("idempotent".into(), json!(outcome.idempotent));
    let created = metadata
        .get("createdAt")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    out.insert("metadata".into(), metadata);
    out.insert("metadataNormalised".into(), normalised);
    out.insert(
        "metadataBytesNormalised".into(),
        json!(mask(
            &std::fs::read_to_string(generation_dir.join("metadata.json")).unwrap_or_default(),
            &generation,
            &created
        )),
    );
    let head_raw = std::fs::read_to_string(store.head_path(&profile)).unwrap_or_default();
    out.insert(
        "headBytesNormalised".into(),
        json!(mask(&head_raw, &generation, &created)),
    );
    out.insert(
        "generationDir".into(),
        json!(generation_dir.to_string_lossy().to_string()),
    );
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut out = Map::new();
    let result = if args.iter().any(|value| value == "--read") {
        let Some(root) = argument(&args, "--read") else {
            eprintln!("--read needs a directory");
            return ExitCode::FAILURE;
        };
        report_read(&PathBuf::from(root), &mut out);
        Ok(())
    } else if args.iter().any(|value| value == "--write") {
        report_write(&args, &mut out)
    } else {
        eprintln!("usage: backup_mirror_parity_probe --read <root> | --write --root <root> ...");
        return ExitCode::FAILURE;
    };
    if let Err(error) = result {
        eprintln!("backup_mirror_parity_probe: {error}");
        return ExitCode::FAILURE;
    }
    println!(
        "{}",
        deepseek_policy::backup_mirror::dumps_indent_two(&Value::Object(out))
    );
    ExitCode::SUCCESS
}
