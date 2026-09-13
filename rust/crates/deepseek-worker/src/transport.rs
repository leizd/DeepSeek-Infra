use std::env::VarError;
use std::io::{self, Cursor};
use std::sync::Arc;

use crate::CallerIdentity;
use crate::service::{
    ProductionFailClosedAuthenticator, ServiceBearerAuthenticator, TransportAuthenticator,
    valid_service_bearer,
};

pub const WORKER_TLS_CERT_FILE: &str = "DEEPSEEK_WORKER_TLS_CERT_FILE";
pub const WORKER_TLS_KEY_FILE: &str = "DEEPSEEK_WORKER_TLS_KEY_FILE";
pub const WORKER_SERVICE_BEARER: &str = "DEEPSEEK_WORKER_SERVICE_BEARER";
pub const WORKER_SERVICE_BEARER_EXPIRES_AT: &str = "DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT";
pub const WORKER_SERVICE_NAME: &str = "DEEPSEEK_WORKER_SERVICE_NAME";
pub const WORKER_SERVICE_ROLE: &str = "DEEPSEEK_WORKER_SERVICE_ROLE";

fn valid_service_identity(service: &str, role: &str) -> bool {
    service == "go-control-plane" && role == "controller"
}

const TLS_ENV_NAMES: [&str; 6] = [
    WORKER_TLS_CERT_FILE,
    WORKER_TLS_KEY_FILE,
    WORKER_SERVICE_BEARER,
    WORKER_SERVICE_BEARER_EXPIRES_AT,
    WORKER_SERVICE_NAME,
    WORKER_SERVICE_ROLE,
];

pub struct WorkerTlsIdentity {
    cert_pem: Vec<u8>,
    key_pem: Vec<u8>,
}

impl WorkerTlsIdentity {
    pub fn server_tls_config(&self) -> tonic::transport::ServerTlsConfig {
        tonic::transport::ServerTlsConfig::new().identity(tonic::transport::Identity::from_pem(
            &self.cert_pem,
            &self.key_pem,
        ))
    }
}

impl std::fmt::Debug for WorkerTlsIdentity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WorkerTlsIdentity")
            .field("cert_pem", &"<redacted>")
            .field("key_pem", &"<redacted>")
            .finish()
    }
}

pub struct LoadedWorkerTransport {
    pub authenticator: Arc<dyn TransportAuthenticator>,
    pub tls_identity: Option<WorkerTlsIdentity>,
}

impl std::fmt::Debug for LoadedWorkerTransport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LoadedWorkerTransport")
            .field("tls", &self.tls_identity.is_some())
            .field("authenticator", &"<redacted>")
            .finish()
    }
}

pub fn load_worker_transport(
    get: impl Fn(&str) -> Result<String, VarError>,
) -> io::Result<LoadedWorkerTransport> {
    let cert_file = optional_var(&get, WORKER_TLS_CERT_FILE)?;
    let key_file = optional_var(&get, WORKER_TLS_KEY_FILE)?;
    let bearer = optional_var(&get, WORKER_SERVICE_BEARER)?;
    let expires_at = optional_var(&get, WORKER_SERVICE_BEARER_EXPIRES_AT)?;
    let service_name = optional_var(&get, WORKER_SERVICE_NAME)?;
    let role = optional_var(&get, WORKER_SERVICE_ROLE)?;
    let present = [
        &cert_file,
        &key_file,
        &bearer,
        &expires_at,
        &service_name,
        &role,
    ]
    .iter()
    .filter(|value| value.is_some())
    .count();
    if present == 0 {
        return Ok(LoadedWorkerTransport {
            authenticator: Arc::new(ProductionFailClosedAuthenticator),
            tls_identity: None,
        });
    }
    if present != TLS_ENV_NAMES.len() {
        return Err(invalid("worker TLS configuration is incomplete"));
    }
    let bearer = bearer.unwrap();
    if !valid_service_bearer(&bearer) || bearer.len() < 32 {
        return Err(invalid("worker service credential is invalid"));
    }
    let service_name = service_name.unwrap();
    let role = role.unwrap();
    if !valid_service_identity(&service_name, &role) {
        return Err(invalid("worker service identity is invalid"));
    }
    let expires_at_unix = crate::authority_request::parse_utc_z_str(expires_at.as_ref().unwrap())
        .map_err(|_| invalid("worker service credential is invalid"))?;
    let now = crate::service::unix_now_seconds()
        .map_err(|_| invalid("worker service credential clock is unavailable"))?;
    if now >= expires_at_unix {
        return Err(invalid("worker service credential is expired"));
    }
    if expires_at_unix.saturating_sub(now) > crate::service::MAX_SERVICE_BEARER_LIFETIME_SECONDS {
        return Err(invalid(
            "worker service credential lifetime exceeds one hour",
        ));
    }
    let cert_pem = read_pem_file(cert_file.as_ref().unwrap(), "worker TLS certificate")?;
    let key_pem = read_pem_file(key_file.as_ref().unwrap(), "worker TLS key")?;
    validate_cert_pem(&cert_pem)?;
    validate_key_pem(&key_pem)?;
    let authenticator = ServiceBearerAuthenticator::new(
        bearer,
        expires_at_unix,
        CallerIdentity { service_name, role },
    );
    Ok(LoadedWorkerTransport {
        authenticator: Arc::new(authenticator),
        tls_identity: Some(WorkerTlsIdentity { cert_pem, key_pem }),
    })
}

