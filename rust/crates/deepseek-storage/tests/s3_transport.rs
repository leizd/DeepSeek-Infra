use deepseek_storage::s3::{S3Config, S3Credentials, S3Error, S3Transport};

fn config(endpoint: &str) -> S3Config {
    S3Config {
        endpoint: endpoint.into(),
        bucket: "native-test".into(),
        prefix: "objects".into(),
        region: "us-east-1".into(),
        allow_http_loopback: false,
    }
}

fn credentials() -> S3Credentials {
    S3Credentials::new("test-access".into(), "test-only-secret".into(), None).unwrap()
}

#[tokio::test]
async fn target_identity_binds_placement_but_not_rotatable_credentials() {
    let options = config("https://s3.example.com");
    let target = S3Transport::new(options.clone(), credentials())
        .unwrap()
        .target_identity();
    let rotated =
        S3Credentials::new("rotated-access".into(), "rotated-test-secret".into(), None).unwrap();
    assert_eq!(
        target,
        S3Transport::new(options.clone(), rotated)
            .unwrap()
            .target_identity()
    );
    assert_eq!(
        target,
        S3Transport::new(config("https://s3.example.com/"), credentials())
            .unwrap()
            .target_identity()
    );
    let variants = [
        S3Config {
            endpoint: "https://other.example.com".into(),
            ..options.clone()
        },
        S3Config {
            endpoint: "https://s3.example.com:9443".into(),
            ..options.clone()
        },
        S3Config {
            bucket: "other-bucket".into(),
            ..options.clone()
        },
        S3Config {
            prefix: "other-prefix".into(),
            ..options.clone()
        },
        S3Config {
            region: "other-region".into(),
            ..options.clone()
        },
    ];
    for variant in variants {
        assert_ne!(
            target,
            S3Transport::new(variant, credentials())
                .unwrap()
                .target_identity()
        );
    }
}

#[test]
fn transport_rejects_ambiguous_or_unsafe_endpoints_without_network() {
    for endpoint in [
        "http://example.com",
        "http://127.0.0.1:9000",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?query",
        "https://example.com#fragment",
        "file:///tmp/bucket",
        "https://example.com/../",
        "https://example.com\\bucket",
    ] {
        assert!(
            matches!(
                S3Transport::new(config(endpoint), credentials()),
                Err(S3Error::InvalidConfig)
            ),
            "{endpoint}"
        );
    }
}

#[tokio::test]
async fn explicit_transport_preserves_exact_keys_and_redacts_credentials() {
    let mut options = config("http://127.0.0.1:9000");
    options.allow_http_loopback = true;
    let transport = S3Transport::new(options, credentials()).unwrap();
    for key in ["chunk.age", "unicode/数据 +%?#.age", "literal/%2e%2e/key"] {
        assert_eq!(transport.object_key(key).unwrap(), format!("objects/{key}"));
    }
    for key in [
        "",
        "/absolute",
        "trailing/",
        "a//b",
        "a/../b",
        "./a",
        "a\\b",
        "a\n",
    ] {
        assert!(
            matches!(transport.object_key(key), Err(S3Error::InvalidKey)),
            "{key:?}"
        );
    }
    assert!(!format!("{transport:?} {:?}", credentials()).contains("test-only-secret"));
}

#[test]
fn incomplete_credentials_never_fall_back_to_the_environment() {
    assert!(matches!(
        S3Credentials::new("".into(), "secret".into(), None),
        Err(S3Error::InvalidConfig)
    ));
    assert!(matches!(
        S3Credentials::new("access".into(), "".into(), None),
        Err(S3Error::InvalidConfig)
    ));
}

#[test]
fn malformed_credentials_cannot_reach_signer_panics() {
    for access in ["key\n", "key/key", "key space", "数据", "key,extra"] {
        assert!(matches!(
            S3Credentials::new(access.into(), "secret".into(), None),
            Err(S3Error::InvalidConfig)
        ));
    }
    for token in ["token\n", "token\r", "token\0", "数据"] {
        assert!(matches!(
            S3Credentials::new("access".into(), "secret".into(), Some(token.into())),
            Err(S3Error::InvalidConfig)
        ));
    }
}

#[tokio::test]
async fn invalid_writes_are_rejected_before_connecting() {
    use bytes::Bytes;
    use deepseek_storage::s3::{ConditionalWrite, MAX_PUT_CHUNK, StorageAuthorityProof};
    use sha2::{Digest, Sha256};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut options = config(&format!("http://{}", listener.local_addr().unwrap()));
    options.allow_http_loopback = true;
    let store = S3Transport::new(options, credentials()).unwrap();
    let payload = Bytes::from_static(b"validated before network");
    let digest = Sha256::digest(&payload).into();
    let proof = StorageAuthorityProof {
        action_id: "action-1".into(),
        execution_epoch: 1,
        fencing_token: 4,
        request_id: "a".repeat(64),
        nonce: "b".repeat(64),
    };
    for etag in [
        "*",
        "",
        "unquoted",
        "W/\"weak\"",
        "\"a\",\"b\"",
        "\"bad\n\"",
        "\"\"",
    ] {
        assert_eq!(
            store
                .put_chunk(
                    "key",
                    payload.clone(),
                    digest,
                    &proof,
                    ConditionalWrite::Match(etag.into())
                )
                .await,
            Err(S3Error::InvalidWrite)
        );
    }
    for bad_proof in [
        StorageAuthorityProof {
            action_id: "".into(),
            ..proof.clone()
        },
        StorageAuthorityProof {
            action_id: "bad\nheader".into(),
            ..proof.clone()
        },
        StorageAuthorityProof {
            execution_epoch: 0,
            ..proof.clone()
        },
        StorageAuthorityProof {
            fencing_token: 0,
            ..proof.clone()
        },
        StorageAuthorityProof {
            fencing_token: -1,
            ..proof.clone()
        },
        StorageAuthorityProof {
            request_id: "short".into(),
            ..proof.clone()
        },
        StorageAuthorityProof {
            nonce: "not-hex".repeat(10),
            ..proof.clone()
        },
    ] {
        assert_eq!(
            store
                .put_chunk(
                    "key",
                    payload.clone(),
                    digest,
                    &bad_proof,
                    ConditionalWrite::Create
                )
                .await,
            Err(S3Error::InvalidWrite)
        );
    }
    assert_eq!(
        store
            .put_chunk("key", payload, [0; 32], &proof, ConditionalWrite::Create)
            .await,
        Err(S3Error::InvalidWrite)
    );
    assert_eq!(
        store
            .put_chunk(
                "key",
                vec![0; MAX_PUT_CHUNK + 1].into(),
                [0; 32],
                &proof,
                ConditionalWrite::Create
            )
            .await,
        Err(S3Error::InvalidWrite)
    );
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(50), listener.accept())
            .await
            .is_err()
    );
}
