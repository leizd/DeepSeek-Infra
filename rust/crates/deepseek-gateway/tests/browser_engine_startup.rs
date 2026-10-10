//! An available gRPC sidecar must be detected from the same async context used
//! by real chat requests. This transport test launches no browser or provider.

use std::net::TcpListener;
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

use deepseek_browser::engine::EngineSettings;
use deepseek_browser::sidecar::BrowserEngineService;
use deepseek_gateway::browser_engine_client::GrpcBrowserEngine;
use deepseek_policy::browser_engine::{BrowserEngine, EngineFence};
use deepseek_protocol::generated::deepseek::browser::v1::browser_engine_server::BrowserEngineServer;
use tokio::sync::oneshot;

struct StatusServer {
    address: String,
    stop: Option<oneshot::Sender<()>>,
    thread: Option<JoinHandle<()>>,
    _root: tempfile::TempDir,
}

impl StatusServer {
    fn start() -> Self {
        let root = tempfile::tempdir().unwrap();
        let service = BrowserEngineService::new(
            EngineSettings::default(),
            (root.path().join("profiles"), root.path().join("downloads")),
        );
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = format!("http://{}", listener.local_addr().unwrap());
        let (stop, stopped) = oneshot::channel();
        let thread = std::thread::spawn(move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            runtime.block_on(async move {
                let listener = tokio::net::TcpListener::from_std(listener).unwrap();
                let incoming = async_stream::stream! {
                    loop {
                        yield listener.accept().await.map(|(stream, _)| stream);
                    }
                };
                tonic::transport::Server::builder()
                    .add_service(BrowserEngineServer::new(service))
                    .serve_with_incoming_shutdown(incoming, async {
                        let _ = stopped.await;
                    })
                    .await
                    .unwrap();
            });
        });
        Self {
            address,
            stop: Some(stop),
            thread: Some(thread),
            _root: root,
        }
    }
}

impl Drop for StatusServer {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn measured_status(server: &StatusServer) -> Arc<GrpcBrowserEngine> {
    let engine = GrpcBrowserEngine::connect(&server.address)
        .expect("real sidecar was hidden merely because the gateway caller is async");
    let status = engine
        .status(&EngineFence::for_request("owned-status-probe", 1))
        .unwrap();
    assert!(!status.available);
    assert_eq!(status.reason, "DEEPSEEK_BROWSER_CHROMIUM is not set");
    assert!(status.chromium_revision.is_empty());
    engine
}

#[tokio::test(flavor = "current_thread")]
async fn async_chat_context_detects_the_actual_sidecar() {
    let server = StatusServer::start();
    drop(measured_status(&server));
}

#[test]
fn synchronous_startup_still_detects_the_actual_sidecar() {
    let server = StatusServer::start();
    drop(measured_status(&server));
}

#[tokio::test(flavor = "current_thread")]
async fn absent_or_invalid_sidecars_settle_without_panicking() {
    let started = std::time::Instant::now();
    assert!(GrpcBrowserEngine::connect("http://127.0.0.1:1").is_none());
    assert!(GrpcBrowserEngine::connect("invalid endpoint").is_none());
    assert!(started.elapsed() < Duration::from_secs(6));
}
