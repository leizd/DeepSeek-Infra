//! Streaming digests used by private media images and temporary record spools.
use sha2::{Digest, Sha256};
use std::io::{self, Read, Seek, Write};

pub(super) trait WriteSeek: Write + Seek + Send {}
impl<T: Write + Seek + Send> WriteSeek for T {}

pub(super) struct DigestWriter<W> {
    inner: W,
    hash: Sha256,
    bytes: u64,
}
impl<W: Write> DigestWriter<W> {
    pub(super) fn new(inner: W) -> Self {
        Self {
            inner,
            hash: Sha256::new(),
            bytes: 0,
        }
    }
    pub(super) fn finish(self) -> (u64, Vec<u8>) {
        (self.bytes, self.hash.finalize().to_vec())
    }
}
impl<W: Write> Write for DigestWriter<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let count = self.inner.write(bytes)?;
        self.bytes = self
            .bytes
            .checked_add(count as u64)
            .ok_or_else(|| io::Error::other("media stream length overflow"))?;
        self.hash.update(&bytes[..count]);
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

pub(super) struct DigestReader<R> {
    inner: R,
    hash: Sha256,
    bytes: u64,
}
impl<R: Read> DigestReader<R> {
    pub(super) fn new(inner: R) -> Self {
        Self {
            inner,
            hash: Sha256::new(),
            bytes: 0,
        }
    }
    pub(super) fn finish(self) -> (u64, Vec<u8>) {
        (self.bytes, self.hash.finalize().to_vec())
    }
}
impl<R: Read> Read for DigestReader<R> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        let count = self.inner.read(bytes)?;
        self.bytes = self
            .bytes
            .checked_add(count as u64)
            .ok_or_else(|| io::Error::other("media stream length overflow"))?;
        self.hash.update(&bytes[..count]);
        Ok(count)
    }
}

#[cfg(test)]
#[path = "media_stream_io_tests.rs"]
mod tests;
