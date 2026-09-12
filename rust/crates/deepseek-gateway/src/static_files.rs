use axum::{
    body::Body,
    extract::Request,
    http::{HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use percent_encoding::percent_decode_str;
use std::{fs as std_fs, io, path::Path, path::PathBuf};
use tokio::fs;
use tower::ServiceExt;
use tower_http::services::ServeFile;

const IMMUTABLE_CACHE_CONTROL: &str = "public, max-age=31536000, immutable";
const CONTENT_SECURITY_POLICY: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: http: https:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'";

#[derive(Clone, Debug)]
pub struct StaticFiles {
    root: PathBuf,
    index: PathBuf,
}

enum FileLookup {
    File(PathBuf),
    Missing,
    Rejected,
}

impl FileLookup {
    fn into_file(self) -> Option<PathBuf> {
        match self {
            Self::File(path) => Some(path),
            Self::Missing | Self::Rejected => None,
        }
    }
}

impl StaticFiles {
    pub fn load(root: impl AsRef<Path>) -> io::Result<Self> {
        let configured_root = root.as_ref();
        let root = std_fs::canonicalize(configured_root).map_err(|error| {
            io::Error::new(
                error.kind(),
                format!(
                    "static root {} is unavailable: {error}",
                    configured_root.display()
                ),
            )
        })?;
        if !root.is_dir() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("static root {} is not a directory", root.display()),
            ));
        }

        let index_candidate = root.join("ui").join("index.html");
        let index = std_fs::canonicalize(&index_candidate).map_err(|error| {
            io::Error::new(
                error.kind(),
                format!(
                    "React frontend build is missing at static/ui/index.html ({}): {error}",
                    index_candidate.display()
                ),
            )
        })?;
        if !index.is_file() || !index.starts_with(&root) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "React frontend build at static/ui/index.html must be a file inside {}",
                    root.display()
                ),
            ));
        }

        Ok(Self { root, index })
    }

    pub async fn serve(&self, request: Request) -> Response {
        let request_path = request.uri().path().to_owned();
        let mut response = if request.method() != Method::GET && request.method() != Method::HEAD {
            method_not_allowed()
        } else if let Some(target) = self.resolve(&request_path).await {
            let response = ServeFile::new(target)
                .oneshot(request)
                .await
                .expect("ServeFile is infallible");
            response.map(Body::new)
        } else {
            StatusCode::NOT_FOUND.into_response()
        };
        apply_frontend_headers(&request_path, &mut response);
        response
    }

    async fn resolve(&self, encoded_path: &str) -> Option<PathBuf> {
        let decoded = percent_decode_str(encoded_path).decode_utf8().ok()?;
        if decoded.contains(['\0', '\\']) {
            return None;
        }

        let parts = decoded
            .split('/')
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>();
        if parts.iter().any(|part| matches!(*part, "." | "..")) {
            return None;
        }
        if parts.is_empty() || parts.as_slice() == ["ui"] {
            return Some(self.index.clone());
        }
        if parts.first() == Some(&"legacy") {
            return None;
        }

        if parts.as_slice() == ["manifest.webmanifest"] {
            return self
                .lookup_file(self.root.join("ui/manifest-root.webmanifest"))
                .await
                .into_file();
        }
        if parts.len() == 1 {
            if let Some(build_id) = service_worker_build_id(parts[0]) {
                return self
                    .lookup_file(self.root.join("ui").join(format!("sw-root-{build_id}.js")))
                    .await
                    .into_file();
            }
        }

        let candidate = parts
            .iter()
            .fold(self.root.clone(), |path, part| path.join(part));
        match self.lookup_file(candidate).await {
            FileLookup::File(path) => Some(path),
            FileLookup::Missing if !has_extension(parts.last().copied().unwrap_or_default()) => {
                Some(self.index.clone())
            }
            FileLookup::Missing | FileLookup::Rejected => None,
        }
    }

    async fn lookup_file(&self, candidate: PathBuf) -> FileLookup {
        match fs::canonicalize(candidate).await {
            Ok(canonical) if canonical.starts_with(&self.root) && canonical.is_file() => {
                FileLookup::File(canonical)
            }
            Ok(_) => FileLookup::Rejected,
            Err(error) if error.kind() == io::ErrorKind::NotFound => FileLookup::Missing,
            Err(_) => FileLookup::Rejected,
        }
    }
}

fn has_extension(segment: &str) -> bool {
    Path::new(segment)
        .extension()
        .is_some_and(|extension| !extension.is_empty())
}

fn service_worker_build_id(name: &str) -> Option<&str> {
    let build_id = name.strip_prefix("sw-")?.strip_suffix(".js")?;
    (build_id.len() == 16
        && build_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))
    .then_some(build_id)
}

fn method_not_allowed() -> Response {
    let mut response = StatusCode::METHOD_NOT_ALLOWED.into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
    response
}

fn apply_frontend_headers(path: &str, response: &mut Response) {
    let headers = response.headers_mut();
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CONTENT_SECURITY_POLICY),
    );
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));

    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    let cache_control = frontend_cache_control(path, content_type);
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static(cache_control),
    );
}

fn frontend_cache_control(path: &str, content_type: &str) -> &'static str {
    if matches!(
        path,
        "/index.html" | "/ui/index.html" | "/ui/workspace-assets.json"
    ) || content_type.to_ascii_lowercase().starts_with("text/html")
    {
        return "no-store";
    }
    if is_build_scoped_frontend_path(path) || is_hashed_frontend_asset(path) {
        return IMMUTABLE_CACHE_CONTROL;
    }
    "no-cache"
}

fn is_build_scoped_frontend_path(path: &str) -> bool {
    let name = path
        .strip_prefix("/ui/")
        .or_else(|| path.strip_prefix('/'))
        .unwrap_or(path);
    if let Some(build_id) = name
        .strip_prefix("sw-root-")
        .and_then(|value| value.strip_suffix(".js"))
        .or_else(|| service_worker_build_id(name))
        .or_else(|| {
            name.strip_prefix("workspace-assets-")
                .and_then(|value| value.strip_suffix(".json"))
        })
    {
        return build_id.len() == 16
            && build_id
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    }
    false
}

fn is_hashed_frontend_asset(path: &str) -> bool {
    let Some(relative) = path.strip_prefix("/ui/assets/") else {
        return false;
    };
    let Some((stem, extension)) = relative.rsplit_once('.') else {
        return false;
    };
    if !matches!(
        extension.to_ascii_lowercase().as_str(),
        "css" | "js" | "mjs" | "png" | "svg" | "webp" | "woff" | "woff2"
    ) {
        return false;
    }
    stem.match_indices('-').any(|(index, _)| {
        let hash = &stem[index + 1..];
        hash.len() >= 8
            && hash
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    })
}
