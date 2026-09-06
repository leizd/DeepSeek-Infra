//! Real three-MinIO byte tests. Missing providers are errors, never skips.
use std::{
    pin::Pin,
    task::{Context, Poll},
};

use bytes::Bytes;
use deepseek_storage::s3::{
    ConditionalWrite, S3Config, S3Credentials, S3Error, S3Transport, StorageAuthorityProof,
};
use sha2::{Digest, Sha256};
use tokio::io::AsyncWrite;

fn endpoints() -> Vec<String> {
    let endpoints: Vec<_> = std::env::var("DEEPSEEK_NATIVE_S3_ENDPOINTS")
        .expect("run scripts/run_native_s3_e2e.py with real MinIO")
        .split(',')
        .map(str::to_owned)
        .collect();
    assert_eq!(endpoints.len(), 3);
    assert!(
        endpoints[0] != endpoints[1]
            && endpoints[1] != endpoints[2]
            && endpoints[0] != endpoints[2]
    );
    endpoints
}

fn store(endpoint: &str) -> S3Transport {
    store_in_bucket(
        endpoint,
        std::env::var("DEEPSEEK_NATIVE_S3_BUCKET").unwrap(),
    )
}

fn store_in_bucket(endpoint: &str, bucket: String) -> S3Transport {
    S3Transport::new(
        S3Config {
            endpoint: endpoint.into(),
            bucket,
            prefix: "native-byte-tests".into(),
            region: "us-east-1".into(),
            allow_http_loopback: true,
        },
        S3Credentials::new(
            std::env::var("AWS_ACCESS_KEY_ID").unwrap(),
            std::env::var("AWS_SECRET_ACCESS_KEY").unwrap(),
            None,
        )
        .unwrap(),
    )
    .unwrap()
}

#[tokio::test]
async fn missing_bucket_is_not_evidence_of_a_conditional_write_rejection() {
    let bucket = format!(
        "missing-{}",
        std::env::var("DEEPSEEK_NATIVE_S3_BUCKET").unwrap()
    );
    let store = store_in_bucket(&endpoints()[0], bucket);
    let payload = Bytes::from_static(b"missing bucket");
    let digest = Sha256::digest(&payload).into();
    assert_eq!(
        store
            .put_chunk(
                "key",
                payload,
                digest,
                &fence(12),
                ConditionalWrite::Match("\"previous\"".into())
            )
            .await,
        Err(S3Error::EffectUnknown)
    );
}

fn fence(epoch: u64) -> StorageAuthorityProof {
    StorageAuthorityProof {
        action_id: "native-s3-real-test".into(),
        execution_epoch: epoch,
        fencing_token: 4,
        request_id: "a".repeat(64),
        nonce: "b".repeat(64),
    }
}

#[derive(Default)]
struct HashSink {
    hash: Sha256,
    bytes: u64,
    writes: usize,
}

