use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::env;
use std::fmt::Write as _;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Write as IoWrite};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, mpsc};
use std::thread;

const MIN_CHUNK: usize = 512 * 1024;
const AVG_CHUNK: usize = 2 * 1024 * 1024;
const MAX_CHUNK: usize = 8 * 1024 * 1024;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Chunk {
    offset: u64,
    length: usize,
    sha256: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Scan {
    size: u64,
    sha256: String,
    protocol: String,
    chunks: Vec<Chunk>,
}

#[derive(Deserialize)]
struct BatchRequest {
    id: serde_json::Value,
    path: PathBuf,
    protocol: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BatchResponse {
    id: serde_json::Value,
    #[serde(flatten)]
    scan: Scan,
}

#[derive(Serialize)]
struct BatchError {
    id: serde_json::Value,
    error: &'static str,
}

fn gears() -> [u64; 256] {
    let mut values = [0_u64; 256];
    let mut seed = 0x9E37_79B9_7F4A_7C15_u64;
    for value in &mut values {
        seed = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = seed;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        *value = z ^ (z >> 31);
    }
    values
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(
        String::with_capacity(bytes.len() * 2),
        |mut output, byte| {
            write!(output, "{byte:02x}").expect("writing to a String cannot fail");
            output
        },
    )
}

fn scan(path: PathBuf, protocol: String) -> io::Result<Scan> {
    let size = path.metadata()?.len();
    let (early_bits, late_bits) = match protocol.as_str() {
        "fastcdc-gear-v2" => (13_u32, 22_u32),
        "fastcdc-gear-v3" if size <= 16 * 1024 * 1024 => (13_u32, 22_u32),
        "fastcdc-gear-v3" => (22_u32, 20_u32),
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unsupported CDC protocol",
            ));
        }
    };
    let mut reader = BufReader::with_capacity(1024 * 1024, File::open(path)?);
    let mut block = vec![0_u8; 1024 * 1024];
    let mut file_hash = Sha256::new();
    let mut chunk = Vec::with_capacity(MAX_CHUNK);
    let mut chunks = Vec::new();
    let mut fingerprint = 0_u64;
    let mut offset = 0_u64;
    let mut chunk_start = 0_u64;
    let table = gears();
    loop {
        let read = reader.read(&mut block)?;
        if read == 0 {
            break;
        }
        file_hash.update(&block[..read]);
        for byte in &block[..read] {
            chunk.push(*byte);
            let length = chunk.len();
            let emit = if length >= MAX_CHUNK {
                true
            } else if length >= MIN_CHUNK {
                fingerprint = fingerprint
                    .wrapping_shl(1)
                    .wrapping_add(table[*byte as usize]);
                let bits = if length >= AVG_CHUNK {
                    late_bits
                } else {
                    early_bits
                };
                fingerprint & ((1_u64 << bits) - 1) == 0
            } else {
                false
            };
            if emit {
                chunks.push(Chunk {
                    offset: chunk_start,
                    length,
                    sha256: hex(&Sha256::digest(&chunk)),
                });
                chunk.clear();
                chunk_start = offset + 1;
            }
            offset += 1;
        }
    }
    if !chunk.is_empty() {
        if protocol == "fastcdc-gear-v3" && chunks.is_empty() && chunk.len() >= AVG_CHUNK {
            let midpoint = chunk.len() / 2;
            chunks.push(Chunk {
                offset: chunk_start,
                length: midpoint,
                sha256: hex(&Sha256::digest(&chunk[..midpoint])),
            });
            chunks.push(Chunk {
                offset: chunk_start + midpoint as u64,
                length: chunk.len() - midpoint,
                sha256: hex(&Sha256::digest(&chunk[midpoint..])),
            });
        } else {
            chunks.push(Chunk {
                offset: chunk_start,
                length: chunk.len(),
                sha256: hex(&Sha256::digest(&chunk)),
            });
        }
    }
    if chunks.iter().map(|item| item.length as u64).sum::<u64>() != size {
        return Err(io::Error::other("CDC ranges do not cover the file"));
    }
    Ok(Scan {
        size,
        sha256: hex(&file_hash.finalize()),
        protocol,
        chunks,
    })
}

fn write_batch_value<W: IoWrite, T: Serialize>(writer: &mut W, value: &T) -> io::Result<()> {
    serde_json::to_writer(&mut *writer, value)?;
    writer.write_all(b"\n")?;
    writer.flush()
}

