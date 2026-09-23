//! Native Skill System. The Python skill modules are the offline compatibility oracle.
pub mod analytics;
pub mod catalog;
mod evidence;
pub mod media;
pub mod project_integration;
pub mod registry;
pub mod runner;
pub mod schema;
pub mod security;
pub mod templates;
mod trace;
pub mod versioning;

use crate::app_error::AppError;
use crate::core_utils::{python_truthy, text_or_empty};
use serde_json::Value;

pub type Result<T> = std::result::Result<T, AppError>;
pub fn text(value: &Value, key: &str) -> String {
    text_or_empty(value.get(key))
}
pub fn truth(value: &Value, key: &str) -> bool {
    value.get(key).is_some_and(python_truthy)
}
pub fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .map(|v| text_or_empty(Some(v)).trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}
pub fn hash(value: &Value) -> String {
    use sha2::{Digest, Sha256};
    format!(
        "{:x}",
        Sha256::digest(crate::python_json::dumps_compact(value).as_bytes())
    )
}
pub fn error(message: impl Into<String>, status: u16) -> AppError {
    AppError {
        message: message.into(),
        status,
        code: match status {
            403 => "forbidden",
            404 => "not_found",
            500 => "internal",
            _ => "invalid_payload",
        },
    }
}
