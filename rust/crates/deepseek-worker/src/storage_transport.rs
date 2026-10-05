#[cfg(feature = "s3")]
use deepseek_storage::s3::{S3Config, S3Credentials, S3Transport};
#[cfg(feature = "s3")]
use std::{env::VarError, io, sync::Arc};

pub const WORKER_S3_ENV_NAMES: [&str; 8] = [
    "DEEPSEEK_WORKER_S3_ENDPOINT",
    "DEEPSEEK_WORKER_S3_BUCKET",
    "DEEPSEEK_WORKER_S3_PREFIX",
    "DEEPSEEK_WORKER_S3_REGION",
    "DEEPSEEK_WORKER_S3_ACCESS_KEY",
    "DEEPSEEK_WORKER_S3_SECRET_KEY",
    "DEEPSEEK_WORKER_S3_SESSION_TOKEN",
    "DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK",
];

#[cfg(feature = "s3")]
pub fn load_worker_storage_transport(
    get: impl Fn(&str) -> Result<String, VarError>,
) -> io::Result<Option<Arc<S3Transport>>> {
    let mut values: [Option<String>; 8] = std::array::from_fn(|_| None);
    for (index, name) in WORKER_S3_ENV_NAMES.iter().enumerate() {
        values[index] = match get(name) {
            Ok(value) => Some(value),
            Err(VarError::NotPresent) => None,
            Err(VarError::NotUnicode(_)) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("{name} must be valid Unicode"),
                ));
            }
        };
    }
    if values.iter().all(Option::is_none) {
        return Ok(None);
    }
    let required = |index: usize| -> io::Result<String> {
        values[index]
            .as_ref()
            .filter(|value| !value.is_empty())
            .cloned()
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!(
                        "configured worker S3 requires {}",
                        WORKER_S3_ENV_NAMES[index]
                    ),
                )
            })
    };
    let allow_http_loopback = match values[7].as_deref() {
        None | Some("false") => false,
        Some("true") => true,
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK must be true or false",
            ));
        }
    };
    let config = S3Config {
        endpoint: required(0)?,
        bucket: required(1)?,
        prefix: values[2].clone().unwrap_or_default(),
        region: values[3].clone().unwrap_or_else(|| "us-east-1".into()),
        allow_http_loopback,
    };
    let credentials =
        S3Credentials::new(required(4)?, required(5)?, values[6].clone()).map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "worker S3 credentials are invalid",
            )
        })?;
    let transport = S3Transport::new(config, credentials).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "worker S3 configuration is invalid",
        )
    })?;
    Ok(Some(Arc::new(transport)))
}