fn optional_var(
    get: &impl Fn(&str) -> Result<String, VarError>,
    name: &str,
) -> io::Result<Option<String>> {
    match get(name) {
        Ok(value) => {
            let trimmed = value.trim();
            if trimmed.is_empty() {
                Err(invalid("worker TLS configuration is incomplete"))
            } else {
                Ok(Some(trimmed.to_string()))
            }
        }
        Err(VarError::NotPresent) => Ok(None),
        Err(VarError::NotUnicode(_)) => Err(invalid("worker TLS configuration is incomplete")),
    }
}

fn read_pem_file(path: &str, label: &str) -> io::Result<Vec<u8>> {
    std::fs::read(path).map_err(|_| invalid(&format!("{label} is unreadable")))
}

fn validate_cert_pem(pem: &[u8]) -> io::Result<()> {
    let mut reader = Cursor::new(pem);
    let certs: Result<Vec<_>, _> = rustls_pemfile::certs(&mut reader).collect();
    match certs {
        Ok(certs) if !certs.is_empty() => Ok(()),
        _ => Err(invalid("worker TLS certificate is invalid")),
    }
}

fn validate_key_pem(pem: &[u8]) -> io::Result<()> {
    let mut reader = Cursor::new(pem);
    match rustls_pemfile::private_key(&mut reader) {
        Ok(Some(_)) => Ok(()),
        _ => Err(invalid("worker TLS key is invalid")),
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::AuthError;
    use crate::service::bearer_token_from_metadata;
    use std::collections::HashMap;
    use tonic::metadata::{MetadataMap, MetadataValue};

    const SECRET: &str = "tls-bearer-secret-value-do-not-log";

    fn utc_z_after_minutes(minutes: i64) -> String {
        let when = time::OffsetDateTime::now_utc() + time::Duration::minutes(minutes);
        format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
            when.year(),
            u8::from(when.month()),
            when.day(),
            when.hour(),
            when.minute(),
            when.second()
        )
    }

    fn env_map(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Result<String, VarError> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect();
        move |name: &str| map.get(name).cloned().ok_or(VarError::NotPresent)
    }

    fn write_tls_material(dir: &std::path::Path, server_name: &str) -> (String, String, String) {
        let mut ca_params = rcgen::CertificateParams::default();
        ca_params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        ca_params.key_usages = vec![
            rcgen::KeyUsagePurpose::KeyCertSign,
            rcgen::KeyUsagePurpose::CrlSign,
        ];
        let ca_key = rcgen::KeyPair::generate().unwrap();
        let ca_cert = ca_params.self_signed(&ca_key).unwrap();

        let mut leaf_params = rcgen::CertificateParams::new(vec![server_name.to_string()]).unwrap();
        leaf_params.key_usages = vec![rcgen::KeyUsagePurpose::DigitalSignature];
        leaf_params.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
        let leaf_key = rcgen::KeyPair::generate().unwrap();
        let leaf = leaf_params.signed_by(&leaf_key, &ca_cert, &ca_key).unwrap();

        let cert_path = dir.join("server.pem");
        let key_path = dir.join("server.key");
        let ca_path = dir.join("ca.pem");
        std::fs::write(&cert_path, leaf.pem()).unwrap();
        std::fs::write(&key_path, leaf_key.serialize_pem()).unwrap();
        std::fs::write(&ca_path, ca_cert.pem()).unwrap();
        (
            cert_path.to_string_lossy().into_owned(),
            key_path.to_string_lossy().into_owned(),
            ca_path.to_string_lossy().into_owned(),
        )
    }

    fn complete_env(dir: &std::path::Path) -> (HashMap<String, String>, String) {
        let (cert, key, ca) = write_tls_material(dir, "deepseek-worker.test");
        let mut env = HashMap::new();
        env.insert(WORKER_TLS_CERT_FILE.to_string(), cert);
        env.insert(WORKER_TLS_KEY_FILE.to_string(), key);
        env.insert(WORKER_SERVICE_BEARER.to_string(), SECRET.to_string());
        env.insert(
            WORKER_SERVICE_BEARER_EXPIRES_AT.to_string(),
            utc_z_after_minutes(10),
        );
        env.insert(
            WORKER_SERVICE_NAME.to_string(),
            "go-control-plane".to_string(),
        );
        env.insert(WORKER_SERVICE_ROLE.to_string(), "controller".to_string());
        (env, ca)
    }

    #[test]
    fn unconfigured_transport_is_production_fail_closed() {
        let loaded = load_worker_transport(|_| Err(VarError::NotPresent)).unwrap();
        assert!(loaded.tls_identity.is_none());
        let err = loaded
            .authenticator
            .authenticate(&MetadataMap::new())
            .unwrap_err();
        assert_eq!(err, AuthError::ServiceAuthenticationUnavailable);
        let debug = format!("{loaded:?}");
        assert!(!debug.contains(SECRET));
        assert!(!debug.contains("BEGIN"));
    }

    #[test]
    fn partial_tls_env_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let (env, _) = complete_env(dir.path());
        for dropped in TLS_ENV_NAMES {
            let get = |name: &str| {
                if name == dropped {
                    Err(VarError::NotPresent)
                } else {
                    env.get(name).cloned().ok_or(VarError::NotPresent)
                }
            };
            let err = load_worker_transport(get).unwrap_err();
            assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
            assert!(!err.to_string().contains(SECRET));
        }
    }

    #[test]
    fn empty_tls_env_value_fails_closed() {
        let err = load_worker_transport(env_map(&[(WORKER_TLS_CERT_FILE, "   ")])).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn non_unicode_tls_env_fails_closed() {
        let err =
            load_worker_transport(|_| Err(VarError::NotUnicode(std::ffi::OsString::from("x"))))
                .unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn expired_service_credential_fails_closed_at_load() {
        let dir = tempfile::tempdir().unwrap();
        let (mut env, _) = complete_env(dir.path());
        env.insert(
            WORKER_SERVICE_BEARER_EXPIRES_AT.to_string(),
            "2000-01-01T00:00:00Z".to_string(),
        );
        let err = load_worker_transport(|name| env.get(name).cloned().ok_or(VarError::NotPresent))
            .unwrap_err();
        assert!(err.to_string().contains("expired"));
        assert!(!err.to_string().contains(SECRET));
    }

    #[test]
    fn excessive_lifetime_weak_credential_and_wrong_role_fail_closed() {
        let dir = tempfile::tempdir().unwrap();
        let (complete, _) = complete_env(dir.path());
        for (name, value) in [
            (WORKER_SERVICE_BEARER_EXPIRES_AT, "2099-01-01T00:00:00Z"),
            (WORKER_SERVICE_BEARER, "weak"),
            (WORKER_SERVICE_NAME, "other-service"),
            (WORKER_SERVICE_ROLE, "admin"),
        ] {
            let mut env = complete.clone();
            env.insert(name.to_string(), value.to_string());
            let error =
                load_worker_transport(|name| env.get(name).cloned().ok_or(VarError::NotPresent))
                    .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
            assert!(!error.to_string().contains(SECRET));
        }
    }

    #[test]
    fn invalid_trust_material_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let (mut env, _) = complete_env(dir.path());
        let garbage = dir.path().join("garbage.pem");
        std::fs::write(&garbage, "not-a-certificate").unwrap();
        env.insert(
            WORKER_TLS_CERT_FILE.to_string(),
            garbage.to_string_lossy().into_owned(),
        );
        let err = load_worker_transport(|name| env.get(name).cloned().ok_or(VarError::NotPresent))
            .unwrap_err();
        assert!(!err.to_string().contains(SECRET));
        assert!(!err.to_string().contains("not-a-certificate"));
    }

    #[test]
    fn missing_cert_file_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let (mut env, _) = complete_env(dir.path());
        env.insert(
            WORKER_TLS_CERT_FILE.to_string(),
            dir.path()
                .join("missing.pem")
                .to_string_lossy()
                .into_owned(),
        );
        let err = load_worker_transport(|name| env.get(name).cloned().ok_or(VarError::NotPresent))
            .unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
        assert!(!err.to_string().contains(SECRET));
    }

    #[test]
    fn complete_tls_env_loads_server_identity_and_bearer() {
        let dir = tempfile::tempdir().unwrap();
        let (env, _) = complete_env(dir.path());
        let loaded =
            load_worker_transport(|name| env.get(name).cloned().ok_or(VarError::NotPresent))
                .unwrap();
        assert!(loaded.tls_identity.is_some());
        let debug = format!("{:?} {:?}", loaded, loaded.tls_identity.as_ref().unwrap());
        assert!(!debug.contains(SECRET));
        assert!(!debug.contains("BEGIN PRIVATE KEY"));
        let mut metadata = MetadataMap::new();
        metadata.insert("authorization", format!("Bearer {SECRET}").parse().unwrap());
        let identity = loaded.authenticator.authenticate(&metadata).unwrap();
        assert_eq!(identity.service_name, "go-control-plane");
        assert_eq!(identity.role, "controller");
        let _ = loaded.tls_identity.unwrap().server_tls_config();
    }

    #[test]
    fn service_bearer_rejects_missing_wrong_duplicate_and_expired() {
        let auth = ServiceBearerAuthenticator::new(
            SECRET,
            crate::service::unix_now_seconds().unwrap() + 3600,
            CallerIdentity {
                service_name: "go-control-plane".into(),
                role: "controller".into(),
            },
        );
        assert_eq!(
            auth.authenticate(&MetadataMap::new()),
            Err(AuthError::MissingAuthorization)
        );

        let mut wrong = MetadataMap::new();
        wrong.insert("authorization", "Bearer wrong-token".parse().unwrap());
        assert_eq!(auth.authenticate(&wrong), Err(AuthError::InvalidToken));
        assert!(!format!("{}", AuthError::InvalidToken).contains(SECRET));

        let mut duplicate = MetadataMap::new();
        duplicate.append(
            "authorization",
            MetadataValue::from_static("Bearer tls-bearer-secret-value-do-not-log"),
        );
        duplicate.append(
            "authorization",
            MetadataValue::from_static("Bearer tls-bearer-secret-value-do-not-log"),
        );
        assert_eq!(
            bearer_token_from_metadata(&duplicate),
            Err(AuthError::InvalidToken)
        );
        assert_eq!(auth.authenticate(&duplicate), Err(AuthError::InvalidToken));

        let expired = ServiceBearerAuthenticator::new(
            SECRET,
            1,
            CallerIdentity {
                service_name: "go-control-plane".into(),
                role: "controller".into(),
            },
        );
        let mut valid = MetadataMap::new();
        valid.insert("authorization", format!("Bearer {SECRET}").parse().unwrap());
        assert_eq!(expired.authenticate(&valid), Err(AuthError::InvalidToken));
        let debug = format!("{auth:?} {expired:?}");
        assert!(!debug.contains(SECRET));
    }

    #[test]
    fn malformed_expiry_and_non_graphic_bearer_fail_closed() {
        let dir = tempfile::tempdir().unwrap();
        let (mut env, _) = complete_env(dir.path());
        env.insert(
            WORKER_SERVICE_BEARER_EXPIRES_AT.to_string(),
            "not-a-timestamp".to_string(),
        );
        assert!(
            load_worker_transport(|name| env.get(name).cloned().ok_or(VarError::NotPresent))
                .is_err()
        );
        env.insert(
            WORKER_SERVICE_BEARER_EXPIRES_AT.to_string(),
            "2099-01-01T00:00:00Z".to_string(),
        );
        env.insert(WORKER_SERVICE_BEARER.to_string(), "has space".to_string());
        let err = load_worker_transport(|name| env.get(name).cloned().ok_or(VarError::NotPresent))
            .unwrap_err();
        assert!(!err.to_string().contains("has space"));
    }
}
