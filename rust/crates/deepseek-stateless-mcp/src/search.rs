use std::io;
use std::path::Path;
use std::process::Stdio;

use serde_json::{Value, json};
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;

pub async fn search_code(
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
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command
        .spawn()
        .map_err(|error| format!("rg failed to start: {error}"))?;
    let stdout = child.stdout.take().expect("stdout was configured as piped");
    let stderr = child.stderr.take().expect("stderr was configured as piped");
    let result = tokio::try_join!(
        read_capped(stdout, max_output_bytes),
        read_capped(stderr, max_output_bytes),
        child.wait(),
    );
    let (stdout, stderr, status) = match result {
        Ok(result) => result,
        Err(error) => {
            // Reap a child whose pipe failed instead of leaving it blocked on output.
            let _ = child.kill().await;
            return Err(format!("rg failed to start: {error}"));
        }
    };
    let code = status.code();
    if code != Some(0) && code != Some(1) {
        let message = String::from_utf8_lossy(&stderr.bytes);
        let suffix = if stderr.truncated {
            format!(" [stderr truncated at {max_output_bytes} bytes]")
        } else {
            String::new()
        };
        return Err(format!(
            "rg failed with exit code {}: {}{}",
            code.unwrap_or(-1),
            message.trim(),
            suffix,
        ));
    }
    let mut truncated = stdout.truncated;
    let text = String::from_utf8_lossy(&stdout.bytes);
    let matches: Vec<&str> = text
        .split(['\n', '\r'])
        .filter(|line| !line.is_empty())
        .take(max_results)
        .collect();
    truncated = truncated || matches.len() >= max_results;
    Ok(json!({"matches": matches, "truncated": truncated}))
}

struct CappedOutput {
    bytes: Vec<u8>,
    truncated: bool,
}

async fn read_capped(mut reader: impl AsyncRead + Unpin, limit: usize) -> io::Result<CappedOutput> {
    let mut bytes = Vec::with_capacity(limit.min(8192));
    let mut buffer = [0_u8; 8192];
    let mut truncated = false;
    loop {
        let count = match reader.read(&mut buffer).await {
            Ok(0) => break,
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        let keep = count.min(limit - bytes.len());
        bytes.extend_from_slice(&buffer[..keep]);
        truncated |= keep < count;
        // Continue draining both pipes after the cap so rg can finish normally.
    }
    Ok(CappedOutput { bytes, truncated })
}

#[cfg(test)]
mod tests {
    use super::read_capped;
    use std::io;
    use std::pin::Pin;
    use std::task::{Context, Poll};
    use tokio::io::{AsyncRead, AsyncReadExt, ReadBuf};

    #[tokio::test]
    async fn capped_reader_drains_large_output_without_retaining_it() {
        let mut reader = tokio::io::repeat(b'x').take(8 * 1024 * 1024);
        let output = read_capped(&mut reader, 17).await.unwrap();
        assert_eq!(reader.limit(), 0, "the pipe must be drained to EOF");
        assert_eq!(output.bytes, vec![b'x'; 17]);
        assert!(output.truncated);
    }

    #[tokio::test]
    async fn capped_reader_marks_only_bytes_beyond_the_exact_limit() {
        for (input, expected, truncated) in [
            (&b""[..], &b""[..], false),
            (&b"ab"[..], &b"ab"[..], false),
            (&b"abc"[..], &b"abc"[..], false),
            (&b"abcd"[..], &b"abc"[..], true),
        ] {
            let output = read_capped(input, 3).await.unwrap();
            assert_eq!(output.bytes, expected);
            assert_eq!(output.truncated, truncated);
        }
    }

    #[tokio::test]
    async fn capped_reader_drains_with_a_zero_byte_limit() {
        let mut reader = &b"discard everything"[..];
        let output = read_capped(&mut reader, 0).await.unwrap();
        assert!(reader.is_empty());
        assert!(output.bytes.is_empty());
        assert!(output.truncated);
    }

    #[tokio::test]
    async fn capped_reader_keeps_raw_utf8_prefix_for_existing_lossy_decoding() {
        let output = read_capped("a中b".as_bytes(), 3).await.unwrap();
        assert_eq!(output.bytes, [b'a', 0xe4, 0xb8]);
        assert_eq!(String::from_utf8_lossy(&output.bytes), "a\u{fffd}");
        assert!(output.truncated);
    }

    struct InterruptedThenError(bool);

    impl AsyncRead for InterruptedThenError {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
            _: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            if !self.0 {
                self.0 = true;
                Poll::Ready(Err(io::ErrorKind::Interrupted.into()))
            } else {
                Poll::Ready(Err(io::ErrorKind::BrokenPipe.into()))
            }
        }
    }

    #[tokio::test]
    async fn capped_reader_retries_interruption_and_propagates_pipe_failure() {
        let error = read_capped(InterruptedThenError(false), 3)
            .await
            .err()
            .expect("broken pipes must fail the search");
        assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
    }
}
