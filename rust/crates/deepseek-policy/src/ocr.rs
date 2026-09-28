//! Local OCR used when an upload has no selectable text.
//!
//! Engine order matches `extract_image_ocr` / `extract_pdf_ocr`: an explicit
//! `OCR_FORMULA_CMD`, otherwise pix2tex/latexocr when they are on `PATH`, then
//! Tesseract, then Windows OCR. DeepSeek's hosted OCR is skipped when no API
//! key is configured. The highest `_ocr_text_score` wins. A tied score keeps
//! the earlier engine.

use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::app_error::{AppError, codes};

const IMAGE_UNAVAILABLE: &str = "No OCR engine is available. Set DEEPSEEK_API_KEY for DeepSeek OCR, or install requirements-ocr.txt and Tesseract to use the local fallback for image text.";
const PDF_UNAVAILABLE: &str = "No OCR engine is available. Set DEEPSEEK_API_KEY for DeepSeek OCR, or install requirements-ocr.txt and Tesseract to use the local fallback for scanned PDFs.";
const IMAGE_EMPTY: &str = "OCR did not recognize any text in image.";
const PDF_EMPTY: &str = "OCR did not recognize any text.";

pub fn ocr_image(data: &[u8]) -> Result<String, AppError> {
    recognize(data, IMAGE_EMPTY, IMAGE_UNAVAILABLE)
}

pub fn ocr_pdf(data: &[u8]) -> Result<String, AppError> {
    let pages = render_pdf_pages(data)?;
    let mut blocks = Vec::new();
    let mut saw_empty = false;
    for (index, page) in pages.iter().enumerate() {
        match recognize(page, PDF_EMPTY, PDF_UNAVAILABLE) {
            Ok(text) => {
                // OCR engines can hallucinate a glyph on a completely white
                // rendered page. Keep the engine's unavailable result above,
                // but do not persist text that has no source pixels.
                if is_blank_pdf_page_png(page) {
                    saw_empty = true;
                    continue;
                }
                let text = text.trim();
                if text.is_empty() {
                    saw_empty = true;
                    continue;
                }
                blocks.push(format!("[PDF 第 {} 页 (OCR)]\n{text}", index + 1));
            }
            Err(error) if error.code == codes::OCR_EMPTY => saw_empty = true,
            Err(error) => return Err(error),
        }
    }
    if blocks.is_empty() {
        if saw_empty {
            return Err(empty_error(PDF_EMPTY));
        }
        return Err(unavailable(PDF_UNAVAILABLE));
    }
    Ok(blocks.join("\n\n"))
}

fn is_blank_pdf_page_png(page: &[u8]) -> bool {
    let Ok(mut reader) = png::Decoder::new(Cursor::new(page)).read_info() else {
        return false;
    };
    if !matches!(
        reader.output_color_type(),
        (
            png::ColorType::Rgb | png::ColorType::Grayscale,
            png::BitDepth::Eight
        )
    ) {
        return false;
    }
    let mut saw_row = false;
    loop {
        match reader.next_row() {
            Ok(Some(row)) => {
                saw_row = true;
                if row.data().iter().any(|channel| *channel != 255) {
                    return false;
                }
            }
            Ok(None) => return saw_row,
            Err(_) => return false,
        }
    }
}

fn recognize(
    image: &[u8],
    empty_message: &str,
    unavailable_message: &str,
) -> Result<String, AppError> {
    let mut best = String::new();
    let mut best_score = -1i64;
    let mut saw_empty = false;
    let mut any_engine = false;
    if let Some(command) = formula_command() {
        any_engine = true;
        match formula_text(image, &command) {
            Ok(text) => consider(&mut best, &mut best_score, &mut saw_empty, &text),
            Err(error)
                if error.code == codes::OCR_UNAVAILABLE || error.code == codes::OCR_EMPTY =>
            {
                saw_empty = saw_empty || error.code == codes::OCR_EMPTY;
            }
            Err(error) => return Err(error),
        }
    }
    if let Some(executable) = locate_tesseract() {
        any_engine = true;
        match tesseract_text(image, &executable) {
            Ok(text) => consider(&mut best, &mut best_score, &mut saw_empty, &text),
            Err(error) if error.code == codes::OCR_UNAVAILABLE => {}
            Err(error) => return Err(error),
        }
    }
    if cfg!(windows) && powershell_path().is_some() {
        any_engine = true;
        match windows_text(image) {
            Ok(text) => consider(&mut best, &mut best_score, &mut saw_empty, &text),
            Err(error) if error.code == codes::OCR_UNAVAILABLE => {}
            Err(error) => return Err(error),
        }
    }
    if !best.is_empty() {
        return Ok(best);
    }
    if !any_engine {
        return Err(unavailable(unavailable_message));
    }
    if saw_empty {
        return Err(empty_error(empty_message));
    }
    Err(unavailable(unavailable_message))
}

