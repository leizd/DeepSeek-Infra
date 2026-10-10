//! Real HTTP coverage for `POST /api/file-text`.
//!
//! A legal text upload is extracted, cached, and readable again through the
//! production `/api/file-reader` and `/api/file-source` routes. An empty body, a
//! non-multipart body, an empty file and a binary file are refused and leave no
//! cache entry.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-file-text-token";

static ENV_BUSY: AtomicBool = AtomicBool::new(false);

struct EnvLock;
impl EnvLock {
    fn acquire() -> Self {
        while ENV_BUSY
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            std::thread::yield_now();
        }
        Self
    }
}
impl Drop for EnvLock {
    fn drop(&mut self) {
        ENV_BUSY.store(false, Ordering::Release);
    }
}

struct EnvGuard {
    saved: Vec<(&'static str, Option<String>)>,
}
impl EnvGuard {
    fn set(pairs: &[(&'static str, String)]) -> Self {
        let saved = pairs
            .iter()
            .map(|(name, value)| {
                let previous = std::env::var(name).ok();
                unsafe { std::env::set_var(name, value) }
                (*name, previous)
            })
            .collect();
        Self { saved }
    }
}
impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            match value {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
    }
}

struct Fixture {
    root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("OCR_FORMULA_CMD", "cmd /c exit 1".to_string()),
        ]);
        let static_root = tempfile::tempdir().expect("static");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _env: env,
            app,
        }
    }

    async fn post(
        &self,
        content_type: &str,
        body: Vec<u8>,
        auth: bool,
    ) -> (StatusCode, Value, Vec<u8>) {
        let mut request = Request::builder()
            .method("POST")
            .uri("/api/file-text")
            .header(header::CONTENT_TYPE, content_type)
            .header(header::CONTENT_LENGTH, body.len().to_string());
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::from(body)).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let json = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, json, bytes.to_vec())
    }

    fn cache_files(&self) -> Vec<String> {
        let dir = self.root.path().join(".file-cache");
        let Ok(entries) = std::fs::read_dir(dir) else {
            return Vec::new();
        };
        let mut names: Vec<String> = entries
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }
}

fn multipart(filename: &str, content_type: &str, data: &[u8]) -> (String, Vec<u8>) {
    multipart_with_ocr(filename, content_type, data, false)
}

fn multipart_with_ocr(
    filename: &str,
    content_type: &str,
    data: &[u8],
    ocr: bool,
) -> (String, Vec<u8>) {
    let boundary = "DeepseekBoundary";
    let mut body = Vec::new();
    if ocr {
        body.extend(
            format!(
                "--{boundary}\r\nContent-Disposition: form-data; name=\"ocrEnabled\"\r\n\r\n1\r\n"
            )
            .into_bytes(),
        );
    }
    body.extend(
        format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
        )
        .into_bytes(),
    );
    body.extend(data);
    body.extend(format!("\r\n--{boundary}--\r\n").into_bytes());
    (format!("multipart/form-data; boundary={boundary}"), body)
}

#[tokio::test]
async fn a_text_upload_is_cached_and_readable_again() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let (content_type, body) = multipart("notes.txt", "text/plain", b"hello\nworld");
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["name"], "notes.txt");
    assert_eq!(json["file"]["kind"], "txt");
    assert_eq!(json["file"]["text"], "hello\nworld");
    assert_eq!(json["file"]["preview"], "hello\nworld");
    assert_eq!(json["file"]["charCount"], 11);
    assert_eq!(json["file"]["chunkCount"], 1);
    assert_eq!(json["file"]["chunked"], false);
    assert_eq!(json["file"]["sourceAvailable"], true);
    assert_eq!(json["files"][0]["text"], "hello\nworld");
    assert_eq!(json["errors"], json!([]));
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    assert_eq!(file_id.len(), 32);

    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), b"hello\nworld");

    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(json!({"fileId": file_id}).to_string()))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(reader_body["chunks"][0]["text"], "hello\nworld");
    assert!(
        fixture
            .cache_files()
            .iter()
            .any(|name| name.ends_with(".json"))
    );
}

