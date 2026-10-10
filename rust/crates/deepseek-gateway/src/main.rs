use deepseek_gateway::create_production_app;
use std::{error::Error, path::PathBuf};
use tokio::net::TcpListener;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "deepseek_gateway=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    let addr = std::env::var("GATEWAY_BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8787".to_string());
    let static_root = std::env::var_os("DEEPSEEK_INFRA_STATIC_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("static"));
    let app = create_production_app(&static_root)?;
    let listener = TcpListener::bind(&addr).await?;
    tracing::info!("deepseek-gateway-rs listening on {}", addr);

    axum::serve(listener, app).await?;
    Ok(())
}
