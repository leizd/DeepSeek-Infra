//! Default-launch custody provisioning. Private bytes are created/read only by Rust.
//! The Go supervisor sees public X.509 transport certificates and the typed
//! GetBinding RPC. This module never initializes a Go store or adopts legacy data.
use deepseek_federation::{create_control_signer_bundle, load_control_signer};
use prost::Message as _;
use rcgen::{
    BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, KeyPair, KeyUsagePurpose,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    env::VarError,
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
};
use zeroize::Zeroizing;

const ROOT: &str = "DEEPSEEK_WORKER_MANAGED_ROOT";
const ID: &str = "DEEPSEEK_WORKER_MANAGED_PROFILE_ID";
const INIT: &str = "DEEPSEEK_WORKER_MANAGED_INITIALIZE";
const FENCE: &str = "DEEPSEEK_WORKER_MANAGED_FENCING_TOKEN";
const FILES: [&str; 7] = [
    "transport-ca.pem",
    "transport-ca-key.pem",
    "worker-cert.pem",
    "worker-key.pem",
    "control-client.pem",
    "control-client-key.pem",
    "signer.encrypted.json",
];
const PASSWORD: &str = "signer.passphrase";

pub struct ManagedConfiguration {
    pub initialize_only: bool,
    pub values: HashMap<String, String>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema: String,
    profile_id: String,
    fleet_id: String,
    environment: String,
    signer_public_key: String,
    files: HashMap<String, String>,
}
fn denied() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "managed Rust custody is missing, partial or inconsistent",
    )
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub(crate) fn read(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    let info = fs::symlink_metadata(path)?;
    if !info.is_file() || info.len() == 0 || info.len() > limit {
        return Err(denied());
    }
    let bytes = fs::read(path)?;
    if bytes.is_empty() || bytes.len() as u64 > limit {
        return Err(denied());
    }
    Ok(bytes)
}
pub(crate) fn publish(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}
fn directory(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)
}
pub(crate) fn sync_directory(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        fs::File::open(path)?.sync_all()?;
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
    Ok(())
}
fn validate(root: &Path, id: &str) -> io::Result<Manifest> {
    let custody = root.join("custody");
    if !fs::symlink_metadata(&custody)?.is_dir() {
        return Err(denied());
    }
    let manifest: Manifest =
        serde_json::from_slice(&read(&custody.join("manifest.json"), 64 * 1024)?)
            .map_err(|_| denied())?;
    let native = manifest.schema == "native-managed-rust-custody-v2";
    if (!native && manifest.schema != "native-managed-rust-custody-v1")
        || manifest.profile_id != id
        || manifest.fleet_id != format!("native-{id}")
        || manifest.environment != "local"
        || manifest.files.len() != FILES.len() + 1 + usize::from(native)
    {
        return Err(denied());
    }
    for name in FILES.into_iter().chain([PASSWORD]) {
        let bytes = Zeroizing::new(read(&custody.join(name), 128 * 1024)?);
        if manifest.files.get(name) != Some(&digest(&bytes)) {
            return Err(denied());
        }
    }
    if native {
        let bytes = read(&custody.join("native-origin.pb"), 16 * 1024)?;
        if manifest.files.get("native-origin.pb") != Some(&digest(&bytes)) {
            return Err(denied());
        }
        let origin = crate::native_origin::decode_origin(&bytes)?;
        let evidence = origin.evidence.as_ref().ok_or_else(denied)?;
        if evidence.profile_id != id || evidence.signer_public_key != manifest.signer_public_key {
            return Err(denied());
        }
    }
    let password = Zeroizing::new(read(&custody.join(PASSWORD), 1024)?);
    let bundle = serde_json::from_slice(&read(&custody.join("signer.encrypted.json"), 128 * 1024)?)
        .map_err(|_| denied())?;
    load_control_signer(
        &bundle,
        &password,
        &manifest.fleet_id,
        &manifest.environment,
        &manifest.signer_public_key,
    )
    .map_err(|_| denied())?;
    Ok(manifest)
}
fn initialize(root: &Path, id: &str) -> io::Result<()> {
    if fs::symlink_metadata(root.join("custody")).is_ok() {
        validate(root, id)?;
        return Ok(());
    }
    // A lost bundle must never be regenerated over existing journals or effects.
    if fs::read_dir(root)?.next().is_some() {
        return Err(denied());
    }
    let staging = root.join(".custody-initializing");
    directory(&staging)?;
    let mut ca_params = CertificateParams::new(vec!["deepseek-native-transport.local".into()])
        .map_err(|_| denied())?;
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    ca_params.key_usages = vec![
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
        KeyUsagePurpose::DigitalSignature,
    ];
    let ca_key = KeyPair::generate().map_err(|_| denied())?;
    let ca = ca_params.self_signed(&ca_key).map_err(|_| denied())?;
    let mut values: HashMap<&str, Zeroizing<Vec<u8>>> = HashMap::new();
    values.insert("transport-ca.pem", Zeroizing::new(ca.pem().into_bytes()));
    values.insert(
        "transport-ca-key.pem",
        Zeroizing::new(ca_key.serialize_pem().into_bytes()),
    );
    for (cert_name, key_name, hostname, purpose) in [
        (
            "worker-cert.pem",
            "worker-key.pem",
            "deepseek-worker.local",
            ExtendedKeyUsagePurpose::ServerAuth,
        ),
        (
            "control-client.pem",
            "control-client-key.pem",
            "deepseek-rust.local",
            ExtendedKeyUsagePurpose::ClientAuth,
        ),
    ] {
        let key = KeyPair::generate().map_err(|_| denied())?;
        let mut params = CertificateParams::new(vec![hostname.into()]).map_err(|_| denied())?;
        params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        params.extended_key_usages = vec![purpose];
        let cert = params.signed_by(&key, &ca, &ca_key).map_err(|_| denied())?;
        values.insert(cert_name, Zeroizing::new(cert.pem().into_bytes()));
        values.insert(key_name, Zeroizing::new(key.serialize_pem().into_bytes()));
    }
    // Independent CSPRNG material, never a Go-generated signing seed.
    let password_key = KeyPair::generate().map_err(|_| denied())?;
    let password_material = Zeroizing::new(password_key.serialize_der());
    let password = Zeroizing::new(digest(&password_material).into_bytes());
    let fleet = format!("native-{id}");
    let bundle = create_control_signer_bundle(&password, &fleet, "local").map_err(|_| denied())?;
    let public = bundle["binding"]["signerPublicKey"]
        .as_str()
        .ok_or_else(denied)?
        .to_owned();
    values.insert(
        "signer.encrypted.json",
        Zeroizing::new(serde_json::to_vec(&bundle).map_err(|_| denied())?),
    );
    let key =
        load_control_signer(&bundle, &password, &fleet, "local", &public).map_err(|_| denied())?;
    let incarnation = digest(&KeyPair::generate().map_err(|_| denied())?.public_key_der());
    let created_at = time::OffsetDateTime::now_utc()
        .replace_nanosecond(0)
        .map_err(|_| denied())?
        .format(&time::format_description::well_known::Rfc3339)
        .map_err(|_| denied())?;
    let origin = key
        .create_native_origin(id, &incarnation, &created_at)
        .map_err(|_| denied())?;
    values.insert("native-origin.pb", Zeroizing::new(origin.encode_to_vec()));
    values.insert(PASSWORD, password);
    let mut hashes = HashMap::new();
    for (name, bytes) in &values {
        publish(&staging.join(name), bytes)?;
        hashes.insert((*name).to_owned(), digest(bytes));
    }
    let manifest = Manifest {
        schema: "native-managed-rust-custody-v2".into(),
        profile_id: id.into(),
        fleet_id: fleet,
        environment: "local".into(),
        signer_public_key: public,
        files: hashes,
    };
    publish(
        &staging.join("manifest.json"),
        &serde_json::to_vec(&manifest).map_err(|_| denied())?,
    )?;
    sync_directory(&staging)?;
    fs::rename(&staging, root.join("custody"))?;
    sync_directory(root)?;
    validate(root, id)?;
    Ok(())
}