fn scan_batch_pool_io<R, W, F>(reader: R, writer: W, workers: usize, scanner: F) -> io::Result<()>
where
    R: BufRead,
    W: IoWrite + Send + 'static,
    F: Fn(PathBuf, String) -> io::Result<Scan> + Send + Sync + 'static,
{
    let worker_count = workers.clamp(1, 64);
    let writer = Arc::new(Mutex::new(writer));
    let scanner = Arc::new(scanner);
    let (sender, receiver) = mpsc::sync_channel::<BatchRequest>(worker_count * 2);
    let receiver = Arc::new(Mutex::new(receiver));
    let mut handles = Vec::with_capacity(worker_count);
    for _ in 0..worker_count {
        let receiver = Arc::clone(&receiver);
        let scanner = Arc::clone(&scanner);
        let writer = Arc::clone(&writer);
        handles.push(thread::spawn(move || -> io::Result<()> {
            loop {
                let request = {
                    let guard = receiver
                        .lock()
                        .map_err(|_| io::Error::other("batch receiver lock poisoned"))?;
                    match guard.recv() {
                        Ok(value) => value,
                        Err(_) => break,
                    }
                };
                let result = scanner(request.path, request.protocol);
                let mut output = writer
                    .lock()
                    .map_err(|_| io::Error::other("batch writer lock poisoned"))?;
                match result {
                    Ok(result) => write_batch_value(
                        &mut *output,
                        &BatchResponse {
                            id: request.id,
                            scan: result,
                        },
                    )?,
                    Err(_) => write_batch_value(
                        &mut *output,
                        &BatchError {
                            id: request.id,
                            error: "scan-failed",
                        },
                    )?,
                }
            }
            Ok(())
        }));
    }

    let mut read_error = None;
    for line in reader.lines() {
        let line = match line {
            Ok(value) => value,
            Err(error) => {
                read_error = Some(error);
                break;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        let request: BatchRequest = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(_) => {
                let mut output = writer
                    .lock()
                    .map_err(|_| io::Error::other("batch writer lock poisoned"))?;
                write_batch_value(
                    &mut *output,
                    &BatchError {
                        id: serde_json::Value::Null,
                        error: "invalid-request",
                    },
                )?;
                continue;
            }
        };
        if sender.send(request).is_err() {
            read_error = Some(io::Error::other("native scan workers stopped unexpectedly"));
            break;
        }
    }
    drop(sender);

    let mut worker_error = None;
    for handle in handles {
        match handle.join() {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                worker_error.get_or_insert(error);
            }
            Err(_) => {
                worker_error.get_or_insert_with(|| io::Error::other("native scan worker panicked"));
            }
        };
    }
    if let Some(error) = read_error {
        return Err(error);
    }
    if let Some(error) = worker_error {
        return Err(error);
    }
    Ok(())
}