#[tokio::test]
async fn illegal_uploads_write_nothing() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();

    let (status, json, _) = fixture
        .post("application/json", br#"{"x":1}"#.to_vec(), true)
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{json}");
    assert_eq!(json["error"], "Expected multipart/form-data");

    let (content_type, body) = multipart("empty.txt", "text/plain", b"");
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{json}");
    assert_eq!(json["error"], "Uploaded file is empty: empty.txt");

    let (content_type, body) = multipart("blob.bin", "application/octet-stream", b"a\0b");
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNSUPPORTED_MEDIA_TYPE, "{json}");
    assert_eq!(json["code"], "unsupported_file");

    let (content_type, body) = multipart("scan.png", "image/png", b"\x89PNG");
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNSUPPORTED_MEDIA_TYPE, "{json}");
    assert_eq!(json["code"], "ocr_required");

    let (content_type, body) = multipart("page.pdf", "application/pdf", b"%PDF-1.4");
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{json}");
    assert_eq!(json["error"], "Could not extract text from this PDF");
    assert_eq!(json["code"], "invalid_payload");

    let blank = simple_pdf(&[vec![]]);
    let (content_type, body) = multipart("blank.pdf", "application/pdf", &blank);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{json}");
    assert_eq!(json["code"], "ocr_required");

    let (content_type, body) = multipart("bad.epub", "application/epub+zip", b"not a zip");
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{json}");
    assert_eq!(json["error"], "Invalid epub file");
    assert_eq!(json["code"], "invalid_payload");

    let nav_only = stored_zip(&[("nav.xhtml", b"<p>SECRET NAV</p>")]);
    let (content_type, body) = multipart("empty.epub", "application/epub+zip", &nav_only);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{json}");
    assert_eq!(json["error"], "No readable text found in this file");

    let (content_type, body) = multipart(
        "bad.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        b"not a zip",
    );
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{json}");
    assert_eq!(json["error"], "Invalid xlsx file");
    assert_eq!(json["code"], "invalid_payload");

    let (content_type, body) = multipart(
        "bad.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        b"not a zip",
    );
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{json}");
    assert_eq!(json["error"], "Invalid pptx file");
    assert_eq!(json["code"], "invalid_payload");

    let (content_type, body) = multipart(
        "bad.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"not a zip",
    );
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{json}");
    assert_eq!(json["error"], "Invalid docx file");
    assert_eq!(json["code"], "invalid_payload");

    let (content_type, body) = multipart("notes.txt", "text/plain", b"secret");
    let (status, _, _) = fixture.post(&content_type, body, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert!(fixture.cache_files().is_empty());
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for byte in data {
        crc ^= u32::from(*byte);
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !crc
}

fn stored_zip(files: &[(&str, &[u8])]) -> Vec<u8> {
    let mut locals = Vec::new();
    let mut central = Vec::new();
    let mut offset = 0u32;
    for (name, data) in files {
        let name_bytes = name.as_bytes();
        let crc = crc32(data);
        let size = data.len() as u32;
        let mut local = Vec::new();
        local.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        local.extend_from_slice(&20u16.to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local.extend_from_slice(&crc.to_le_bytes());
        local.extend_from_slice(&size.to_le_bytes());
        local.extend_from_slice(&size.to_le_bytes());
        local.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local.extend_from_slice(name_bytes);
        local.extend_from_slice(data);
        let mut entry = Vec::new();
        entry.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        entry.extend_from_slice(&20u16.to_le_bytes());
        entry.extend_from_slice(&20u16.to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&crc.to_le_bytes());
        entry.extend_from_slice(&size.to_le_bytes());
        entry.extend_from_slice(&size.to_le_bytes());
        entry.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&0u16.to_le_bytes());
        entry.extend_from_slice(&0u32.to_le_bytes());
        entry.extend_from_slice(&offset.to_le_bytes());
        entry.extend_from_slice(name_bytes);
        offset += local.len() as u32;
        locals.extend(local);
        central.extend(entry);
    }
    let directory_offset = locals.len() as u32;
    let directory_size = central.len() as u32;
    let mut output = locals;
    output.extend(central);
    output.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    output.extend_from_slice(&0u16.to_le_bytes());
    output.extend_from_slice(&0u16.to_le_bytes());
    output.extend_from_slice(&(files.len() as u16).to_le_bytes());
    output.extend_from_slice(&(files.len() as u16).to_le_bytes());
    output.extend_from_slice(&directory_size.to_le_bytes());
    output.extend_from_slice(&directory_offset.to_le_bytes());
    output.extend_from_slice(&0u16.to_le_bytes());
    output
}

#[tokio::test]
async fn a_docx_upload_is_extracted_and_readable_again() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let document = br#"<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Hello docx</w:t></w:r></w:p></w:body>
</w:document>"#;
    let docx = stored_zip(&[("word/document.xml", document)]);
    let (content_type, body) = multipart(
        "notes.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        &docx,
    );
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "docx");
    assert_eq!(json["file"]["text"], "Hello docx");
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(json!({"fileId": file_id}).to_string()))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(reader_body["chunks"][0]["text"], "Hello docx");
    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), docx.as_slice());
}

