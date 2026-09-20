use std::io;
use std::net::SocketAddr;

use deepseek_browser::BrowserEngine;
use deepseek_protocol::generated::deepseek::browser::v1::browser_engine_server::BrowserEngineServer;
use tonic::transport::Server;

const DEFAULT_LISTEN: &str = "127.0.0.1:50053";
const LISTEN_ENV: &str = "DEEPSEEK_BROWSER_ENGINE_LISTEN";

fn parse_listen_addr(raw: &str) -> io::Result<SocketAddr> {
    let address = raw.parse::<SocketAddr>().map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "DEEPSEEK_BROWSER_ENGINE_LISTEN must be an IP socket address",
        )
    })?;
    if address.port() == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "DEEPSEEK_BROWSER_ENGINE_LISTEN must use a nonzero port",
        ));
    }
    if !address.ip().is_loopback() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "plaintext browser engine gRPC must bind to loopback",
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
                "DEEPSEEK_BROWSER_ENGINE_LISTEN must be valid Unicode",
            ));
        }
    };
    parse_listen_addr(raw.trim())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let address = configured_listen_addr(std::env::var(LISTEN_ENV))?;
    Server::builder()
        .add_service(BrowserEngineServer::new(BrowserEngine))
        .serve(address)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn listener_refuses_wildcard_and_zero_port() {
        assert_eq!(
            parse_listen_addr("0.0.0.0:50053").unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            parse_listen_addr("127.0.0.1:0").unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            parse_listen_addr("localhost:50053").unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(parse_listen_addr("127.0.0.1:50053").is_ok());
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
}
