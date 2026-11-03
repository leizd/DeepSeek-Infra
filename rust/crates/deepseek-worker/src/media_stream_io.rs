//! Shared streaming digests, checked here against worker image identities.
pub(super) use deepseek_policy::media_stream_io::{DigestReader, DigestWriter, WriteSeek};

#[cfg(test)]
use sha2::{Digest, Sha256};
#[cfg(test)]
use std::io::{self, Read, Write};
#[cfg(test)]
#[path = "media_stream_io_tests.rs"]
mod tests;
