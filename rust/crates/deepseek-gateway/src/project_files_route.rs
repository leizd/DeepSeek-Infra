//! `POST /api/project-files` — attach an uploaded file to one project.
//!
//! The production entry is this route. While Python still owns
//! `project_metadata_store`, the handler returns before parsing the body and
//! writes nothing. Once `DEEPSEEK_RUNTIME_MODE=python_disabled` de-authorises
//! Python, the same request extracts the file into `.projects/<id>/files` and
//! records it in `project.json`. `GET /api/workspace/projects/<id>` reads that
//! record back.

use std::path::PathBuf;

use axum::Json;
use axum::body::Bytes;
use axum::extract::Query;
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::projects;
use serde::Deserialize;
use serde_json::json;

use crate::file_text_route::{declared_content_length, read_multipart_upload};
use crate::may_write_native_store;

#[derive(Debug, Default, Deserialize)]
pub(crate) struct ProjectFilesQuery {
    #[serde(default, rename = "projectId")]
    project_id: String,
}

pub(crate) async fn api_project_files(
    Query(query): Query<ProjectFilesQuery>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let native_owner = may_write_native_store("project_metadata_store");
    let ownership = projects::admit_project_metadata_write(native_owner);
    if !ownership.allowed {
        return app_error(AppError {
            message: ownership.message,
            code: ownership.code,
            status: ownership.status,
        });
    }
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let content_length = match declared_content_length(
        headers
            .get(header::CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok()),
        body.len(),
    ) {
        Ok(length) => length,
        Err(error) => return app_error(error),
    };
    let (files, ocr_enabled) = match read_multipart_upload(content_type, content_length, &body) {
        Ok(parsed) => parsed,
        Err(error) => return app_error(error),
    };
    if files.is_empty() {
        return app_error(AppError {
            message: "No file uploaded".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    for file in &files {
        let decision = projects::admit_project_file_part(
            native_owner,
            &file.filename,
            &file.content_type,
            &file.data,
            ocr_enabled,
        );
        if !decision.attach {
            return app_error(AppError {
                message: decision.message,
                code: decision.code,
                status: decision.status,
            });
        }
    }
    let root = root();
    let project_id = query.project_id;
    let control = deepseek_policy::extraction_control::FileProcessingControl::default();
    let _cancel_on_drop = control.cancel_on_drop();
    match tokio::task::spawn_blocking(move || {
        control.run(|| {
            projects::add_project_files(&project_id, &files, ocr_enabled, &root, &SystemEntropy)
        })
    })
    .await
    {
        Ok(Ok(documents)) => Json(json!({"ok": true, "documents": documents})).into_response(),
        Ok(Err(error)) => app_error(error),
        Err(_) => app_error(AppError {
            message: "Project file upload failed".to_string(),
            code: codes::INTERNAL,
            status: 500,
        }),
    }
}

fn root() -> PathBuf {
    std::env::var_os("DEEPSEEK_INFRA_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn app_error(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
