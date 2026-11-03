use super::*;
use sha2::{Digest, Sha256};
use std::io::Cursor;

#[test]
fn media_digest_streams_match_frozen_sha256_and_all_block_boundaries() {
    let abc = [
        0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea, 0x41, 0x41, 0x40, 0xde, 0x5d, 0xae, 0x22,
        0x23, 0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17, 0x7a, 0x9c, 0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00,
        0x15, 0xad,
    ];
    assert_eq!(crate::media_library::sha(b"abc"), abc);
    for length in [
        0, 1, 3, 55, 56, 63, 64, 65, 119, 120, 127, 128, 129, 65535, 65536, 65537,
    ] {
        let bytes: Vec<u8> = (0..length).map(|n| (n % 251) as u8).collect();
        let expected = Sha256::digest(&bytes).to_vec();
        assert_eq!(crate::media_library::sha(&bytes), expected);
        assert_eq!(crate::media_image::Identity::of(&bytes).sha256, expected);
        for chunk in [1, 7, 63, 64, 65, 8192, 65536] {
            let mut output = Vec::new();
            let mut writer = DigestWriter::new(&mut output);
            for part in bytes.chunks(chunk) {
                writer.write_all(part).unwrap();
            }
            writer.flush().unwrap();
            assert_eq!(writer.finish(), (length as u64, expected.clone()));
            assert_eq!(output, bytes);
            let mut reader = DigestReader::new(Cursor::new(&bytes));
            let mut buffer = vec![0; chunk];
            let mut copied = Vec::new();
            loop {
                let count = reader.read(&mut buffer).unwrap();
                if count == 0 {
                    break;
                }
                copied.extend_from_slice(&buffer[..count]);
            }
            assert_eq!(copied, bytes);
            assert_eq!(reader.finish(), (length as u64, expected.clone()));
        }
    }
}

#[test]
fn media_digest_writer_hashes_only_accepted_short_writes_and_preserves_errors() {
    struct ShortWriter(Vec<u8>);
    impl Write for ShortWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.0.len() >= 7 {
                return Err(io::Error::other("injected spool write failure"));
            }
            let count = bytes.len().min(3).min(7 - self.0.len());
            self.0.extend_from_slice(&bytes[..count]);
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("injected spool flush failure"))
        }
    }
    let mut output = ShortWriter(Vec::new());
    let mut writer = DigestWriter::new(&mut output);
    let source = b"original retained bytes";
    assert!(writer.write_all(source).is_err());
    assert!(writer.flush().is_err());
    assert_eq!(writer.finish(), (7, Sha256::digest(&source[..7]).to_vec()));
    assert_eq!(output.0, source[..7]);
}

#[test]
fn media_digest_reader_hashes_only_observed_bytes_and_preserves_source_failure() {
    struct ShortReader {
        bytes: Cursor<Vec<u8>>,
        fail_after: u64,
    }
    impl Read for ShortReader {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            if output.is_empty() {
                return Ok(0);
            }
            if self.bytes.position() == self.fail_after {
                return Err(io::Error::other("injected image read failure"));
            }
            let count = output
                .len()
                .min(3)
                .min((self.fail_after - self.bytes.position()) as usize);
            self.bytes.read(&mut output[..count])
        }
    }
    let source = b"original retained bytes";
    let mut reader = DigestReader::new(ShortReader {
        bytes: Cursor::new(source.to_vec()),
        fail_after: 7,
    });
    let mut copied = Vec::new();
    assert_eq!(reader.read(&mut []).unwrap(), 0);
    assert!(reader.read_to_end(&mut copied).is_err());
    assert_eq!(copied, source[..7]);
    assert_eq!(reader.finish(), (7, Sha256::digest(&source[..7]).to_vec()));
}
