//! Private streamed media images. An image never grants mutation authority.
use crate::{media_library::reject_links, media_stream_io::DigestWriter, media_values::invalid};
use sha2::{Digest, Sha256};
use std::{
    fmt, fs,
    io::{self, BufReader, BufWriter, Cursor, Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
};
use tonic::Status;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Identity {
    pub bytes: u64,
    pub sha256: Vec<u8>,
}
impl Identity {
    pub(super) fn of(bytes: &[u8]) -> Self {
        Self {
            bytes: bytes.len() as u64,
            sha256: Sha256::digest(bytes).to_vec(),
        }
    }
    pub(super) fn validate(&self) -> Result<(), Status> {
        if self.sha256.len() != 32 {
            return Err(invalid());
        }
        Ok(())
    }
}
pub(super) trait LibrarySource {
    fn identity(&self) -> Identity;
    fn reader(&self) -> Result<Box<dyn Read + Send + '_>, Status>;
    fn legacy_bytes(&self) -> Option<&[u8]> {
        None
    }
}
impl LibrarySource for [u8] {
    fn identity(&self) -> Identity {
        Identity::of(self)
    }
    fn reader(&self) -> Result<Box<dyn Read + Send + '_>, Status> {
        Ok(Box::new(Cursor::new(self)))
    }
    fn legacy_bytes(&self) -> Option<&[u8]> {
        Some(self)
    }
}
impl<const N: usize> LibrarySource for [u8; N] {
    fn identity(&self) -> Identity {
        Identity::of(self)
    }
    fn reader(&self) -> Result<Box<dyn Read + Send + '_>, Status> {
        self.as_slice().reader()
    }
    fn legacy_bytes(&self) -> Option<&[u8]> {
        Some(self)
    }
}
impl LibrarySource for Vec<u8> {
    fn identity(&self) -> Identity {
        Identity::of(self)
    }
    fn reader(&self) -> Result<Box<dyn Read + Send + '_>, Status> {
        self.as_slice().reader()
    }
    fn legacy_bytes(&self) -> Option<&[u8]> {
        Some(self)
    }
}

#[derive(Clone)]
pub(super) enum Image {
    Legacy(Vec<u8>),
    Temporary {
        file: Arc<tempfile::NamedTempFile>,
        identity: Identity,
    },
    Retained {
        path: PathBuf,
        identity: Identity,
    },
}
impl fmt::Debug for Image {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("MediaImage")
            .field("identity", &self.identity())
            .finish()
    }
}
impl LibrarySource for Image {
    fn identity(&self) -> Identity {
        match self {
            Self::Legacy(v) => Identity::of(v),
            Self::Temporary { identity, .. } | Self::Retained { identity, .. } => identity.clone(),
        }
    }
    fn reader(&self) -> Result<Box<dyn Read + Send + '_>, Status> {
        match self {
            Self::Legacy(v) => v.reader(),
            Self::Temporary { file, identity } => open_verified(file.path(), identity),
            Self::Retained { path, identity } => open_verified(path, identity),
        }
    }
    fn legacy_bytes(&self) -> Option<&[u8]> {
        if let Self::Legacy(v) = self {
            Some(v)
        } else {
            None
        }
    }
}

struct VerifiedReader {
    file: BufReader<fs::File>,
    identity: Identity,
    hash: Sha256,
    bytes: u64,
    finished: bool,
}
impl Read for VerifiedReader {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let count = self.file.read(bytes)?;
        self.bytes = self
            .bytes
            .checked_add(count as u64)
            .ok_or_else(|| io::Error::other("media image length overflow"))?;
        self.hash.update(&bytes[..count]);
        if self.bytes > self.identity.bytes {
            return Err(io::Error::other("media image length changed"));
        }
        if count == 0 && !self.finished {
            if self.bytes != self.identity.bytes
                || self.hash.clone().finalize()[..] != self.identity.sha256
            {
                return Err(io::Error::other("media image digest changed"));
            }
            self.finished = true;
        }
        Ok(count)
    }
}
fn open_verified(path: &Path, identity: &Identity) -> Result<Box<dyn Read + Send>, Status> {
    identity.validate()?;
    reject_links(path)?;
    let file = fs::File::open(path).map_err(|_| invalid())?;
    let metadata = file.metadata().map_err(|_| invalid())?;
    if !metadata.is_file() || metadata.len() != identity.bytes {
        return Err(invalid());
    }
    Ok(Box::new(VerifiedReader {
        file: BufReader::with_capacity(64 * 1024, file),
        identity: identity.clone(),
        hash: Sha256::new(),
        bytes: 0,
        finished: false,
    }))
}

