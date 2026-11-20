//! Shared streaming digests, checked here against worker image identities.
pub(super) use deepseek_policy::media_stream_io::DigestWriter;

#[cfg(test)]
use deepseek_policy::media_stream_io::DigestReader;
#[cfg(test)]
use std::io::{self, Read, Write};
#[cfg(test)]
#[path = "media_stream_io_tests.rs"]
mod tests;