fn scan_batch(workers: usize) -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    scan_batch_pool_io(stdin.lock(), stdout, workers, scan)
}

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    if args.get(1).is_some_and(|value| value == "scan-batch") {
        let workers = match args.as_slice() {
            [_, _] => 1,
            [_, _, flag, value] if flag == "--workers" => value.parse::<usize>().map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidInput, "--workers must be an integer")
            })?,
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "usage: deepseek-backup scan-batch [--workers <count>]",
                ));
            }
        };
        return scan_batch(workers);
    }
    if args.len() != 5 || args[1] != "scan" || args[2] != "--protocol" {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "usage: deepseek-backup scan --protocol <name> <path>",
        ));
    }
    println!(
        "{}",
        serde_json::to_string(&scan(PathBuf::from(&args[4]), args[3].clone())?)?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::Duration;

    #[derive(Clone, Default)]
    struct SharedOutput(Arc<Mutex<Vec<u8>>>);

    impl IoWrite for SharedOutput {
        fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
            self.0
                .lock()
                .expect("output lock")
                .extend_from_slice(buffer);
            Ok(buffer.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn observe_maximum(maximum: &AtomicUsize, active: usize) {
        let mut observed = maximum.load(Ordering::SeqCst);
        while active > observed {
            match maximum.compare_exchange(observed, active, Ordering::SeqCst, Ordering::SeqCst) {
                Ok(_) => break,
                Err(current) => observed = current,
            }
        }
    }

    #[test]
    fn v2_and_v3_cover_the_file_and_hash_in_one_scan() {
        let path = env::temp_dir().join(format!("deepseek-backup-{}.bin", std::process::id()));
        let data: Vec<u8> = (0..(3 * 1024 * 1024))
            .map(|index| (index % 251) as u8)
            .collect();
        fs::write(&path, &data).expect("write fixture");
        for protocol in ["fastcdc-gear-v2", "fastcdc-gear-v3"] {
            let result = scan(path.clone(), protocol.to_string()).expect("scan");
            assert_eq!(result.size, data.len() as u64);
            assert_eq!(result.sha256, hex(&Sha256::digest(&data)));
            assert_eq!(
                result.chunks.iter().map(|item| item.length).sum::<usize>(),
                data.len()
            );
            let mut offset = 0_u64;
            for chunk in result.chunks {
                assert_eq!(chunk.offset, offset);
                offset += chunk.length as u64;
            }
        }
        fs::remove_file(path).expect("remove fixture");
    }

    #[test]
    fn rejects_unknown_protocol() {
        let path = env::temp_dir().join(format!(
            "deepseek-backup-invalid-{}.bin",
            std::process::id()
        ));
        fs::write(&path, b"data").expect("write fixture");
        assert!(scan(path.clone(), "future".to_string()).is_err());
        fs::remove_file(path).expect("remove fixture");
    }

    #[test]
    fn batch_scanner_keeps_ids_and_reports_item_failures() {
        let path =
            env::temp_dir().join(format!("deepseek-backup-batch-{}.bin", std::process::id()));
        fs::write(&path, b"batch-data").expect("write fixture");
        let requests = format!(
            "{{\"id\":7,\"path\":{},\"protocol\":\"fastcdc-gear-v3\"}}\n{{\"id\":8,\"path\":{},\"protocol\":\"future\"}}\n",
            serde_json::to_string(&path).expect("encode path"),
            serde_json::to_string(&path).expect("encode path")
        );
        let output = SharedOutput::default();
        let output_bytes = Arc::clone(&output.0);
        scan_batch_pool_io(BufReader::new(requests.as_bytes()), output, 2, scan)
            .expect("scan batch");
        let mut lines: Vec<serde_json::Value> =
            String::from_utf8(output_bytes.lock().expect("output lock").clone())
                .expect("utf8")
                .lines()
                .map(|line| serde_json::from_str(line).expect("jsonl"))
                .collect();
        lines.sort_by_key(|value| value["id"].as_i64());
        assert_eq!(lines[0]["id"], 7);
        assert_eq!(lines[0]["size"], 10);
        assert_eq!(lines[1]["id"], 8);
        assert_eq!(lines[1]["error"], "scan-failed");
        fs::remove_file(path).expect("remove fixture");
    }

    #[test]
    fn batch_scanner_runs_through_a_bounded_worker_pool() {
        let mut requests = String::new();
        for id in 0..8 {
            writeln!(
                &mut requests,
                "{{\"id\":{id},\"path\":\"fixture-{id}\",\"protocol\":\"fastcdc-gear-v3\"}}"
            )
            .expect("write request");
        }
        let output = SharedOutput::default();
        let output_bytes = Arc::clone(&output.0);
        let active = Arc::new(AtomicUsize::new(0));
        let maximum = Arc::new(AtomicUsize::new(0));
        let scanner = {
            let active = Arc::clone(&active);
            let maximum = Arc::clone(&maximum);
            move |_path: PathBuf, protocol: String| {
                let now = active.fetch_add(1, Ordering::SeqCst) + 1;
                observe_maximum(&maximum, now);
                thread::sleep(Duration::from_millis(20));
                active.fetch_sub(1, Ordering::SeqCst);
                Ok(Scan {
                    size: 0,
                    sha256: "0".repeat(64),
                    protocol,
                    chunks: Vec::new(),
                })
            }
        };

        scan_batch_pool_io(BufReader::new(requests.as_bytes()), output, 2, scanner)
            .expect("scan batch pool");

        assert_eq!(maximum.load(Ordering::SeqCst), 2);
        assert_eq!(
            String::from_utf8(output_bytes.lock().expect("output lock").clone())
                .expect("utf8")
                .lines()
                .count(),
            8
        );
    }
}
