//! Read-only transfer budget projection: `GET /api/workspace/backup-transfer-budget` (S1).
//!
//! The original answer comes from the *process-local* `TransferBudgetManager`
//! singleton merged with the durable `qos_transfers` rows:
//!
//! ```text
//! durable = {t["transferId"]: t for t in backup_control.list_qos_transfers()}
//! for id, transfer in self._active_transfers.items():
//!     durable.setdefault(id, {...})   # in-memory entries only fill gaps
//! ```
//!
//! Two properties decide this port's shape:
//!
//! * the durable read is `list_qos_transfers`, which first `DELETE`s expired
//!   leases. A read-only native route must not delete, so expired rows are
//!   filtered out with the same `lease_until < now` predicate instead — the
//!   answer is identical and the store is untouched;
//! * the in-memory half only exists inside the Python process that is currently
//!   transferring. A separate native process can never observe it, and no other
//!   route exposes it, so the native answer is the durable projection. That is
//!   recorded as the slice's narrowing rather than papered over with a probe.
//!
//! Consequently this module also does not create `.backup-control/` or its
//! schema: a workspace that has never recorded a transfer answers the same body
//! the original does after its own store was initialized.

use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{Map, Value, json};

use crate::route_support;

const DEFAULT_GLOBAL_BYTES_PER_SECOND: i64 = 200 * 1024 * 1024;
const DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC: i64 = 100 * 1024 * 1024;
const DEFAULT_RESERVED_REPAIR_BYTES_PER_SEC: i64 = 100 * 1024 * 1024;
const MINIMUM_GLOBAL_BYTES_PER_SECOND: i64 = 1024 * 1024;

pub fn router() -> Router {
    Router::new().route("/api/workspace/backup-transfer-budget", any(dispatch))
}

async fn dispatch(method: Method) -> Response {
    match method {
        Method::GET | Method::HEAD => {
            let joined = tokio::task::spawn_blocking(budget_body).await;
            match joined {
                Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
                Ok(Ok(body)) => Json(body).into_response(),
                Ok(Err(error)) => route_support::app_error_response(error),
                Err(_) if method == Method::HEAD => {
                    StatusCode::INTERNAL_SERVER_ERROR.into_response()
                }
                Err(_) => route_support::app_error_response(route_support::unhandled_error()),
            }
        }
        Method::POST => route_support::route_not_found(),
        Method::OPTIONS => route_support::options_response(),
        _ => route_support::method_not_allowed(),
    }
}

fn budget_body() -> Result<Value, AppError> {
    let transfers = list_active_qos_transfers()?;

    let mut p0_count = 0_i64;
    let mut repair_count = 0_i64;
    let mut rebalance_backlog: i64 = 0;
    for transfer in &transfers {
        match transfer.traffic_class {
            0 => p0_count += 1,
            2 | 3 => repair_count += 1,
            5 | 6 => {
                rebalance_backlog = rebalance_backlog.saturating_add(
                    transfer
                        .estimated_bytes
                        .saturating_sub(transfer.bytes_transferred)
                        .max(0),
                );
            }
            _ => {}
        }
    }

    // `TransferBudgetManager()` defaults: the global rate is floored at 1 MiB/s,
    // the recovery reserve is floored at 0, and the repair reserve is capped at
    // half of the global rate.
    let global = DEFAULT_GLOBAL_BYTES_PER_SECOND.max(MINIMUM_GLOBAL_BYTES_PER_SECOND);
    let recovery = DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC.max(0);
    let repair = DEFAULT_RESERVED_REPAIR_BYTES_PER_SEC.min(global / 2).max(0);

    let mut body = Map::new();
    body.insert(
        "activeTransfersTotal".to_string(),
        json!(transfers.len() as i64),
    );
    body.insert("activeRecoveryTransfers".to_string(), json!(p0_count));
    body.insert("activeRepairTransfers".to_string(), json!(repair_count));
    body.insert(
        "rebalanceBacklogBytes".to_string(),
        json!(rebalance_backlog),
    );
    body.insert(
        "backgroundThrottleActive".to_string(),
        Value::Bool(p0_count > 0),
    );
    body.insert("globalBytesPerSecond".to_string(), json!(global));
    body.insert("globalBandwidthBytesPerSec".to_string(), json!(global));
    body.insert(
        "reservedRecoveryBytesPerSecond".to_string(),
        json!(recovery),
    );
    body.insert(
        "reservedDrBandwidthBytesPerSec".to_string(),
        json!(recovery),
    );
    body.insert("repairReservedBytesPerSecond".to_string(), json!(repair));
    Ok(Value::Object(body))
}

struct QosTransfer {
    traffic_class: i64,
    estimated_bytes: i64,
    bytes_transferred: i64,
}

fn control_database() -> PathBuf {
    crate::data_routes::workspace_root()
        .join(".backup-control")
        .join("control.sqlite3")
}