fn consider(best: &mut String, best_score: &mut i64, saw_empty: &mut bool, text: &str) {
    let text = normalize_ocr_text(text);
    if text.trim().is_empty() {
        *saw_empty = true;
        return;
    }
    let score = ocr_text_score(&text);
    if score > *best_score {
        *best = text.trim().to_string();
        *best_score = score;
    }
}

fn formula_command() -> Option<String> {
    let override_command = std::env::var("OCR_FORMULA_CMD").unwrap_or_default();
    let override_command = override_command.trim();
    if !override_command.is_empty() {
        return Some(override_command.to_string());
    }
    for (executable, template) in [
        ("pix2tex", "pix2tex {image}"),
        ("latexocr", "latexocr {image}"),
    ] {
        if which(executable).is_some() {
            return Some(template.to_string());
        }
    }
    None
}

fn formula_text(image: &[u8], command: &str) -> Result<String, AppError> {
    let path = write_temp_image(image)?;
    let args = formula_args(command, &path);
    let result = run_command(&args, 120);
    let _ = std::fs::remove_file(&path);
    match result {
        Ok(output) if output.status.success() => Ok(output.stdout),
        Ok(_) | Err(_) => Err(unavailable("Formula OCR command failed.")),
    }
}

fn formula_args(template: &str, image: &Path) -> Vec<String> {
    let image = image.display().to_string();
    let mut args: Vec<String> = template.split_whitespace().map(str::to_string).collect();
    let mut replaced = false;
    for arg in &mut args {
        if arg.contains("{image}") {
            *arg = arg.replace("{image}", &image);
            replaced = true;
        }
    }
    if !replaced {
        args.push(image);
    }
    args
}

fn tesseract_text(image: &[u8], executable: &Path) -> Result<String, AppError> {
    let path = write_temp_image(image)?;
    let lang = tesseract_lang(executable);
    let mut best = String::new();
    let mut best_score = -1i64;
    let mut ran = false;
    for config in tesseract_configs() {
        let mut args = vec![
            path.display().to_string(),
            "stdout".to_string(),
            "-l".to_string(),
            lang.clone(),
        ];
        args.extend(config.split_whitespace().map(str::to_string));
        if let Ok(output) = run_command_at(executable, &args, 60) {
            if output.status.success() {
                ran = true;
                consider(&mut best, &mut best_score, &mut false, &output.stdout);
            }
        }
    }
    let _ = std::fs::remove_file(&path);
    if !best.is_empty() {
        return Ok(best);
    }
    // A successful process that recognized nothing is an empty result. The caller
    // records that as OCR_EMPTY. A process that never started is unavailable.
    if ran {
        return Ok(String::new());
    }
    Err(unavailable("Image OCR failed."))
}

fn tesseract_configs() -> &'static [&'static str] {
    match ocr_mode() {
        "fast" => &["--psm 6 -c preserve_interword_spaces=1"],
        "quality" => &[
            "--psm 6 -c preserve_interword_spaces=1",
            "--psm 11 -c preserve_interword_spaces=1",
            "--psm 7 -c preserve_interword_spaces=1",
            "--psm 13 -c preserve_interword_spaces=1",
            "--psm 4 -c preserve_interword_spaces=1",
            "--psm 3 -c preserve_interword_spaces=1",
        ],
        _ => &[
            "--psm 6 -c preserve_interword_spaces=1",
            "--psm 11 -c preserve_interword_spaces=1",
            "--psm 7 -c preserve_interword_spaces=1",
        ],
    }
}

fn tesseract_lang(executable: &Path) -> String {
    let output = Command::new(executable).arg("--list-langs").output();
    let mut available = Vec::new();
    if let Ok(output) = output {
        let text = String::from_utf8_lossy(&output.stdout);
        let err = String::from_utf8_lossy(&output.stderr);
        for line in text.lines().chain(err.lines()) {
            let line = line.trim();
            if line.is_empty() || line.contains(' ') || line.contains(':') {
                continue;
            }
            available.push(line.to_string());
        }
    }
    let preferred = ["chi_sim", "chi_tra", "eng", "equ", "jpn", "kor"];
    let picked: Vec<&str> = preferred
        .into_iter()
        .filter(|code| available.iter().any(|item| item == code))
        .collect();
    if picked.is_empty() {
        "eng".to_string()
    } else {
        picked.join("+")
    }
}

