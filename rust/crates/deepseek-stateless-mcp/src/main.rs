use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use deepseek_stateless_mcp::http::{AppState, ServiceConfig, router};
use deepseek_stateless_mcp::model::TaskOutcome;
use deepseek_stateless_mcp::runner::execute_configured_task;
use deepseek_stateless_mcp::store::TaskBackend;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let redis_url = nonempty("REDIS_URL");
    let host = env_string("MCP_HOST", "0.0.0.0");
    let port: u16 = env_string("MCP_PORT", "8010").parse()?;
    let instance_id =
        std::env::var("MCP_INSTANCE_ID").unwrap_or_else(|_| format!("mcp-{}", std::process::id()));
    let workspace_root = PathBuf::from(env_string("MCP_WORKSPACE_ROOT", "."));
    let allowed_hosts = env_string("MCP_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],mcp-lb")
        .split(',')
        .map(|item| item.trim().to_string())
        .filter(|item| !item.is_empty())
        .collect::<Vec<_>>();
    let config = ServiceConfig {
        instance_id: instance_id.clone(),
        workspace_root: workspace_root.clone(),
        allowed_hosts,
        auth_token: nonempty("MCP_AUTH_TOKEN"),
        internal_backup_token: nonempty("MCP_INTERNAL_BACKUP_TOKEN"),
        durable_task_state: TaskBackend::advertised_durable_task_state(redis_url.is_some())
            .to_string(),
        max_output_bytes: env_string("MCP_MAX_OUTPUT_BYTES", "262144")
            .parse()
            .unwrap_or(262_144),
        task_timeout_seconds: env_string("MCP_TASK_TIMEOUT_SECONDS", "600")
            .parse()
            .unwrap_or(600),
        lease_ms: env_string("MCP_TASK_LEASE_MS", "15000")
            .parse()
            .unwrap_or(15_000),
    };
    let lease_ms = config.lease_ms;
    let poll_ms = env_string("MCP_TASK_POLL_MS", "250")
        .parse::<u64>()
        .unwrap_or(250);
    let timeout_seconds = config.task_timeout_seconds;
    let max_output_bytes = config.max_output_bytes;
    let store = Arc::new(match redis_url {
        Some(url) => {
            TaskBackend::connect_redis(&url, &env_string("REDIS_PREFIX", "deepseek-infra:mcp:v1"))?
        }
        None => TaskBackend::memory(),
    });
    let worker_store = Arc::clone(&store);
    let worker_root = workspace_root.clone();
    tokio::spawn(async move {
        loop {
            let claimed = tokio::task::block_in_place(|| {
                worker_store.claim(&instance_id, now_millis(), lease_ms)
            })
            .ok()
            .flatten();
            if let Some(task) = claimed {
                let limit = timeout_seconds.min(task.arguments.timeout_seconds);
                let task_id = task.id.clone();
                let cancel = Arc::new(AtomicBool::new(false));
                let heartbeat = tokio::spawn({
                    let store = Arc::clone(&worker_store);
                    let cancel = Arc::clone(&cancel);
                    let task_id = task_id.clone();
                    let instance_id = instance_id.clone();
                    async move {
                        let step = (lease_ms / 3).max(250) as u64;
                        loop {
                            tokio::time::sleep(Duration::from_millis(step)).await;
                            let owned = tokio::task::block_in_place(|| {
                                store.heartbeat(&task_id, &instance_id, now_millis(), lease_ms)
                            })
                            .unwrap_or(false);
                            if !owned {
                                cancel.store(true, Ordering::Relaxed);
                                break;
                            }
                        }
                    }
                });
                let outcome = tokio::task::spawn_blocking({
                    let root = worker_root.clone();
                    let target = task.arguments.target.clone();
                    let keyword = task.arguments.keyword.clone();
                    let markers = task.arguments.markers.clone();
                    let cancel = Arc::clone(&cancel);
                    move || {
                        execute_configured_task(
                            &root,
                            &target,
                            keyword.as_deref(),
                            markers.as_deref(),
                            limit,
                            max_output_bytes,
                            &cancel,
                        )
                    }
                })
                .await
                .unwrap_or_else(|error| TaskOutcome {
                    stdout: String::new(),
                    stderr: String::new(),
                    exit_code: None,
                    error: Some(error.to_string()),
                });
                heartbeat.abort();
                if !cancel.load(Ordering::Relaxed) {
                    let _ = tokio::task::block_in_place(|| {
                        worker_store.complete(&task_id, &instance_id, outcome, now_millis())
                    });
                }
                continue;
            }
            tokio::time::sleep(Duration::from_millis(poll_ms)).await;
        }
    });
    let app = router(AppState { store, config });
    let listener = tokio::net::TcpListener::bind((host.as_str(), port)).await?;
    println!("stateless_mcp_listening host={host} port={port}");
    axum::serve(listener, app).await?;
    Ok(())
}

fn env_string(name: &str, fallback: &str) -> String {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

fn nonempty(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn now_millis() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or(0)
}