#[cfg(all(test, feature = "s3"))]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn configuration() -> HashMap<&'static str, String> {
        HashMap::from([
            (WORKER_S3_ENV_NAMES[0], "https://storage.example".into()),
            (WORKER_S3_ENV_NAMES[1], "native-worker".into()),
            (WORKER_S3_ENV_NAMES[4], "offline-access".into()),
            (WORKER_S3_ENV_NAMES[5], "offline-secret".into()),
        ])
    }

    fn load(values: &HashMap<&str, String>) -> io::Result<Option<Arc<S3Transport>>> {
        load_worker_storage_transport(|name| values.get(name).cloned().ok_or(VarError::NotPresent))
    }

    #[tokio::test]
    async fn absent_configuration_keeps_mutations_unconfigured() {
        assert!(load(&HashMap::new()).unwrap().is_none());
    }

    #[tokio::test]
    async fn required_configuration_installs_transport_with_stable_defaults() {
        use deepseek_storage::s3::{S3Config, S3Credentials};
        let expected = S3Transport::new(
            S3Config {
                endpoint: "https://storage.example".into(),
                bucket: "native-worker".into(),
                prefix: String::new(),
                region: "us-east-1".into(),
                allow_http_loopback: false,
            },
            S3Credentials::new("offline-access".into(), "offline-secret".into(), None).unwrap(),
        )
        .unwrap();
        assert_eq!(
            load(&configuration()).unwrap().unwrap().target_identity(),
            expected.target_identity()
        );
    }

    #[tokio::test]
    async fn every_partial_configuration_and_blank_required_value_is_rejected() {
        for name in WORKER_S3_ENV_NAMES {
            assert!(load(&HashMap::from([(name, "configured".into())])).is_err());
        }
        for name in [
            WORKER_S3_ENV_NAMES[0],
            WORKER_S3_ENV_NAMES[1],
            WORKER_S3_ENV_NAMES[4],
            WORKER_S3_ENV_NAMES[5],
        ] {
            let mut values = configuration();
            values.remove(name);
            assert!(load(&values).is_err(), "missing {name}");
            values.insert(name, String::new());
            assert!(load(&values).is_err(), "blank {name}");
        }
    }

    #[tokio::test]
    async fn insecure_or_normalized_endpoint_is_rejected_without_disclosing_it() {
        for endpoint in [
            "http://127.0.0.1:9000",
            "http://storage.example",
            "https://secret-user:secret-password@storage.example",
            "https://storage.example/private",
            " https://storage.example",
        ] {
            let mut values = configuration();
            values.insert(WORKER_S3_ENV_NAMES[0], endpoint.into());
            let error = load(&values).unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
            assert!(!error.to_string().contains(endpoint));
            assert!(!format!("{error:?}").contains("secret-password"));
        }
    }

    #[tokio::test]
    async fn http_requires_explicit_loopback_opt_in_and_rejects_remote_origins() {
        let mut values = configuration();
        values.insert(WORKER_S3_ENV_NAMES[0], "http://127.0.0.1:9000".into());
        values.insert(WORKER_S3_ENV_NAMES[7], "true".into());
        assert!(load(&values).unwrap().is_some());
        values.insert(WORKER_S3_ENV_NAMES[0], "http://storage.example".into());
        assert!(load(&values).is_err());
        values.insert(WORKER_S3_ENV_NAMES[0], "https://storage.example".into());
        for flag in ["", "yes", "TRUE", "1"] {
            values.insert(WORKER_S3_ENV_NAMES[7], flag.into());
            assert!(load(&values).is_err());
        }
        values.insert(WORKER_S3_ENV_NAMES[7], "false".into());
        assert!(load(&values).unwrap().is_some());
    }

    #[tokio::test]
    async fn placement_changes_target_but_credentials_do_not() {
        let mut values = configuration();
        let initial = load(&values).unwrap().unwrap().target_identity();
        values.insert(WORKER_S3_ENV_NAMES[4], "rotated-access".into());
        values.insert(WORKER_S3_ENV_NAMES[5], "rotated-secret".into());
        values.insert(WORKER_S3_ENV_NAMES[6], "offline-session-token".into());
        assert_eq!(load(&values).unwrap().unwrap().target_identity(), initial);
        values.insert(WORKER_S3_ENV_NAMES[2], "worker-chunks".into());
        assert_ne!(load(&values).unwrap().unwrap().target_identity(), initial);
        values.insert(WORKER_S3_ENV_NAMES[3], "eu-west-1".into());
        assert_ne!(load(&values).unwrap().unwrap().target_identity(), initial);
    }

    #[tokio::test]
    async fn invalid_credentials_and_optional_placement_are_rejected() {
        for (name, value) in [
            (WORKER_S3_ENV_NAMES[4], "bad access"),
            (WORKER_S3_ENV_NAMES[5], "secret\nvalue"),
            (WORKER_S3_ENV_NAMES[6], ""),
            (WORKER_S3_ENV_NAMES[2], "../escape"),
            (WORKER_S3_ENV_NAMES[3], ""),
        ] {
            let mut values = configuration();
            values.insert(name, value.into());
            let error = load(&values).unwrap_err();
            assert!(!format!("{error:?}").contains("secret\nvalue"));
        }
    }

    #[tokio::test]
    async fn non_unicode_configuration_is_rejected_with_key_only() {
        for name in WORKER_S3_ENV_NAMES {
            let error = load_worker_storage_transport(|requested| {
                if requested == name {
                    Err(VarError::NotUnicode(std::ffi::OsString::from(
                        "sensitive-value",
                    )))
                } else {
                    Err(VarError::NotPresent)
                }
            })
            .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
            assert!(error.to_string().contains(name));
            assert!(!format!("{error:?}").contains("sensitive-value"));
        }
    }
}