#[tokio::test]
async fn a_pptx_upload_is_extracted_and_readable_again() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let slide = br#"<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p><a:r><a:t>Hello pptx</a:t></a:r></a:p></p:txBody></p:sp>
  </p:spTree></p:cSld>
</p:sld>"#;
    let pptx = stored_zip(&[("ppt/slides/slide1.xml", slide)]);
    let (content_type, body) = multipart(
        "deck.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        &pptx,
    );
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "pptx");
    assert_eq!(json["file"]["text"], "[PPTX 第 1 页]\nHello pptx");
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(json!({"fileId": file_id}).to_string()))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        reader_body["chunks"][0]["text"],
        "[PPTX 第 1 页]\nHello pptx"
    );
    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), pptx.as_slice());
}

fn simple_pdf(pages: &[Vec<&str>]) -> Vec<u8> {
    let mut bodies: Vec<Vec<u8>> = Vec::new();
    let font = bodies.len() + 1;
    bodies.push(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_vec());
    let pages_id = bodies.len() + 1;
    bodies.push(Vec::new());
    let catalog = bodies.len() + 1;
    bodies.push(format!("<< /Type /Catalog /Pages {pages_id} 0 R >>").into_bytes());
    let mut page_ids = Vec::new();
    for lines in pages {
        let mut commands = vec![
            "BT".to_string(),
            "/F1 12 Tf".to_string(),
            "72 720 Td".to_string(),
        ];
        for (index, line) in lines.iter().enumerate() {
            if index > 0 {
                commands.push("0 -16 Td".to_string());
            }
            let escaped = line
                .replace('\\', "\\\\")
                .replace('(', "\\(")
                .replace(')', "\\)");
            commands.push(format!("({escaped}) Tj"));
        }
        commands.push("ET".to_string());
        let stream = format!("{}\n", commands.join("\n")).into_bytes();
        let mut content = format!("<< /Length {} >>\nstream\n", stream.len()).into_bytes();
        content.extend_from_slice(&stream);
        content.extend_from_slice(b"endstream");
        let content_id = bodies.len() + 1;
        bodies.push(content);
        let page_id = bodies.len() + 1;
        bodies.push(
            format!(
                "<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
            )
            .into_bytes(),
        );
        page_ids.push(page_id);
    }
    let kids = page_ids
        .iter()
        .map(|page| format!("{page} 0 R"))
        .collect::<Vec<_>>()
        .join(" ");
    bodies[pages_id - 1] = format!(
        "<< /Type /Pages /Kids [{kids}] /Count {} >>",
        page_ids.len()
    )
    .into_bytes();
    let mut out = b"%PDF-1.4\n".to_vec();
    let mut offsets = vec![0u32];
    for (index, body) in bodies.iter().enumerate() {
        offsets.push(out.len() as u32);
        let number = index + 1;
        out.extend_from_slice(format!("{number} 0 obj\n").as_bytes());
        out.extend_from_slice(body);
        if !body.ends_with(b"\n") {
            out.push(b'\n');
        }
        out.extend_from_slice(b"endobj\n");
    }
    let xref = out.len() as u32;
    let size = bodies.len() + 1;
    out.extend_from_slice(format!("xref\n0 {size}\n").as_bytes());
    out.extend_from_slice(b"0000000000 65535 f \n");
    for offset in offsets.iter().skip(1) {
        out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!("trailer\n<< /Size {size} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n")
            .as_bytes(),
    );
    out
}

#[tokio::test]
async fn an_xlsx_upload_is_extracted_and_readable_again() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let content_types = br#"<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"#;
    let workbook = br#"<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>
</workbook>"#;
    let rels = br#"<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"#;
    let sheet = br#"<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Hello xlsx</t></is></c>
      <c r="B1" t="n"><v>2</v></c>
    </row>
  </sheetData>