fn windows_text(image: &[u8]) -> Result<String, AppError> {
    let Some(powershell) = powershell_path() else {
        return Err(unavailable("PowerShell is required for Windows OCR."));
    };
    let image_path = write_temp_image(image)?;
    let script_path = std::env::temp_dir().join(format!(
        "deepseek-ocr-{}-{}.ps1",
        std::process::id(),
        unique_stamp()
    ));
    std::fs::write(&script_path, WINDOWS_OCR_PS).map_err(|_| unavailable("Windows OCR failed."))?;
    let output = run_command_at(
        &powershell,
        &[
            "-NoProfile".to_string(),
            "-ExecutionPolicy".to_string(),
            "Bypass".to_string(),
            "-File".to_string(),
            script_path.display().to_string(),
            image_path.display().to_string(),
        ],
        90,
    );
    let _ = std::fs::remove_file(&script_path);
    let _ = std::fs::remove_file(&image_path);
    match output {
        Ok(output) if output.status.success() => Ok(output.stdout),
        _ => Err(unavailable("Windows OCR failed.")),
    }
}

fn render_pdf_pages(pdf: &[u8]) -> Result<Vec<Vec<u8>>, AppError> {
    let Some(pdftoppm) = which("pdftoppm") else {
        return Err(unavailable("PDF OCR requires pdf2image and pdftoppm."));
    };
    let directory = std::env::temp_dir().join(format!(
        "deepseek-ocr-pdf-{}-{}",
        std::process::id(),
        unique_stamp()
    ));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).map_err(|_| unavailable("PDF OCR rendering failed."))?;
    let input = directory.join("input.pdf");
    std::fs::write(&input, pdf).map_err(|_| unavailable("PDF OCR rendering failed."))?;
    let prefix = directory.join("page");
    let dpi = pdf_dpi().to_string();
    let rendered = run_command_at(
        &pdftoppm,
        &[
            "-r".to_string(),
            dpi,
            "-png".to_string(),
            input.display().to_string(),
            prefix.display().to_string(),
        ],
        60,
    );
    let mut pages = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&directory) {
        let mut names: Vec<PathBuf> = entries
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("png"))
            .collect();
        names.sort();
        for name in names {
            if let Ok(bytes) = std::fs::read(&name) {
                pages.push(bytes);
            }
        }
    }
    let _ = std::fs::remove_dir_all(&directory);
    if rendered.is_err() || pages.is_empty() {
        return Err(unavailable("PDF OCR rendering failed."));
    }
    Ok(pages)
}

fn write_temp_image(image: &[u8]) -> Result<PathBuf, AppError> {
    let suffix = if image.starts_with(b"\x89PNG") {
        "png"
    } else if image.starts_with(&[0xFF, 0xD8]) {
        "jpg"
    } else if image.starts_with(b"%PDF") {
        "pdf"
    } else {
        "img"
    };
    let path = std::env::temp_dir().join(format!(
        "deepseek-ocr-{}-{}.{}",
        std::process::id(),
        unique_stamp(),
        suffix
    ));
    std::fs::write(&path, image).map_err(|_| unavailable("Image OCR failed."))?;
    Ok(path)
}

struct CommandOutput {
    status: std::process::ExitStatus,
    stdout: String,
}

fn run_command(args: &[String], timeout_seconds: u64) -> Result<CommandOutput, AppError> {
    let Some(program) = args.first() else {
        return Err(unavailable("Formula OCR command failed."));
    };
    run_command_at(Path::new(program), &args[1..], timeout_seconds)
}

fn run_command_at(
    program: &Path,
    args: &[String],
    _timeout_seconds: u64,
) -> Result<CommandOutput, AppError> {
    let output = Command::new(program)
        .args(args)
        .output()
        .map_err(|_| unavailable("Image OCR failed."))?;
    Ok(CommandOutput {
        status: output.status,
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
    })
}

