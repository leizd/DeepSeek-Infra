//! Ephemeral in-memory secret slots for S5 backup and restore unlock operations.
//!
//! Mirrors `deepseek_infra.infra.workspace.backup_crypto` (`MAX_SECRET_BYTES`,
//! `SECRET_TTL_SECONDS`, `SECRET_ATTEMPTS`): passphrases and Age identities live
//! strictly in process memory with bounded attempts and explicit zeroization on
//! expiry, overwrite, clear, or drop.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use deepseek_policy::app_error::{AppError, codes};
use serde_json::{Value, json};

pub const MAX_SECRET_BYTES: usize = 64 * 1024;
pub const SECRET_TTL_SECONDS: u64 = 5 * 60;
pub const SECRET_ATTEMPTS: usize = 5;

pub(crate) struct SecretBytes(Vec<u8>);

impl SecretBytes {
    pub(crate) fn as_slice(&self) -> &[u8] {
        &self.0
    }

    pub(crate) fn as_str(&self) -> Result<&str, AppError> {
        std::str::from_utf8(&self.0)
            .map_err(|_| AppError::invalid_payload("Backup secret must be valid UTF-8"))
    }
}

impl Drop for SecretBytes {
    fn drop(&mut self) {
        self.0.fill(0);
    }
}

struct EphemeralSlot {
    kind: String,
    value: Vec<u8>,
    expires_at: Instant,
}

impl Drop for EphemeralSlot {
    fn drop(&mut self) {
        self.value.fill(0);
    }
}

#[derive(Default)]
struct Registry {
    slots: HashMap<String, EphemeralSlot>,
    failures: HashMap<String, (usize, Instant)>,
}

impl Registry {
    fn prune(&mut self, now: Instant) {
        self.slots.retain(|_, slot| slot.expires_at > now);
        self.failures.retain(|_, (_, expires_at)| *expires_at > now);
    }
}

static REGISTRY: LazyLock<Mutex<Registry>> = LazyLock::new(|| Mutex::new(Registry::default()));

pub(crate) fn put_secret(session_id: &str, kind: &str, secret: &str) -> Result<Value, AppError> {
    put_secret_bytes(session_id, kind, secret.as_bytes())
}

pub(crate) fn put_secret_bytes(
    session_id: &str,
    kind: &str,
    encoded: &[u8],
) -> Result<Value, AppError> {
    if kind != "passphrase" && kind != "age-identity" {
        return Err(AppError::invalid_payload("Unsupported backup secret kind"));
    }
    if encoded.is_empty() || encoded.len() > MAX_SECRET_BYTES || encoded.contains(&0) {
        return Err(AppError::invalid_payload("Backup secret is invalid"));
    }
    let now = Instant::now();
    let ttl = Duration::from_secs(SECRET_TTL_SECONDS);
    let mut guard = REGISTRY
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    guard.prune(now);
    let failures = guard
        .failures
        .get(session_id)
        .map(|(count, _)| *count)
        .unwrap_or(0);
    if failures >= SECRET_ATTEMPTS {
        return Err(AppError {
            message: "Unable to unlock backup".to_string(),
            code: codes::INVALID_REQUEST,
            status: 429,
        });
    }
    guard.slots.insert(
        session_id.to_string(),
        EphemeralSlot {
            kind: kind.to_string(),
            value: encoded.to_vec(),
            expires_at: now + ttl,
        },
    );
    Ok(json!({
        "ok": true,
        "sessionId": session_id,
        "kind": kind,
        "expiresInSeconds": SECRET_TTL_SECONDS,
        "attemptsRemaining": SECRET_ATTEMPTS - failures,
    }))
}

pub(crate) fn consume_secret(
    session_id: &str,
    expected_kind: Option<&str>,
) -> Result<(String, SecretBytes), AppError> {
    let now = Instant::now();
    let mut guard = REGISTRY
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    guard.prune(now);
    let Some(mut slot) = guard.slots.remove(session_id) else {
        return Err(AppError {
            message: "Backup secret is required or expired".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    };
    if let Some(expected) = expected_kind {
        if slot.kind != expected {
            // Drop zeroizes `slot.value` immediately.
            return Err(AppError {
                message: "Backup secret is required or expired".to_string(),
                code: codes::INVALID_REQUEST,
                status: 409,
            });
        }
    }
    let kind = slot.kind.clone();
    let value = std::mem::take(&mut slot.value);
    Ok((kind, SecretBytes(value)))
}

pub(crate) fn record_unlock_failure(session_id: &str) {
    let now = Instant::now();
    let ttl = Duration::from_secs(SECRET_TTL_SECONDS);
    let mut guard = REGISTRY
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    guard.prune(now);
    let count = guard
        .failures
        .get(session_id)
        .map(|(existing, _)| *existing)
        .unwrap_or(0);
    guard
        .failures
        .insert(session_id.to_string(), (count + 1, now + ttl));
}

pub(crate) fn clear_secret(session_id: &str) {
    let mut guard = REGISTRY
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    guard.slots.remove(session_id);
}

pub(crate) fn has_secret(session_id: &str) -> bool {
    let now = Instant::now();
    let mut guard = REGISTRY
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    guard.prune(now);
    guard.slots.contains_key(session_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consume_and_failure_limit_behave_fail_closed() {
        let id = format!("unit_slot_{}", std::process::id());
        clear_secret(&id);
        put_secret(&id, "passphrase", "pw-1").unwrap();
        assert!(has_secret(&id));
        let (kind, bytes) = consume_secret(&id, Some("passphrase")).unwrap();
        assert_eq!(kind, "passphrase");
        assert_eq!(bytes.as_slice(), b"pw-1");
        assert!(!has_secret(&id));
        assert_eq!(consume_secret(&id, None).err().unwrap().status, 409);

        put_secret(&id, "age-identity", "AGE-SECRET-KEY-1X").unwrap();
        assert_eq!(
            consume_secret(&id, Some("passphrase"))
                .err()
                .unwrap()
                .status,
            409
        );
        assert!(!has_secret(&id));

        for _ in 0..SECRET_ATTEMPTS {
            record_unlock_failure(&id);
        }
        assert_eq!(
            put_secret(&id, "passphrase", "pw-2").unwrap_err().status,
            429
        );
    }
}