</worksheet>"#;
    let xlsx = stored_zip(&[
        ("[Content_Types].xml", content_types),
        ("xl/workbook.xml", workbook),
        ("xl/_rels/workbook.xml.rels", rels),
        ("xl/worksheets/sheet1.xml", sheet),
    ]);
    let (content_type, body) = multipart(
        "book.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        &xlsx,
    );
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "xlsx");
    assert_eq!(
        json["file"]["text"],
        "Sheet: Data\n行 1\tA1=Hello xlsx\tB1=2"
    );
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(json!({"fileId": file_id}).to_string()))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        reader_body["chunks"][0]["text"],
        "Sheet: Data\n行 1\tA1=Hello xlsx\tB1=2"
    );
    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), xlsx.as_slice());
}

#[tokio::test]
async fn a_pdf_upload_is_extracted_and_readable_again() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let pdf = simple_pdf(&[vec!["Hello pdf", "World"], vec!["Second"]]);
    let (content_type, body) = multipart("notes.pdf", "application/pdf", &pdf);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "pdf");
    assert_eq!(json["file"]["pageCount"], 2);
    assert_eq!(
        json["file"]["text"],
        "[PDF page 1]\nHello pdf\nWorld\n\n[PDF page 2]\nSecond"
    );
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(json!({"fileId": file_id}).to_string()))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        reader_body["chunks"][0]["text"],
        "[PDF page 1]\nHello pdf\nWorld\n\n[PDF page 2]\nSecond"
    );
    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), pdf.as_slice());
}

#[tokio::test]
async fn an_epub_upload_is_extracted_and_readable_again() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let chapter_a = b"<p>Hello epub</p><script>secret()</script>";
    let chapter_b = b"<h1>Later</h1>";
    let nav = b"<p>SECRET NAV</p>";
    let epub = stored_zip(&[
        ("b.xhtml", chapter_b),
        ("nav.xhtml", nav),
        ("a.xhtml", chapter_a),
    ]);
    let (content_type, body) = multipart("book.epub", "application/epub+zip", &epub);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "epub");
    let text = json["file"]["text"].as_str().unwrap_or("");
    assert_eq!(
        text,
        "[EPUB: a.xhtml]\nHello epub\n\n[EPUB: b.xhtml]\nLater"
    );
    assert!(!text.contains("SECRET"), "{text}");
    assert!(!text.contains("secret"), "{text}");
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(json!({"fileId": file_id}).to_string()))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        reader_body["chunks"][0]["text"],
        "[EPUB: a.xhtml]\nHello epub\n\n[EPUB: b.xhtml]\nLater"
    );
    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), epub.as_slice());
}

#[tokio::test]
async fn an_ocr_image_is_extracted_and_readable_again() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let png = include_bytes!("fixtures/hello-ocr.png");
    let (content_type, body) = multipart_with_ocr("hello.png", "image/png", png, true);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "image");
    assert_eq!(json["file"]["pageCount"], 1);
    assert_eq!(json["file"]["text"], "HELLO");
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(json!({"fileId": file_id}).to_string()))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(reader_body["chunks"][0]["text"], "HELLO");
    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), png.as_slice());
}

#[tokio::test]
async fn an_ocr_pdf_is_extracted_and_a_blank_pdf_writes_nothing() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let pdf = include_bytes!("fixtures/hello-ocr.pdf");
    let (content_type, body) = multipart_with_ocr("scan.pdf", "application/pdf", pdf, true);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "pdf");
    assert_eq!(json["file"]["text"], "[PDF 第 1 页 (OCR)]\nHELLO");
    let file_id = json["file"]["fileId"].as_str().expect("file id");
    let source = Request::builder()
        .uri(format!("/api/file-source?fileId={file_id}"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(source).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), pdf.as_slice());

    let blank = simple_pdf(&[vec![]]);
    let before = fixture.cache_files();
    let (content_type, body) = multipart_with_ocr("blank.pdf", "application/pdf", &blank, true);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(
        (status, json["code"].as_str(), json["error"].as_str()),
        (
            StatusCode::UNPROCESSABLE_ENTITY,
            Some("ocr_empty"),
            Some("OCR did not recognize any text."),
        ),
        "{json}"
    );
    assert_eq!(fixture.cache_files(), before);
}

#[tokio::test]
async fn html_script_is_not_extracted() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let html = b"<p>Hello</p><script>secret()</script>";
    let (content_type, body) = multipart("page.html", "text/html", html);
    let (status, json, _) = fixture.post(&content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["file"]["kind"], "html");
    let text = json["file"]["text"].as_str().unwrap_or("");
    assert!(text.contains("Hello"), "{text}");
    assert!(!text.contains("secret"), "{text}");
}
