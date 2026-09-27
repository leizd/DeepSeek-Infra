//! Layout and PNG probe. Compared with `tasks/native-runtime/file_page_render_probe.py`,
//! which calls the unmodified `render_pdf_page_layout` and `render_pdf_page_png`.

use std::path::Path;

use deepseek_policy::pdf_page::{render_pdf_page_layout, render_pdf_page_png};
use serde_json::json;

fn main() {
    let dir = std::env::var("FILE_PAGE_RENDER_DIR").expect("fixture dir");
    let mut out = serde_json::Map::new();
    for name in ["simple.pdf", "drawn.pdf", "two.pdf"] {
        let data = std::fs::read(Path::new(&dir).join(name)).expect(name);
        let layout = match render_pdf_page_layout(&data, 1) {
            Ok(value) => json!({"ok": value}),
            Err(error) => {
                json!({"error": {"message": error.message, "code": error.code, "status": error.status}})
            }
        };
        let second = match render_pdf_page_layout(&data, 2) {
            Ok(value) => json!({"ok": value}),
            Err(error) => {
                json!({"error": {"message": error.message, "code": error.code, "status": error.status}})
            }
        };
        let png = match render_pdf_page_png(&data, 1, 1.6) {
            Ok(image) => json!({
                "page": image.page,
                "pageCount": image.page_count,
                "png": !image.bytes.is_empty() && image.bytes.starts_with(b"\x89PNG"),
                "bytes": image.bytes.len(),
                "width": png_dimension(&image.bytes, 0),
                "height": png_dimension(&image.bytes, 1),
            }),
            Err(error) => {
                json!({"error": {"message": error.message, "code": error.code, "status": error.status}})
            }
        };
        out.insert(
            name.to_string(),
            json!({"layout": layout, "second": second, "png": png}),
        );
    }
    println!("{}", serde_json::Value::Object(out));
}

fn png_dimension(bytes: &[u8], index: usize) -> i64 {
    if bytes.len() < 24 || &bytes[12..16] != b"IHDR" {
        return 0;
    }
    let start = 16 + index * 4;
    i64::from(u32::from_be_bytes(
        bytes[start..start + 4].try_into().unwrap_or([0; 4]),
    ))
}