pub fn load(get: impl Fn(&str) -> Result<String, VarError>) -> io::Result<ManagedConfiguration> {
    let absent = ManagedConfiguration {
        initialize_only: false,
        values: HashMap::new(),
    };
    let raw = match get(ROOT) {
        Ok(value) => value,
        Err(VarError::NotPresent) => {
            if [ID, INIT, FENCE]
                .iter()
                .any(|name| !matches!(get(name), Err(VarError::NotPresent)))
            {
                return Err(denied());
            }
            return Ok(absent);
        }
        Err(_) => return Err(denied()),
    };
    let root = PathBuf::from(raw);
    let id = get(ID).map_err(|_| denied())?;
    if !root.is_absolute()
        || id.len() != 32
        || !id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(denied());
    }
    if !fs::symlink_metadata(&root)?.is_dir()
        || fs::canonicalize(&root)?
            != fs::canonicalize(root.parent().ok_or_else(denied)?)?
                .join(root.file_name().ok_or_else(denied)?)
    {
        return Err(denied());
    }
    match get(INIT) {
        Ok(value) if value == "1" => {
            initialize(&root, &id)?;
            return Ok(ManagedConfiguration {
                initialize_only: true,
                values: HashMap::new(),
            });
        }
        Err(VarError::NotPresent) => {}
        _ => return Err(denied()),
    }
    let manifest = validate(&root, &id)?;
    let fence = get(FENCE)
        .map_err(|_| denied())?
        .parse::<i64>()
        .map_err(|_| denied())?;
    if fence <= 0 {
        return Err(denied());
    }
    let custody = root.join("custody");
    let values = [
        (
            crate::control_signer::BUNDLE_FILE,
            custody
                .join("signer.encrypted.json")
                .to_string_lossy()
                .into_owned(),
        ),
        (
            crate::control_signer::PASSPHRASE_FILE,
            custody.join(PASSWORD).to_string_lossy().into_owned(),
        ),
        (
            crate::transport::WORKER_TLS_CERT_FILE,
            custody
                .join("worker-cert.pem")
                .to_string_lossy()
                .into_owned(),
        ),
        (
            crate::transport::WORKER_TLS_KEY_FILE,
            custody
                .join("worker-key.pem")
                .to_string_lossy()
                .into_owned(),
        ),
        (
            "DEEPSEEK_WORKER_STATE_ROOT",
            root.join("state").to_string_lossy().into_owned(),
        ),
        (
            "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
            manifest.signer_public_key,
        ),
        ("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", manifest.fleet_id),
        (
            "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT",
            manifest.environment,
        ),
        ("DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN", fence.to_string()),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value))
    .collect();
    Ok(ManagedConfiguration {
        initialize_only: false,
        values,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    const PROFILE: &str = "0123456789abcdef0123456789abcdef";
    fn env(root: &Path, init: bool) -> HashMap<String, String> {
        let mut values = HashMap::from([
            (ROOT.into(), root.to_string_lossy().into_owned()),
            (ID.into(), PROFILE.into()),
            (FENCE.into(), "1".into()),
        ]);
        if init {
            values.insert(INIT.into(), "1".into());
        }
        values
    }
    fn call(values: &HashMap<String, String>) -> io::Result<ManagedConfiguration> {
        load(|n| values.get(n).cloned().ok_or(VarError::NotPresent))
    }
    #[test]
    fn initialize_and_reopen_never_replaces_custody() {
        let root = tempfile::tempdir().unwrap();
        let values = env(root.path(), true);
        assert!(call(&values).unwrap().initialize_only);
        let original = read(
            &root.path().join("custody/signer.encrypted.json"),
            128 * 1024,
        )
        .unwrap();
        assert!(call(&values).unwrap().initialize_only);
        assert_eq!(
            digest(&original),
            digest(
                &read(
                    &root.path().join("custody/signer.encrypted.json"),
                    128 * 1024
                )
                .unwrap()
            )
        );
        let config = call(&env(root.path(), false)).unwrap();
        assert!(!config.initialize_only);
        assert_eq!(
            config.values["DEEPSEEK_WORKER_AUTHORITY_FLEET_ID"],
            format!("native-{PROFILE}")
        );
        assert_eq!(
            config.values["DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN"],
            "1"
        );
        let mut restarted = env(root.path(), false);
        restarted.insert(FENCE.into(), "2".into());
        assert_eq!(
            call(&restarted).unwrap().values["DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN"],
            "2"
        );
        assert!(!root.path().join("state").exists());
    }
    #[test]
    fn native_birth_is_sealed_once_and_old_custody_never_gains_it_on_upgrade() {
        let root = tempfile::tempdir().unwrap();
        call(&env(root.path(), true)).unwrap();
        let birth = read(&root.path().join("custody/native-origin.pb"), 16384).unwrap();
        let original = crate::native_origin::decode_origin(&birth).unwrap();
        assert_eq!(original.evidence.unwrap().profile_id, PROFILE);
        call(&env(root.path(), true)).unwrap();
        assert_eq!(
            read(&root.path().join("custody/native-origin.pb"), 16384).unwrap(),
            birth
        );
        // Materialize the actual older manifest shape, not an absent v2 proof.
        let manifest_path = root.path().join("custody/manifest.json");
        let mut manifest: Manifest =
            serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
        manifest.schema = "native-managed-rust-custody-v1".into();
        manifest.files.remove("native-origin.pb");
        fs::remove_file(root.path().join("custody/native-origin.pb")).unwrap();
        fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        call(&env(root.path(), true)).unwrap();
        call(&env(root.path(), false)).unwrap();
        assert!(!root.path().join("custody/native-origin.pb").exists());
    }
    #[test]
    fn missing_or_partial_custody_is_never_regenerated() {
        let root = tempfile::tempdir().unwrap();
        assert!(call(&env(root.path(), false)).is_err());
        fs::create_dir(root.path().join("state")).unwrap();
        fs::write(root.path().join("state/retained-effect"), b"EFFECT_UNKNOWN").unwrap();
        assert!(call(&env(root.path(), true)).is_err());
        assert!(!root.path().join("custody").exists());
        let root = tempfile::tempdir().unwrap();
        directory(&root.path().join(".custody-initializing")).unwrap();
        assert!(call(&env(root.path(), true)).is_err());
    }
    #[test]
    fn foreign_scope_and_changed_files_fail_closed() {
        let root = tempfile::tempdir().unwrap();
        call(&env(root.path(), true)).unwrap();
        for (name, value) in [
            (ID, "foreign"),
            (ID, "ffffffffffffffffffffffffffffffff"),
            (FENCE, "0"),
            (FENCE, "-1"),
            (INIT, "false"),
        ] {
            let mut values = env(root.path(), false);
            values.insert(name.into(), value.into());
            assert!(call(&values).is_err());
        }
        fs::write(root.path().join("custody/worker-cert.pem"), b"tampered").unwrap();
        assert!(call(&env(root.path(), false)).is_err());
        assert!(call(&env(root.path(), true)).is_err());
    }
    #[test]
    fn absent_managed_settings_preserve_operator_deployments() {
        assert!(call(&HashMap::new()).unwrap().values.is_empty());
        assert!(call(&HashMap::from([(FENCE.into(), "1".into())])).is_err());
        let mut values = HashMap::from([
            (ROOT.into(), "relative".into()),
            (ID.into(), PROFILE.into()),
        ]);
        assert!(call(&values).is_err());
        values.insert(ROOT.into(), "".into());
        assert!(call(&values).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn symlinks_cannot_redirect_managed_custody() {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().unwrap();
        let other = tempfile::tempdir().unwrap();
        symlink(other.path(), root.path().join("custody")).unwrap();
        assert!(call(&env(root.path(), true)).is_err());
        assert!(fs::read_dir(other.path()).unwrap().next().is_none());
    }
}
