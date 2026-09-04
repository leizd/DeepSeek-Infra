use std::io;
use std::net::SocketAddr;

use deepseek_protocol::generated::deepseek::action::v1::worker_server::WorkerServer;
use deepseek_worker::{Worker, WorkerRpcService, authority_config_from_env};
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

fn configured_worker() -> io::Result<Worker> {
    let mut worker = Worker::new();
    match authority_config_from_env(|name| std::env::var(name)) {
        Ok(Some(config)) => worker.configure_authority(config).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("worker authority configuration rejected: {}", error.code),
            )
        })?,
        Ok(None) => {}
        Err(error) => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("worker authority configuration rejected: {}", error.code),
            ));
        }
    }
    Ok(worker)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let address = configured_listen_addr(std::env::var("DEEPSEEK_WORKER_LISTEN"))?;
    let worker = configured_worker()?;
    let authority = if worker.authority_configured() {
        "configured"
    } else {
        "uninitialized"
    };
    let service = WorkerRpcService::new(worker);
    println!("deepseek-worker listening on {address} authority={authority} mutation=denied");
    Server::builder()
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
        let worker = configured_worker().expect("unconfigured worker");
        assert!(!worker.authority_configured());
    }
}
