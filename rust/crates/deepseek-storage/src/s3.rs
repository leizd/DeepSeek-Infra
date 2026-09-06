//! Explicit, bounded S3 transport. This module does not grant production authority.
//! See docs/NATIVE_S3_TRANSPORT.md for effect uncertainty and staging obligations.

mod http;

use std::{fmt, net::IpAddr, time::Duration};

use bytes::Bytes;
use futures_util::TryStreamExt;
use object_store::{
    Attribute, Attributes, GetOptions, GetResult, ObjectStore, PutMode, PutOptions, RetryConfig,
    UpdateVersion,
    aws::{AmazonS3, AmazonS3Builder, Checksum, S3ConditionalPut},
    path::Path,
};
use reqwest::{Client, Url};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncWrite, AsyncWriteExt};

pub const MAX_PUT_CHUNK: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone)]
pub enum ConditionalWrite {
    Create,
    Match(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageAuthorityProof {
    pub action_id: String,
    pub execution_epoch: u64,
    pub fencing_token: i64,
    pub request_id: String,
    pub nonce: String,
}

/// An observation of one PUT response, not a durable effect or live lease proof.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PutObservation {
    pub authority: StorageAuthorityProof,
    pub length: u64,
    pub sha256: [u8; 32],
    pub etag: String,
    pub version: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObjectObservation {
    pub length: u64,
    pub etag: String,
    pub version: Option<String>,
    pub claimed_sha256: Option<String>,
    pub claimed_action_id: Option<String>,
    pub claimed_execution_epoch: Option<String>,
    pub claimed_fencing_token: Option<String>,
    pub claimed_request_id: Option<String>,
    pub claimed_nonce: Option<String>,
}

#[derive(Clone)]
pub struct S3Config {
    pub endpoint: String,
    pub bucket: String,
    pub prefix: String,
    pub region: String,
    pub allow_http_loopback: bool,
}

pub struct S3Credentials {
    access: String,
    secret: String,
    token: Option<String>,
}

impl fmt::Debug for S3Credentials {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("S3Credentials([redacted])")
    }
}

impl S3Credentials {
    pub fn new(access: String, secret: String, token: Option<String>) -> Result<Self, S3Error> {
        if access.is_empty()
            || access.len() > 128
            || !access
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
            || !credential_text(&secret, 4096)
            || token
                .as_ref()
                .is_some_and(|value| !credential_text(value, 16384))
        {
            return Err(S3Error::InvalidConfig);
        }
        Ok(Self {
            access,
            secret,
            token,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum S3Error {
    InvalidConfig,
    InvalidKey,
    InvalidWrite,
    PreconditionRejected,
    EffectUnknown,
    NotFound,
    ReadFailed,
    IntegrityMismatch,
    SinkFailed,
}

impl fmt::Display for S3Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for S3Error {}

pub struct S3Transport {
    store: AmazonS3,
    bucket: String,
    prefix: String,
    target_identity: [u8; 32],
}

impl fmt::Debug for S3Transport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("S3Transport([redacted])")
    }
}

impl S3Transport {
    pub fn new(config: S3Config, credentials: S3Credentials) -> Result<Self, S3Error> {
        validate_config(&config)?;
        // Versioned, length-framed placement identity. Credentials may rotate
        // without changing the target; caller key is bound separately in the journal.
        let mut identity = Sha256::new();
        identity.update(b"deepseek-infra:s3-target-v1\0");
        for field in [
            config.endpoint.trim_end_matches('/'),
            &config.region,
            &config.bucket,
            &config.prefix,
        ] {
            identity.update((field.len() as u64).to_be_bytes());
            identity.update(field.as_bytes());
        }
        let target_identity = identity.finalize().into();
        let bucket = config.bucket.clone();
        // Explicit policy at BOTH retry layers; no ambient proxy or redirect target.
        // https://docs.rs/reqwest/0.12.28/reqwest/struct.ClientBuilder.html
        let client = Client::builder()
            .https_only(!config.allow_http_loopback)
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            // hyper-util can retry cancellation on pooled connections independently of
            // reqwest's retry policy. Do not reuse HTTP/1 or multiplex HTTP/2 connections.
            .http1_only()
            .pool_max_idle_per_host(0)
            .no_gzip()
            .no_brotli()
            .no_zstd()
            .no_deflate()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(60))
            .build()
            .map_err(|_| S3Error::InvalidConfig)?;
        let mut builder = AmazonS3Builder::new()
            .with_endpoint(config.endpoint)
            .with_bucket_name(config.bucket)
            .with_region(config.region)
            .with_access_key_id(credentials.access)
            .with_secret_access_key(credentials.secret)
            .with_allow_http(config.allow_http_loopback)
            .with_virtual_hosted_style_request(false)
            .with_checksum_algorithm(Checksum::SHA256)
            .with_conditional_put(S3ConditionalPut::ETagMatch)
            .with_retry(RetryConfig {
                max_retries: 0,
                ..Default::default()
            })
            .with_http_connector(http::DirectConnector(client));
        if let Some(token) = credentials.token {
            builder = builder.with_token(token);
        }
        Ok(Self {
            store: builder.build().map_err(|_| S3Error::InvalidConfig)?,
            bucket,
            prefix: config.prefix,
            target_identity,
        })
    }

    pub fn bucket(&self) -> &str {
        &self.bucket
    }

    pub fn prefix(&self) -> &str {
        &self.prefix
    }

    /// Stable fingerprint of configured endpoint, region, bucket and prefix.
    /// This is placement binding, not proof of provider ownership or permission.
    pub fn target_identity(&self) -> [u8; 32] {
        self.target_identity
    }

    pub fn object_key(&self, key: &str) -> Result<String, S3Error> {
        exact_path(key)?;
        let full = if self.prefix.is_empty() {
            key.to_owned()
        } else {
            format!("{}/{key}", self.prefix)
        };
        exact_path(&full)?;
        Ok(full)
    }

    pub async fn put_chunk(
        &self,
        key: &str,
        payload: Bytes,
        sha256: [u8; 32],
        authority: &StorageAuthorityProof,
        condition: ConditionalWrite,
    ) -> Result<PutObservation, S3Error> {
        let path = exact_path(&self.object_key(key)?)?;
        let length = payload.len() as u64;
        if payload.len() > MAX_PUT_CHUNK
            || authority.execution_epoch == 0
            || authority.fencing_token <= 0
            || authority.action_id.is_empty()
            || authority.action_id.len() > 128
            || !authority
                .action_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_.:".contains(&b))
            || authority.request_id.len() != 64
            || !authority.request_id.bytes().all(|b| b.is_ascii_hexdigit())
            || authority.nonce.len() != 64
            || !authority.nonce.bytes().all(|b| b.is_ascii_hexdigit())
            || <[u8; 32]>::from(Sha256::digest(&payload)) != sha256
        {
            return Err(S3Error::InvalidWrite);
        }
        let mode = match condition {
            ConditionalWrite::Create => PutMode::Create,
            ConditionalWrite::Match(etag) if strong_etag(&etag) => PutMode::Update(UpdateVersion {
                e_tag: Some(etag),
                version: None,
            }),
            _ => return Err(S3Error::InvalidWrite),
        };
        let attributes: Attributes = [
            (
                Attribute::Metadata("sha256".into()),
                format!("{:x}", Sha256::digest(&payload)),
            ),
            (
                Attribute::Metadata("action-id".into()),
                authority.action_id.clone(),
            ),
            (
                Attribute::Metadata("execution-epoch".into()),
                authority.execution_epoch.to_string(),
            ),
            (
                Attribute::Metadata("fencing-token".into()),
                authority.fencing_token.to_string(),
            ),
            (
                Attribute::Metadata("request-id".into()),
                authority.request_id.clone(),
            ),
            (Attribute::Metadata("nonce".into()), authority.nonce.clone()),
        ]
        .into_iter()
        .collect();
        let response = self
            .store
            .put_opts(
                &path,
                payload.into(),
                PutOptions {
                    mode,
                    attributes,
                    ..Default::default()
                },
            )
            .await
            .map_err(|error| {
                if http::is_precondition(&error) {
                    S3Error::PreconditionRejected
                } else {
                    S3Error::EffectUnknown
                }
            })?;
        let etag = response
            .e_tag
            .filter(|etag| strong_etag(etag))
            .ok_or(S3Error::EffectUnknown)?;
        Ok(PutObservation {
            authority: authority.clone(),
            length,
            sha256,
            etag,
            version: response.version,
        })
    }

    pub async fn stat(&self, key: &str) -> Result<Option<ObjectObservation>, S3Error> {
        let path = exact_path(&self.object_key(key)?)?;
        let response = match self
            .store
            .get_opts(
                &path,
                GetOptions {
                    head: true,
                    ..Default::default()
                },
            )
            .await
        {
            Ok(response) => response,
            Err(object_store::Error::NotFound { .. }) => return Ok(None),
            Err(_) => return Err(S3Error::ReadFailed),
        };
        object_observation(&response).map(Some)
    }

    /// Requires fresh/empty staging at offset zero. Success verifies the received stream
    /// and flush, not arbitrary existing sink contents or filesystem durability.
    /// On any error the sink is unverified staging data, never a committed restore.
    pub async fn download_verified<W: AsyncWrite + Unpin>(
        &self,
        key: &str,
        length: u64,
        sha256: [u8; 32],
        sink: &mut W,
    ) -> Result<(), S3Error> {
        let path = exact_path(&self.object_key(key)?)?;
        let response = self
            .store
            .get_opts(&path, GetOptions::default())
            .await
            .map_err(|error| match error {
                object_store::Error::NotFound { .. } => S3Error::NotFound,
                _ => S3Error::ReadFailed,
            })?;
        stream_verified(response, length, sha256, sink).await
    }

    /// Verify the exact observed ETag/version, authority metadata, length and bytes
    /// in one conditional GET. An old HEAD alone is not byte-integrity evidence.
    /// The sink has the same uncommitted staging obligations as download_verified.
    pub async fn download_observation_verified<W: AsyncWrite + Unpin>(
        &self,
        key: &str,
        observation: &ObjectObservation,
        sha256: [u8; 32],
        sink: &mut W,
    ) -> Result<(), S3Error> {
        if !strong_etag(&observation.etag) {
            return Err(S3Error::IntegrityMismatch);
        }
        let path = exact_path(&self.object_key(key)?)?;
        let response = self
            .store
            .get_opts(
                &path,
                GetOptions {
                    if_match: Some(observation.etag.clone()),
                    version: observation.version.clone(),
                    ..Default::default()
                },
            )
            .await
            .map_err(|error| match error {
                object_store::Error::NotFound { .. } => S3Error::NotFound,
                _ => S3Error::ReadFailed,
            })?;
        // Same-content overwrites can keep the same ETag while replacing metadata.
        // Compare the GET's metadata too, before allowing any bytes into staging.
        if object_observation(&response)? != *observation {
            return Err(S3Error::IntegrityMismatch);
        }
        stream_verified(response, observation.length, sha256, sink).await
    }
}

fn object_observation(response: &GetResult) -> Result<ObjectObservation, S3Error> {
    let claim = |name: &'static str| {
        response
            .attributes
            .get(&Attribute::Metadata(name.into()))
            .map(|value| value.to_string())
    };
    let etag = response
        .meta
        .e_tag
        .clone()
        .filter(|etag| strong_etag(etag))
        .ok_or(S3Error::ReadFailed)?;
    Ok(ObjectObservation {
        length: response.meta.size,
        etag,
        version: response.meta.version.clone(),
        claimed_sha256: claim("sha256"),
        claimed_action_id: claim("action-id"),
        claimed_execution_epoch: claim("execution-epoch"),
        claimed_fencing_token: claim("fencing-token"),
        claimed_request_id: claim("request-id"),
        claimed_nonce: claim("nonce"),
    })
}

async fn stream_verified<W: AsyncWrite + Unpin>(
    response: GetResult,
    length: u64,
    sha256: [u8; 32],
    sink: &mut W,
) -> Result<(), S3Error> {
    if response.meta.size != length || response.range != (0..length) {
        return Err(S3Error::IntegrityMismatch);
    }
    let mut stream = response.into_stream();
    let mut received = 0u64;
    let mut hash = Sha256::new();
    while let Some(bytes) = stream.try_next().await.map_err(|_| S3Error::ReadFailed)? {
        received = received
            .checked_add(bytes.len() as u64)
            .ok_or(S3Error::IntegrityMismatch)?;
        if received > length {
            return Err(S3Error::IntegrityMismatch);
        }
        hash.update(&bytes);
        sink.write_all(&bytes)
            .await
            .map_err(|_| S3Error::SinkFailed)?;
    }
    if received != length || <[u8; 32]>::from(hash.finalize()) != sha256 {
        return Err(S3Error::IntegrityMismatch);
    }
    sink.flush().await.map_err(|_| S3Error::SinkFailed)
}

fn strong_etag(etag: &str) -> bool {
    (3..=1024).contains(&etag.len())
        && etag.starts_with('"')
        && etag.ends_with('"')
        && etag.as_bytes()[1..etag.len() - 1]
            .iter()
            .all(|b| (33..=126).contains(b) && *b != b'"')
}

fn credential_text(value: &str, max: usize) -> bool {
    !value.is_empty() && value.len() <= max && value.bytes().all(|byte| (33..=126).contains(&byte))
}

fn exact_path(key: &str) -> Result<Path, S3Error> {
    if key.is_empty() || key.len() > 1024 || key.contains('\\') || key.chars().any(char::is_control)
    {
        return Err(S3Error::InvalidKey);
    }
    let path = Path::parse(key).map_err(|_| S3Error::InvalidKey)?;
    if path.as_ref() != key {
        return Err(S3Error::InvalidKey);
    }
    Ok(path)
}

fn validate_config(config: &S3Config) -> Result<(), S3Error> {
    let url = Url::parse(&config.endpoint).map_err(|_| S3Error::InvalidConfig)?;
    let origin = url.origin().ascii_serialization();
    // Reject URL parser normalization (dot paths, backslashes, whitespace, userinfo).
    if config.endpoint != origin && config.endpoint != format!("{origin}/") {
        return Err(S3Error::InvalidConfig);
    }
    let loopback = url
        .host_str()
        .and_then(|host| host.trim_matches(['[', ']']).parse::<IpAddr>().ok())
        .is_some_and(|address| address.is_loopback());
    if url.scheme() != "https"
        && !(url.scheme() == "http" && config.allow_http_loopback && loopback)
    {
        return Err(S3Error::InvalidConfig);
    }
    let bucket = &config.bucket;
    if !(3..=63).contains(&bucket.len())
        || !bucket
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        || bucket.starts_with('-')
        || bucket.ends_with('-')
        || config.region.is_empty()
        || config.region.len() > 64
        || !config
            .region
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        || (!config.prefix.is_empty() && exact_path(&config.prefix).is_err())
    {
        return Err(S3Error::InvalidConfig);
    }
    Ok(())
}