/// `list_qos_transfers()` without its `DELETE`: expired leases are excluded by
/// the same predicate, so the caller sees exactly the surviving set.
fn list_active_qos_transfers() -> Result<Vec<QosTransfer>, AppError> {
    let database = control_database();
    if !database.is_file() {
        return Ok(Vec::new());
    }
    let directory = database.parent().map(|path| path.to_path_buf());
    let side_files = directory
        .as_ref()
        .map(|dir| {
            dir.join("control.sqlite3-wal").exists() || dir.join("control.sqlite3-shm").exists()
        })
        .unwrap_or(false);
    let connection = if side_files {
        Connection::open_with_flags(
            &database,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
    } else {
        // A cleanly closed WAL database re-creates `-wal`/`-shm` on a plain
        // read-only open; `immutable=1` reads it without touching the writer's
        // files. When the writer's side files exist they are left in place.
        Connection::open_with_flags(
            sqlite_immutable_uri(&database),
            OpenFlags::SQLITE_OPEN_READ_ONLY
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_URI,
        )
    }
    .map_err(|_| unhandled())?;
    let _ = connection.busy_timeout(Duration::from_secs(30));
    let table: Option<String> = connection
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'qos_transfers'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled())?;
    if table.is_none() {
        return Ok(Vec::new());
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0);
    let mut statement = connection
        .prepare(
            "SELECT traffic_class, estimated_bytes, bytes_transferred \
             FROM qos_transfers WHERE lease_until >= ?1 ORDER BY transfer_id",
        )
        .map_err(|_| unhandled())?;
    let rows = statement
        .query_map([now], |row| {
            Ok(QosTransfer {
                traffic_class: row.get(0)?,
                estimated_bytes: row.get::<_, Option<i64>>(1)?.unwrap_or(0),
                bytes_transferred: row.get::<_, Option<i64>>(2)?.unwrap_or(0),
            })
        })
        .map_err(|_| unhandled())?;
    let mut transfers = Vec::new();
    for row in rows {
        transfers.push(row.map_err(|_| unhandled())?);
    }
    Ok(transfers)
}

/// A `file:` URI for `immutable=1`, which reads a cleanly closed WAL database
/// without creating the writer's `-wal`/`-shm` side files.
fn sqlite_immutable_uri(path: &std::path::Path) -> String {
    let text = path.to_string_lossy().replace('\\', "/");
    let mut encoded = String::with_capacity(text.len() + 20);
    for character in text.chars() {
        match character {
            '%' => encoded.push_str("%25"),
            '?' => encoded.push_str("%3F"),
            '#' => encoded.push_str("%23"),
            ' ' => encoded.push_str("%20"),
            other => encoded.push(other),
        }
    }
    if encoded.starts_with('/') {
        format!("file://{encoded}?immutable=1")
    } else {
        format!("file:///{encoded}?immutable=1")
    }
}

fn unhandled() -> AppError {
    route_support::unhandled_error()
}

#[cfg(test)]
mod tests {
    use super::{
        DEFAULT_GLOBAL_BYTES_PER_SECOND, DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC,
        DEFAULT_RESERVED_REPAIR_BYTES_PER_SEC, MINIMUM_GLOBAL_BYTES_PER_SECOND, QosTransfer,
    };

    /// The `TransferBudgetManager.__init__` clamps, which decide the four rate
    /// fields of the body.
    fn rates() -> (i64, i64, i64) {
        let global = DEFAULT_GLOBAL_BYTES_PER_SECOND.max(MINIMUM_GLOBAL_BYTES_PER_SECOND);
        let recovery = DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC.max(0);
        let repair = DEFAULT_RESERVED_REPAIR_BYTES_PER_SEC.min(global / 2).max(0);
        (global, recovery, repair)
    }

    #[test]
    fn default_rates_match_the_original_manager() {
        let (global, recovery, repair) = rates();
        assert_eq!(global, 209_715_200);
        assert_eq!(recovery, 104_857_600);
        assert_eq!(repair, 104_857_600);
    }

    #[test]
    fn rebalance_backlog_counts_only_background_classes() {
        let transfers = [
            QosTransfer {
                traffic_class: 5,
                estimated_bytes: 900,
                bytes_transferred: 100,
            },
            QosTransfer {
                traffic_class: 6,
                estimated_bytes: 400,
                bytes_transferred: 50,
            },
            // Already complete: `max(0, 0 - 0)` contributes nothing.
            QosTransfer {
                traffic_class: 5,
                estimated_bytes: 10,
                bytes_transferred: 10,
            },
            // Another class never contributes.
            QosTransfer {
                traffic_class: 2,
                estimated_bytes: 1_000,
                bytes_transferred: 0,
            },
        ];
        let mut backlog: i64 = 0;
        let mut p0 = 0;
        let mut repair = 0;
        for transfer in &transfers {
            match transfer.traffic_class {
                0 => p0 += 1,
                2 | 3 => repair += 1,
                5 | 6 => {
                    backlog = backlog.saturating_add(
                        transfer
                            .estimated_bytes
                            .saturating_sub(transfer.bytes_transferred)
                            .max(0),
                    );
                }
                _ => {}
            }
        }
        assert_eq!(backlog, 800 + 350);
        assert_eq!(p0, 0);
        assert_eq!(repair, 1);
    }
}
