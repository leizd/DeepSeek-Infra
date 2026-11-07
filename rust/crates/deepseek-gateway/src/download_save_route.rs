//! `POST /api/download-save`.
//!
//! Copies an already-generated file from `.generated/` into the user's Downloads
//! folder and answers with the original
//! `{"ok", "filename", "path", "downloadUrl"}` envelope. Id resolution,
//! traversal safety and the download URL live in `deepseek_policy::generated_files`
//! (shared with `GET /api/download`); this module ports the filename cleanup,
//! the collision suffix and the platform home lookup.

use std::path::{Path, PathBuf};

use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::text_or_empty;
use deepseek_policy::generated_files::{
    GENERATED_EXTENSIONS, download_url, resolve_generated_file, safe_filename,
};
use serde_json::{Value, json};

use crate::route_support;

/// `read_json_body(request)`'s default bound in the original route.
const MAX_JSON_BODY_BYTES: i64 = 2_000_000;

pub fn router() -> Router {
    Router::new().route("/api/download-save", any(dispatch))
}

async fn dispatch(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method == Method::POST {
        let root = crate::data_routes::workspace_root();
        let joined = tokio::task::spawn_blocking(move || save(&headers, &body, &root)).await;
        return match joined {
            Ok(Ok(value)) => Json(value).into_response(),
            Ok(Err(error)) => route_support::app_error_response(error),
            Err(_) => route_support::app_error_response(route_support::unhandled_error()),
        };
    }
    if method == Method::GET || method == Method::HEAD {
        return route_support::route_not_found();
    }
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    route_support::method_not_allowed()
}

fn save(headers: &HeaderMap, body: &[u8], root: &Path) -> Result<Value, AppError> {
    let payload = route_support::read_json_object(headers, body, MAX_JSON_BODY_BYTES)?;
    let file_id = text_or_empty(payload.get("id"));
    let filename = text_or_empty(payload.get("filename"));
    let source = resolve_generated_file(root, &file_id).ok_or_else(|| AppError {
        message: "文件不存在或已过期".to_string(),
        code: codes::NOT_FOUND,
        status: 404,
    })?;
    let extension = source
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let downloads = downloads_dir()?;
    std::fs::create_dir_all(&downloads).map_err(|_| output_failure())?;
    let target = unique_download_path(&downloads, &download_filename(&filename, &extension));
    std::fs::copy(&source, &target).map_err(|_| output_failure())?;
    let name = target
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_string();
    Ok(json!({
        "ok": true,
        "filename": name,
        "path": target.to_string_lossy().to_string(),
        "downloadUrl": download_url(&file_id),
    }))
}

/// `_downloads_dir`: `Path.home() / "Downloads"`.
fn downloads_dir() -> Result<PathBuf, AppError> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from));
    Ok(home
        .ok_or_else(route_support::unhandled_error)?
        .join("Downloads"))
}

/// `_download_filename`: the disk extension wins; the client name contributes
/// only its base after the original cleanup.
fn download_filename(value: &str, extension: &str) -> String {
    let mut raw = value.trim().to_string();
    let lowered = raw.to_ascii_lowercase();
    for registered in GENERATED_EXTENSIONS {
        let suffix = format!(".{registered}");
        if lowered.ends_with(&suffix) {
            raw.truncate(raw.len() - suffix.len());
            break;
        }
    }
    if let Some(rest) = raw
        .strip_prefix("点击下载")
        .or_else(|| raw.strip_prefix("下载"))
    {
        raw = rest.trim_start_matches(char::is_whitespace).to_string();
    }
    format!("{}.{extension}", safe_filename(&raw))
}

/// `_unique_download_path`: `name.ext`, then `name (2).ext`, ...
fn unique_download_path(directory: &Path, filename: &str) -> PathBuf {
    let (base, suffix) = match filename.rfind('.') {
        Some(index) if index > 0 => (&filename[..index], &filename[index..]),
        _ => (filename, ""),
    };
    let base = if base.is_empty() { "document" } else { base };
    let suffix = if suffix.is_empty() { ".bin" } else { suffix };
    let mut candidate = directory.join(format!("{base}{suffix}"));
    let mut index = 2;
    while candidate.exists() {
        candidate = directory.join(format!("{base} ({index}){suffix}"));
        index += 1;
    }
    candidate
}

fn output_failure() -> AppError {
    route_support::unhandled_error()
}

#[cfg(test)]
mod tests {
    use super::{download_filename, unique_download_path};
    use std::path::PathBuf;

    #[test]
    fn download_filename_keeps_the_disk_extension_and_cleans_the_client_name() {
        assert_eq!(download_filename("报告", "pptx"), "报告.pptx");
        assert_eq!(download_filename("报告.pptx", "pptx"), "报告.pptx");
        assert_eq!(
            download_filename("点击下载 幻灯片.docx", "docx"),
            "幻灯片.docx"
        );
        assert_eq!(download_filename("", "pptx"), "document.pptx");
        assert_eq!(download_filename("", "svg"), "document.svg");
        assert_eq!(download_filename("", "md"), "document.md");
        assert_eq!(download_filename("a/b:c*d?", "pdf"), "abcd.pdf");
    }

    #[test]
    fn unique_download_path_appends_the_original_counter() {
        let directory = tempfile::tempdir().expect("dir");
        let first = unique_download_path(directory.path(), "notes.md");
        assert_eq!(first, directory.path().join("notes.md"));
        std::fs::write(&first, b"x").unwrap();
        let second = unique_download_path(directory.path(), "notes.md");
        assert_eq!(second, directory.path().join("notes (2).md"));
    }

    #[test]
    fn empty_suffix_falls_back_to_bin() {
        let temporary = tempfile::tempdir().expect("dir");
        assert_eq!(
            unique_download_path(temporary.path(), "name"),
            temporary.path().join("name.bin")
        );
    }
}
