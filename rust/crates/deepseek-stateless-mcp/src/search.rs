use std::path::Path;
use std::process::{Command, Stdio};

use serde_json::{Value, json};

pub fn search_code(
    workspace_root: &Path,
    search_root: &Path,
    query: &str,
    glob: Option<&str>,
    max_results: usize,
    max_output_bytes: usize,
) -> Result<Value, String> {
    let mut command = Command::new("rg");
    command
        .arg("--line-number")
        .arg("--column")
        .arg("--no-heading")
        .arg("--color")
        .arg("never")
        .arg("--fixed-strings")
        .arg("--max-count")
        .arg(max_results.to_string());
    if let Some(glob) = glob {
        command.arg("--glob").arg(glob);
    }
    command.arg("--").arg(query).arg(search_root);
    command
        .current_dir(workspace_root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = command
        .output()
        .map_err(|error| format!("rg failed to start: {error}"))?;
    let code = output.status.code();
    if code != Some(0) && code != Some(1) {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "rg failed with exit code {}: {}",
            code.unwrap_or(-1),
            stderr.trim()
        ));
    }
    let mut kept = output.stdout;
    let mut truncated = false;
    if kept.len() > max_output_bytes {
        kept.truncate(max_output_bytes);
        truncated = true;
    }
    let text = String::from_utf8_lossy(&kept);
    let matches: Vec<&str> = text
        .split(['\n', '\r'])
        .filter(|line| !line.is_empty())
        .take(max_results)
        .collect();
    truncated = truncated || matches.len() >= max_results;
    Ok(json!({"matches": matches, "truncated": truncated}))
}
