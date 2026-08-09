use serde::Serialize;
use sha2::{Digest, Sha256};
use std::env;
use std::fmt::Write as _;
use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::PathBuf;

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

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
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
}
