//! Deployment-selected private Go RPC transport shared by the Rust peers.
use std::{env::VarError, fmt, fs::File, io::Read, net::SocketAddr};
use tonic::transport::{Certificate, ClientTlsConfig, Endpoint, Identity};
use zeroize::Zeroizing;

const SETTINGS: [&str; 5] = [
    "DEEPSEEK_GO_CONTROL_RPC_URL",
    "DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE",
    "DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE",
    "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE",
    "DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME",
];

#[derive(Debug, Clone, Copy)]
pub struct InvalidControlTransport;

impl fmt::Display for InvalidControlTransport {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("NATIVE_CONTROL_RPC_CONFIG_INVALID")
    }
}
impl std::error::Error for InvalidControlTransport {}

fn material(path: &str) -> Result<Vec<u8>, InvalidControlTransport> {
    const LIMIT: u64 = 1024 * 1024;
    let mut input = File::open(path)
        .map_err(|_| InvalidControlTransport)?
        .take(LIMIT + 1);
    let mut bytes = Vec::new();
    input
        .read_to_end(&mut bytes)
        .map_err(|_| InvalidControlTransport)?;
    if bytes.is_empty() || bytes.len() as u64 > LIMIT {
        return Err(InvalidControlTransport);
    }
    Ok(bytes)
}

/// Absence retains legacy qualification configuration. Any explicit private
/// setting requires the complete trusted transport; failure never selects HTTP.
pub fn private_endpoint_from_lookup(
    get: impl Fn(&str) -> Result<String, VarError>,
) -> Result<Option<Endpoint>, InvalidControlTransport> {
    let mut values = Vec::with_capacity(SETTINGS.len());
    let mut present = false;
    for name in SETTINGS {
        match get(name) {
            Ok(value) => {
                present = true;
                values.push(value.trim().to_string());
            }
            Err(VarError::NotPresent) => values.push(String::new()),
            Err(_) => return Err(InvalidControlTransport),
        }
    }
    if !present {
        return Ok(None);
    }
    if values.iter().any(String::is_empty) {
        return Err(InvalidControlTransport);
    }
    let address = values[0]
        .strip_prefix("https://")
        .ok_or(InvalidControlTransport)?;
    let address: SocketAddr = address
        .strip_suffix('/')
        .unwrap_or(address)
        .parse()
        .map_err(|_| InvalidControlTransport)?;
    if !address.ip().is_loopback()
        || address.port() == 0
        || values[4]
            .chars()
            .any(|value| value.is_whitespace() || "/\\?#@".contains(value))
    {
        return Err(InvalidControlTransport);
    }
    let roots = material(&values[1])?;
    let certificates =
        reqwest::Certificate::from_pem_bundle(&roots).map_err(|_| InvalidControlTransport)?;
    if certificates.is_empty() {
        return Err(InvalidControlTransport);
    }
    let certificate = material(&values[2])?;
    let key = Zeroizing::new(material(&values[3])?);
    let endpoint = Endpoint::from_shared(values[0].clone())
        .map_err(|_| InvalidControlTransport)?
        .tls_config(
            ClientTlsConfig::new()
                .domain_name(&values[4])
                .ca_certificate(Certificate::from_pem(roots))
                .identity(Identity::from_pem(certificate, key.as_slice())),
        )
        .map_err(|_| InvalidControlTransport)?;
    Ok(Some(endpoint))
}