fn locate_tesseract() -> Option<PathBuf> {
    if let Some(override_path) = std::env::var("TESSERACT_CMD")
        .ok()
        .filter(|value| !value.trim().is_empty())
    {
        let path = PathBuf::from(override_path.trim());
        if path.is_file() {
            return Some(path);
        }
    }
    if let Some(found) = which("tesseract") {
        return Some(found);
    }
    for candidate in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"D:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Tesseract-OCR\tesseract.exe",
    ] {
        let path = PathBuf::from(candidate);
        if path.is_file() {
            return Some(path);
        }
    }
    None
}

fn powershell_path() -> Option<PathBuf> {
    which("powershell.exe")
        .or_else(|| which("powershell"))
        .or_else(|| which("pwsh.exe"))
}

fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for entry in std::env::split_paths(&path) {
        let candidate = entry.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
        let with_exe = entry.join(format!("{name}.exe"));
        if with_exe.is_file() {
            return Some(with_exe);
        }
    }
    None
}

fn ocr_mode() -> &'static str {
    match std::env::var("OCR_MODE")
        .ok()
        .map(|value| value.trim().to_ascii_lowercase())
    {
        Some(value) if value == "fast" => "fast",
        Some(value) if value == "quality" => "quality",
        _ => "balanced",
    }
}

fn pdf_dpi() -> i64 {
    let value = std::env::var("OCR_PDF_DPI")
        .ok()
        .and_then(|text| text.trim().parse::<i64>().ok())
        .unwrap_or(300);
    value.clamp(150, 450)
}

fn unique_stamp() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0)
}

fn unavailable(message: &str) -> AppError {
    AppError {
        message: message.to_string(),
        code: codes::OCR_UNAVAILABLE,
        status: 415,
    }
}

fn empty_error(message: &str) -> AppError {
    AppError {
        message: message.to_string(),
        code: codes::OCR_EMPTY,
        status: 422,
    }
}

fn normalize_ocr_text(value: &str) -> String {
    let text = value
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\0', "");
    let mut lines = Vec::new();
    let mut blank = false;
    for raw in text.split('\n') {
        let line = raw.trim();
        if line.is_empty() {
            if !lines.is_empty() && !blank {
                lines.push(String::new());
                blank = true;
            }
            continue;
        }
        if looks_like_ocr_noise(line) {
            continue;
        }
        lines.push(line.to_string());
        blank = false;
    }
    lines.join("\n").trim().to_string()
}

fn looks_like_ocr_noise(line: &str) -> bool {
    let readable = line
        .chars()
        .filter(|ch| ch.is_alphanumeric() || ('\u{4e00}'..='\u{9fff}').contains(ch))
        .count();
    readable == 0 && line.chars().count() <= 3
}

fn ocr_text_score(text: &str) -> i64 {
    let cleaned = normalize_ocr_text(text);
    if cleaned.is_empty() {
        return 0;
    }
    let readable = cleaned
        .chars()
        .filter(|ch| ch.is_alphanumeric() || ('\u{4e00}'..='\u{9fff}').contains(ch))
        .count() as i64;
    readable * 4
}

const WINDOWS_OCR_PS: &str = r#"
param([string]$Path)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:asTaskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
  Where-Object {
    $_.Name -eq "AsTask" -and
    $_.IsGenericMethod -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  } |
  Select-Object -First 1
function Await-Operation($Operation, [Type]$ResultType) {
  $task = $script:asTaskMethod.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  $task.Wait()
  return $task.Result
}
$stream = $null
try {
  $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await-Operation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) { throw "Windows OCR engine is not available for current user languages." }
  $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  [Console]::Write($result.Text)
} finally {
  if ($null -ne $stream) { $stream.Dispose() }
}
"#;

#[cfg(test)]
mod tests {
    use super::is_blank_pdf_page_png;

    fn rendered_png(rgb: &[u8]) -> Vec<u8> {
        let mut bytes = Vec::new();
        {
            let mut encoder = png::Encoder::new(&mut bytes, 2, 1);
            encoder.set_color(png::ColorType::Rgb);
            encoder.set_depth(png::BitDepth::Eight);
            let mut writer = encoder.write_header().expect("PNG header");
            writer.write_image_data(rgb).expect("PNG pixels");
        }
        bytes
    }

    #[test]
    fn blank_pdf_page_check_requires_all_source_pixels_to_be_white() {
        let white = rendered_png(&[255; 6]);
        assert!(is_blank_pdf_page_png(&white));

        let marked = rendered_png(&[255, 255, 255, 255, 255, 254]);
        assert!(!is_blank_pdf_page_png(&marked));
        assert!(!is_blank_pdf_page_png(b"not a PNG"));
    }
}