impl AsyncWrite for HashSink {
    fn poll_write(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        // Deliberately partial writes: the transport must honour sink backpressure.
        let count = bytes.len().min(65536);
        self.hash.update(&bytes[..count]);
        self.bytes += count as u64;
        self.writes += 1;
        Poll::Ready(Ok(count))
    }
    fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

#[tokio::test]
async fn rust_moves_and_verifies_payload_on_three_real_providers() {
    let payload: Bytes = (0..8 * 1024 * 1024)
        .map(|n| (n % 251) as u8)
        .collect::<Vec<_>>()
        .into();
    let digest: [u8; 32] = Sha256::digest(&payload).into();
    let key = "unicode/数据 +%#/%2e%2e/chunk.age";
    for endpoint in endpoints() {
        let store = store(&endpoint);
        let first = store
            .put_chunk(
                key,
                payload.clone(),
                digest,
                &fence(7),
                ConditionalWrite::Create,
            )
            .await
            .unwrap();
        let stat = store.stat(key).await.unwrap().unwrap();
        assert_eq!(stat.length, payload.len() as u64);
        assert_eq!(stat.etag, first.etag);
        assert_eq!(
            stat.claimed_sha256,
            Some(format!("{:x}", Sha256::digest(&payload)))
        );
        assert_eq!(
            stat.claimed_action_id.as_deref(),
            Some("native-s3-real-test")
        );
        assert_eq!(stat.claimed_execution_epoch.as_deref(), Some("7"));
        assert_eq!(stat.claimed_fencing_token.as_deref(), Some("4"));
        assert_eq!(
            stat.claimed_request_id.as_deref(),
            Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        );
        assert_eq!(
            stat.claimed_nonce.as_deref(),
            Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        );
        let mut sink = HashSink::default();
        store
            .download_verified(key, payload.len() as u64, digest, &mut sink)
            .await
            .unwrap();
        assert_eq!(sink.bytes, payload.len() as u64);
        assert!(sink.writes > 1);
        assert_eq!(<[u8; 32]>::from(sink.hash.finalize()), digest);
        assert_eq!(
            store
                .put_chunk(
                    key,
                    payload.clone(),
                    digest,
                    &fence(8),
                    ConditionalWrite::Create
                )
                .await,
            Err(S3Error::PreconditionRejected)
        );
        assert_eq!(
            store
                .put_chunk(
                    key,
                    payload.clone(),
                    digest,
                    &fence(8),
                    ConditionalWrite::Match("\"wrong\"".into())
                )
                .await,
            Err(S3Error::PreconditionRejected)
        );
        let replacement = Bytes::from_static(b"native conditional replacement");
        let replacement_digest = Sha256::digest(&replacement).into();
        store
            .put_chunk(
                key,
                replacement.clone(),
                replacement_digest,
                &fence(8),
                ConditionalWrite::Match(first.etag),
            )
            .await
            .unwrap();
        let mut sink = HashSink::default();
        store
            .download_verified(key, replacement.len() as u64, replacement_digest, &mut sink)
            .await
            .unwrap();
        assert_eq!(
            store
                .stat(key)
                .await
                .unwrap()
                .unwrap()
                .claimed_execution_epoch
                .as_deref(),
            Some("8")
        );
    }
}

#[tokio::test]
async fn real_provider_specific_key_rejection_is_never_reported_as_success() {
    let store = store(&endpoints()[0]);
    let payload = Bytes::from_static(b"provider key domain");
    let digest = Sha256::digest(&payload).into();
    let key = "provider-domain/data?#.age";
    let result = store
        .put_chunk(
            key,
            payload.clone(),
            digest,
            &fence(10),
            ConditionalWrite::Create,
        )
        .await;
    // Windows MinIO stores object paths on Windows and rejects '?'. This is a
    // real rejection test, not a skipped case or a silently rewritten object key.
    if cfg!(windows) {
        assert_eq!(result, Err(S3Error::EffectUnknown));
    } else {
        result.unwrap();
        store
            .download_verified(key, payload.len() as u64, digest, &mut HashSink::default())
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn real_provider_reads_fail_closed_on_integrity_and_sink_errors() {
    let store = store(&endpoints()[0]);
    let payload = Bytes::from_static(b"unpublished staging bytes");
    let digest = Sha256::digest(&payload).into();
    store
        .put_chunk(
            "integrity",
            payload.clone(),
            digest,
            &fence(9),
            ConditionalWrite::Create,
        )
        .await
        .unwrap();
    let mut sink = HashSink::default();
    assert_eq!(
        store
            .download_verified("integrity", 1, digest, &mut sink)
            .await,
        Err(S3Error::IntegrityMismatch)
    );
    assert_eq!(sink.bytes, 0);
    assert_eq!(
        store
            .download_verified("integrity", payload.len() as u64, [0; 32], &mut sink)
            .await,
        Err(S3Error::IntegrityMismatch)
    );
    let (reader, mut writer) = tokio::io::duplex(1);
    drop(reader);
    assert_eq!(
        store
            .download_verified("integrity", payload.len() as u64, digest, &mut writer)
            .await,
        Err(S3Error::SinkFailed)
    );
    assert_eq!(
        store
            .download_verified("missing", 1, digest, &mut sink)
            .await,
        Err(S3Error::NotFound)
    );
    assert!(store.stat("missing").await.unwrap().is_none());
}

#[tokio::test]
async fn lost_success_response_is_unknown_even_when_real_minio_committed_bytes() {
    use std::time::Duration;
    use tokio::{
        io::AsyncReadExt,
        net::{TcpListener, TcpStream},
        time::timeout,
    };
    let endpoint = endpoints()[0].clone();
    let upstream_address = endpoint.strip_prefix("http://").unwrap().to_string();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy_endpoint = format!("http://{}", listener.local_addr().unwrap());
    // Forward the real signed PUT unchanged. Suppress only MinIO's actual 200
    // response, after the provider has accepted the bytes. This is not fake S3.
    let proxy = tokio::spawn(async move {
        {
            let (mut downstream, _) = timeout(Duration::from_secs(10), listener.accept())
                .await
                .unwrap()
                .unwrap();
            let mut upstream = TcpStream::connect(upstream_address).await.unwrap();
            let (mut client_read, _) = downstream.split();
            let (mut provider_read, mut provider_write) = upstream.split();
            let read_response = async {
                let mut response = Vec::new();
                loop {
                    let mut part = [0; 1024];
                    let count = provider_read.read(&mut part).await.unwrap();
                    assert!(count > 0, "provider closed without response");
                    response.extend_from_slice(&part[..count]);
                    assert!(response.len() <= 65536);
                    if response.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
                assert!(
                    response.starts_with(b"HTTP/1.1 200 "),
                    "real provider must ACK before disconnect"
                );
            };
            timeout(Duration::from_secs(10), async {
                tokio::select! {
                    _ = tokio::io::copy(&mut client_read, &mut provider_write) => panic!("client closed before provider ACK"),
                    _ = read_response => {},
                }
            }).await.unwrap();
        } // Disconnect both sockets instead of delivering the successful response.
        assert!(
            timeout(Duration::from_millis(250), listener.accept())
                .await
                .is_err(),
            "unexpected hidden retry"
        );
    });
    let payload = Bytes::from_static(b"provider committed but ACK lost");
    let digest = Sha256::digest(&payload).into();
    let result = store(&proxy_endpoint)
        .put_chunk(
            "lost-ack",
            payload.clone(),
            digest,
            &fence(11),
            ConditionalWrite::Create,
        )
        .await;
    assert_eq!(result, Err(S3Error::EffectUnknown));
    proxy.await.unwrap();
    let direct = store(&endpoint);
    let observation = direct.stat("lost-ack").await.unwrap().unwrap();
    assert_eq!(observation.claimed_execution_epoch.as_deref(), Some("11"));
    direct
        .download_verified(
            "lost-ack",
            payload.len() as u64,
            digest,
            &mut HashSink::default(),
        )
        .await
        .unwrap();
}

#[tokio::test]
async fn conditional_verified_read_binds_bytes_and_metadata_to_the_observed_object() {
    let transport = store(&endpoints()[2]);
    let key = "conditional-observation";
    let payload = Bytes::from_static(b"same bytes but different operation metadata");
    let digest = Sha256::digest(&payload).into();
    transport
        .put_chunk(
            key,
            payload.clone(),
            digest,
            &fence(21),
            ConditionalWrite::Create,
        )
        .await
        .unwrap();
    let first = transport.stat(key).await.unwrap().unwrap();
    transport
        .download_observation_verified(key, &first, digest, &mut HashSink::default())
        .await
        .unwrap();

    // Same payload preserves the ETag but changes action metadata. If-Match alone
    // cannot establish that GET belongs to the object observed by the earlier HEAD.
    transport
        .put_chunk(
            key,
            payload.clone(),
            digest,
            &fence(22),
            ConditionalWrite::Match(first.etag.clone()),
        )
        .await
        .unwrap();
    let second = transport.stat(key).await.unwrap().unwrap();
    assert_eq!(first.etag, second.etag);
    let mut sink = HashSink::default();
    assert_eq!(
        transport
            .download_observation_verified(key, &first, digest, &mut sink)
            .await,
        Err(S3Error::IntegrityMismatch)
    );
    assert_eq!(sink.bytes, 0);
    transport
        .download_observation_verified(key, &second, digest, &mut HashSink::default())
        .await
        .unwrap();

    // A different payload changes the ETag. The stale observation must fail the
    // actual provider If-Match precondition before any response bytes reach staging.
    let replacement = Bytes::from_static(b"different provider bytes");
    let replacement_digest = Sha256::digest(&replacement).into();
    transport
        .put_chunk(
            key,
            replacement,
            replacement_digest,
            &fence(23),
            ConditionalWrite::Match(second.etag.clone()),
        )
        .await
        .unwrap();
    let mut sink = HashSink::default();
    assert_eq!(
        transport
            .download_observation_verified(key, &second, digest, &mut sink)
            .await,
        Err(S3Error::ReadFailed)
    );
    assert_eq!(sink.bytes, 0);
    let current = transport.stat(key).await.unwrap().unwrap();
    assert_eq!(
        transport
            .download_observation_verified(key, &current, digest, &mut HashSink::default())
            .await,
        Err(S3Error::IntegrityMismatch)
    );
    transport
        .download_observation_verified(key, &current, replacement_digest, &mut HashSink::default())
        .await
        .unwrap();
}