impl Image {
    pub(super) fn build(
        write: impl FnOnce(&mut dyn Write) -> Result<(), Status>,
    ) -> Result<Self, Status> {
        let _phase = crate::media_phase_timing::Phase::start("image.build");
        let mut file = tempfile::NamedTempFile::new().map_err(|_| invalid())?;
        let mut output = BufWriter::with_capacity(64 * 1024, DigestWriter::new(file.as_file_mut()));
        write(&mut output)?;
        output.flush().map_err(|_| invalid())?;
        let (bytes, sha256) = output.into_inner().map_err(|_| invalid())?.finish();
        Ok(Self::Temporary {
            file: Arc::new(file),
            identity: Identity { bytes, sha256 },
        })
    }
    pub(super) fn capture(path: &Path) -> Result<Self, Status> {
        let _phase = crate::media_phase_timing::Phase::start("image.capture");
        reject_links(path)?;
        let mut input = match fs::File::open(path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Self::Legacy(Vec::new())),
            Err(_) => return Err(invalid()),
        };
        if !input.metadata().map_err(|_| invalid())?.is_file() {
            return Err(invalid());
        }
        let image = Self::build(|output| {
            io::copy(&mut input, output).map_err(|_| invalid())?;
            Ok(())
        })?;
        reject_links(path)?;
        Ok(image)
    }
    pub(super) fn verify(&self) -> Result<(), Status> {
        let _phase = crate::media_phase_timing::Phase::start("image.verify");
        io::copy(&mut self.reader()?, &mut io::sink()).map_err(|_| invalid())?;
        Ok(())
    }
    pub(super) fn retain(&self, path: &Path) -> Result<Self, Status> {
        let _phase = crate::media_phase_timing::Phase::start("image.retain");
        let identity = self.identity();
        identity.validate()?;
        reject_links(path)?;
        let retained = Self::Retained {
            path: path.into(),
            identity: identity.clone(),
        };
        if path.exists() {
            retained.verify()?;
            return Ok(retained);
        }
        ensure_parent(path)?;
        let parent = path.parent().ok_or_else(invalid)?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(|_| invalid())?;
        io::copy(&mut self.reader()?, temporary.as_file_mut()).map_err(|_| invalid())?;
        temporary.as_file().sync_all().map_err(|_| invalid())?;
        reject_links(path)?;
        match temporary.persist_noclobber(path) {
            Ok(file) => file.sync_all().map_err(|_| invalid())?,
            Err(e) if e.error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(_) => return Err(invalid()),
        }
        sync_directory(parent)?;
        retained.verify()?;
        Ok(retained)
    }
    #[cfg(test)]
    pub(super) fn bytes_for_test(&self) -> Vec<u8> {
        let mut bytes = Vec::new();
        self.reader().unwrap().read_to_end(&mut bytes).unwrap();
        bytes
    }
}
pub(super) fn same(
    left: &(impl LibrarySource + ?Sized),
    right: &(impl LibrarySource + ?Sized),
) -> Result<bool, Status> {
    let _phase = crate::media_phase_timing::Phase::start("image.compare");
    if left.identity() != right.identity() {
        return Ok(false);
    }
    io::copy(&mut left.reader()?, &mut io::sink()).map_err(|_| invalid())?;
    io::copy(&mut right.reader()?, &mut io::sink()).map_err(|_| invalid())?;
    Ok(true)
}
impl PartialEq<Vec<u8>> for Image {
    fn eq(&self, other: &Vec<u8>) -> bool {
        self.identity() == other.identity()
    }
}
impl PartialEq<Image> for Vec<u8> {
    fn eq(&self, other: &Image) -> bool {
        self.identity() == other.identity()
    }
}
impl PartialEq for Image {
    fn eq(&self, other: &Self) -> bool {
        self.identity() == other.identity()
    }
}
impl Eq for Image {}

#[cfg(test)]
#[path = "media_image_tests.rs"]
mod tests;

pub(super) fn sync_directory(path: &Path) -> Result<(), Status> {
    #[cfg(unix)]
    fs::File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|_| invalid())?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}
pub(super) fn ensure_parent(path: &Path) -> Result<(), Status> {
    reject_links(path)?;
    let mut missing = Vec::new();
    let mut current = path.parent().ok_or_else(invalid)?;
    loop {
        match fs::metadata(current) {
            Ok(m) if m.is_dir() => break,
            Ok(_) => return Err(invalid()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                missing.push(current.to_path_buf());
                current = current.parent().ok_or_else(invalid)?;
            }
            Err(_) => return Err(invalid()),
        }
    }
    sync_directory(current)?;
    if let Some(parent) = current.parent() {
        sync_directory(parent)?;
    }
    for directory in missing.iter().rev() {
        match fs::create_dir(directory) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
            Err(_) => return Err(invalid()),
        }
        reject_links(directory)?;
        sync_directory(directory)?;
        sync_directory(directory.parent().ok_or_else(invalid)?)?;
    }
    reject_links(path)?;
    Ok(())
}
