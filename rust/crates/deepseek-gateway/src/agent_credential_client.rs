//! Private TLS delivery to Rust custody; Go receives only the final body hash.
use std::{net::SocketAddr, time::Duration};

use deepseek_policy::agent_run_data::ArtifactReference;
use deepseek_protocol::generated::deepseek::{
    agent::v1::{PrepareAgentCredentialsRequest, agent_node_worker_client::AgentNodeWorkerClient},
    common::v1::ActionFence,
};
use serde_json::{Value, json};
use tonic::{
    Request,
    metadata::MetadataValue,
    transport::{Certificate, ClientTlsConfig, Endpoint},
};

pub(crate) const PRIVATE_FIELD: &str = "_nativeProviderCredentials";

#[derive(Clone)]
pub(crate) struct WorkerConfig {
    pub(crate) origin: String,
    pub(crate) ca_file: std::path::PathBuf,
    pub(crate) server_name: String,
    pub(crate) bearer: zeroize::Zeroizing<String>,
}

impl WorkerConfig {
    pub(crate) fn from_env() -> Option<Self> {
        Some(Self {
            origin: std::env::var("DEEPSEEK_AGENT_CREDENTIAL_WORKER_ORIGIN").ok()?,
            ca_file: std::env::var_os("DEEPSEEK_AGENT_CREDENTIAL_WORKER_CA_FILE")?.into(),
            server_name: std::env::var("DEEPSEEK_AGENT_CREDENTIAL_WORKER_SERVER_NAME").ok()?,
            bearer: zeroize::Zeroizing::new(
                std::env::var("DEEPSEEK_AGENT_CREDENTIAL_WORKER_BEARER").ok()?,
            ),
        })
    }
}

pub(crate) fn has_reserved_field(value: &Value) -> bool {
    match value {
        Value::Object(values) => {
            values.contains_key(PRIVATE_FIELD) || values.values().any(has_reserved_field)
        }
        Value::Array(values) => values.iter().any(has_reserved_field),
        _ => false,
    }
}

pub(crate) async fn prepare(
    config: &WorkerConfig,
    run_id: &str,
    epoch: u64,
    runtime_revision: u64,
    reference: &ArtifactReference,
    api_key: Option<&str>,
    tavily_api_key: Option<&str>,
) -> Result<Value, ()> {
    if runtime_revision == 0 {
        return Err(());
    }
    let origin = &config.origin;
    let address: SocketAddr = origin
        .strip_prefix("https://")
        .ok_or(())?
        .parse()
        .map_err(|_| ())?;
    if !address.ip().is_loopback() || address.port() == 0 {
        return Err(());
    }
    let ca_path = &config.ca_file;
    if !ca_path.is_absolute() || std::fs::metadata(ca_path).map_err(|_| ())?.len() > 128 * 1024 {
        return Err(());
    }
    let ca = std::fs::read(ca_path).map_err(|_| ())?;
    let server_name = &config.server_name;
    if server_name.is_empty()
        || server_name.len() > 253
        || server_name.chars().any(char::is_control)
    {
        return Err(());
    }
    let bearer = &config.bearer;
    if !(32..=4096).contains(&bearer.len()) || !bearer.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(());
    }
    let tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(ca))
        .domain_name(server_name.clone());
    let channel = Endpoint::from_shared(origin.clone())
        .map_err(|_| ())?
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(10))
        .tls_config(tls)
        .map_err(|_| ())?
        .connect()
        .await
        .map_err(|_| ())?;
    let mut client = AgentNodeWorkerClient::new(channel).max_decoding_message_size(16 * 1024);
    let mut request = Request::new(PrepareAgentCredentialsRequest {
        schema_version: 1,
        fence: Some(ActionFence {
            action_id: run_id.into(),
            execution_epoch: epoch,
        }),
        run_id: run_id.into(),
        request: Some(
            deepseek_protocol::generated::deepseek::agent::v1::AgentArtifactReference {
                sha256: reference.sha256.clone(),
                length: reference.length,
            },
        ),
        runtime_revision,
        api_key: api_key.unwrap_or_default().into(),
        tavily_api_key: tavily_api_key.unwrap_or_default().into(),
    });
    let mut authorization: MetadataValue<_> = format!("Bearer {}", bearer.as_str())
        .parse()
        .map_err(|_| ())?;
    authorization.set_sensitive(true);
    request
        .metadata_mut()
        .insert("authorization", authorization);
    let response = client
        .prepare_credentials(request)
        .await
        .map_err(|_| ())?
        .into_inner();
    let scope = response.scope.ok_or(())?;
    let object = response.object.ok_or(())?;
    let hex64 = |value: &str| {
        value.len() == 64
            && value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    };
    if scope.run_id != run_id
        || scope.metadata_epoch != epoch
        || scope.runtime_revision != runtime_revision
        || scope.request_sha256 != reference.sha256
        || scope.request_length != reference.length
        || scope.fleet_id.is_empty()
        || scope.environment.is_empty()
        || !hex64(&object.id)
        || !hex64(&object.sha256)
        || !(1..=65536).contains(&object.length)
    {
        return Err(());
    }
    Ok(
        json!({"scope":{"fleetId":scope.fleet_id,"environment":scope.environment,
        "runId":scope.run_id,"metadataEpoch":scope.metadata_epoch,"runtimeRevision":scope.runtime_revision,
        "requestSha256":scope.request_sha256,"requestLength":scope.request_length},
        "object":{"id":object.id,"sha256":object.sha256,"length":object.length}}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_input_cannot_supply_private_credential_references_at_any_depth() {
        for value in [
            json!({PRIVATE_FIELD:{}}),
            json!({"payload":{PRIVATE_FIELD:{}}}),
            json!({"messages":[{"content":{PRIVATE_FIELD:{}}}]}),
        ] {
            assert!(has_reserved_field(&value));
        }
        assert!(!has_reserved_field(
            &json!({"payload":{"apiKey":"isolated-fixture","messages":[]}})
        ));
    }
}
