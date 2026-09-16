use std::io;
use std::net::SocketAddr;

use deepseek_protocol::generated::deepseek::action::v1::worker_server::WorkerServer;
use deepseek_worker::{Worker, WorkerRpcService, authority_config_from_env, load_worker_transport};
use tonic::transport::Server;

const DEFAULT_LISTEN: &str = "127.0.0.1:50052";

fn parse_listen_addr(raw: &str) -> io::Result<SocketAddr> {
    let address = raw.parse::<SocketAddr>().map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "DEEPSEEK_WORKER_LISTEN must be an IP socket address",
        )
    })?;
    if address.port() == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "DEEPSEEK_WORKER_LISTEN must use a nonzero port",
        ));
    }
    if !address.ip().is_loopback() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "plaintext worker gRPC must bind to loopback",
        ));
    }
    Ok(address)
}

fn configured_listen_addr(
    configured: Result<String, std::env::VarError>,
) -> io::Result<SocketAddr> {
    let raw = match configured {
        Ok(raw) => raw,
        Err(std::env::VarError::NotPresent) => DEFAULT_LISTEN.to_owned(),
        Err(std::env::VarError::NotUnicode(_)) => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "DEEPSEEK_WORKER_LISTEN must be valid Unicode",
            ));
        }
    };
    parse_listen_addr(raw.trim())
}

fn configured_worker(
    get: impl Fn(&str) -> Result<String, std::env::VarError>,
) -> io::Result<Worker> {
    match authority_config_from_env(&get) {
        Ok(Some(config)) => {
            let state_root = get("DEEPSEEK_WORKER_STATE_ROOT").map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "configured worker requires DEEPSEEK_WORKER_STATE_ROOT",
                )
            })?;
            if state_root.trim().is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "worker state root must not be empty",
                ));
            }
            Worker::open_with_authority(config, std::path::Path::new(&state_root)).map_err(
                |error| {
                    io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("worker authority configuration rejected: {}", error.code),
                    )
                },
            )
        }
        Ok(None) => Ok(Worker::new()),
        Err(error) => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("worker authority configuration rejected: {}", error.code),
        )),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let address = configured_listen_addr(std::env::var("DEEPSEEK_WORKER_LISTEN"))?;
    let transport = load_worker_transport(|name| std::env::var(name))?;
    let worker = configured_worker(|name| std::env::var(name))?;
    let authority = if worker.authority_configured() {
        "configured"
    } else {
        "uninitialized"
    };
    let transport_name = if transport.tls_identity.is_some() {
        "tls-server-auth"
    } else {
        "plaintext-loopback"
    };
    let service = WorkerRpcService::new_with_authenticator(worker, transport.authenticator);
    println!(
        "deepseek-worker listening on {address} authority={authority} mutation=denied transport={transport_name}"
    );
    let mut builder = Server::builder();
    if let Some(identity) = transport.tls_identity {
        builder = builder.tls_config(identity.server_tls_config())?;
    }
    builder
        .add_service(WorkerServer::new(service))
        .serve_with_shutdown(address, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn listener_accepts_only_ip_literal_loopback() {
        assert_eq!(
            parse_listen_addr("127.0.0.1:0").unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(parse_listen_addr("[::1]:50052").is_ok());
        assert_eq!(
            parse_listen_addr("0.0.0.0:50052").unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            parse_listen_addr("localhost:50052").unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn listener_defaults_only_when_variable_is_absent() {
        assert_eq!(
            configured_listen_addr(Err(std::env::VarError::NotPresent)).unwrap(),
            DEFAULT_LISTEN.parse::<SocketAddr>().unwrap()
        );
        assert_eq!(
            configured_listen_addr(Err(std::env::VarError::NotUnicode(
                std::ffi::OsString::from("invalid"),
            )))
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn worker_starts_unconfigured_without_authority_env() {
        let worker = configured_worker(|_| Err(std::env::VarError::NotPresent))
            .expect("unconfigured worker");
        assert!(!worker.authority_configured());
    }

    #[test]
    fn tls_listener_still_rejects_wildcard_without_opening_non_loopback() {
        assert_eq!(
            parse_listen_addr("0.0.0.0:50052").unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert!(parse_listen_addr("127.0.0.1:50052").is_ok());
    }
}
