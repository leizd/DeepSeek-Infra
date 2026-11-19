//! Filesystem handles used for backup target marker creation and observation.
use sha2::{Digest as _, Sha256};
use std::{
    fs::File,
    io::{self, Read, Write},
    path::Path,
};

pub(super) const MARKER_NAME: &str = ".deepseek-infra-backup-target.json";

fn invalid_directory() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "backup target directory identity is invalid",
    )
}

pub(super) struct MarkerDirectory {
    #[cfg(unix)]
    handle: File,
    #[cfg(windows)]
    path: std::path::PathBuf,
    #[cfg(windows)]
    handles: Vec<File>,
}

impl MarkerDirectory {
    #[cfg(unix)]
    pub(super) fn open(path: &Path) -> io::Result<Self> {
        use rustix::fs::{Mode, OFlags, open, openat};
        use std::path::Component;

        let directory_flags = OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        #[cfg(any(target_os = "linux", target_os = "android"))]
        let search_flags = directory_flags | OFlags::PATH;
        #[cfg(not(any(target_os = "linux", target_os = "android")))]
        let search_flags = directory_flags | OFlags::RDONLY;
        let mut parts = path.components();
        if parts.next() != Some(Component::RootDir) {
            return Err(invalid_directory());
        }
        let mut dir = open("/", search_flags, Mode::empty())?;
        for part in parts {
            let Component::Normal(name) = part else {
                return Err(invalid_directory());
            };
            dir = openat(&dir, name, search_flags, Mode::empty())?;
        }
        // Android app directories can have search-only ancestors. Keep the
        // path walk pinned and open the final directory for marker I/O and sync.
        #[cfg(any(target_os = "linux", target_os = "android"))]
        let dir = openat(&dir, ".", directory_flags | OFlags::RDONLY, Mode::empty())?;
        Ok(Self { handle: dir.into() })
    }

    #[cfg(windows)]
    pub(super) fn open(path: &Path) -> io::Result<Self> {
        use std::fs::OpenOptions;
        use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
            FILE_READ_ATTRIBUTES, FILE_SHARE_READ,
        };

        if !path.is_absolute() {
            return Err(invalid_directory());
        }
        let mut handles = Vec::new();
        for part in path.ancestors().collect::<Vec<_>>().into_iter().rev() {
            // Keep the directory components open until the no-replace move
            // completes. The share mode denies replacement and reparse writes.
            let handle = OpenOptions::new()
                .access_mode(FILE_READ_ATTRIBUTES)
                .share_mode(FILE_SHARE_READ)
                .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
                .open(part)?;
            let metadata = handle.metadata()?;
            if !metadata.is_dir() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err(invalid_directory());
            }
            handles.push(handle);
        }
        Ok(Self {
            path: path.to_path_buf(),
            handles,
        })
    }

    pub(super) fn identity(&self) -> io::Result<[u8; 32]> {
        let mut digest = Sha256::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let info = self.handle.metadata()?;
            digest.update(b"unix-directory-v1\0");
            digest.update(info.dev().to_le_bytes());
            digest.update(info.ino().to_le_bytes());
        }
        #[cfg(windows)]
        {
            use std::mem::{MaybeUninit, size_of};
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::{
                FILE_ID_INFO, FileIdInfo, GetFileInformationByHandleEx,
            };
            let mut info = MaybeUninit::<FILE_ID_INFO>::uninit();
            let directory = self.handles.last().ok_or_else(invalid_directory)?;
            // SAFETY: the handle remains owned by self, and the destination has
            // the alignment and length required for FileIdInfo.
            let result = unsafe {
                GetFileInformationByHandleEx(
                    directory.as_raw_handle(),
                    FileIdInfo,
                    info.as_mut_ptr().cast(),
                    size_of::<FILE_ID_INFO>() as u32,
                )
            };
            if result == 0 {
                return Err(io::Error::last_os_error());
            }
            // SAFETY: Win32 initialized FILE_ID_INFO on the success path.
            let info = unsafe { info.assume_init() };
            digest.update(b"windows-directory-v1\0");
            digest.update(info.VolumeSerialNumber.to_le_bytes());
            digest.update(info.FileId.Identifier);
        }
        Ok(digest.finalize().into())
    }

    #[cfg(unix)]
    fn open_marker(&self) -> io::Result<File> {
        use rustix::fs::{Mode, OFlags, openat};
        Ok(openat(
            &self.handle,
            MARKER_NAME,
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK,
            Mode::empty(),
        )?
        .into())
    }

    #[cfg(windows)]
    fn open_marker(&self) -> io::Result<File> {
        use std::fs::OpenOptions;
        use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
        };
        let file = OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(self.path.join(MARKER_NAME))?;
        if file.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(invalid_directory());
        }
        Ok(file)
    }

    pub(super) fn marker(&self, maximum: usize) -> io::Result<Option<Vec<u8>>> {
        Ok(self.read_marker(maximum)?.map(|(_, bytes)| bytes))
    }

    fn read_marker(&self, maximum: usize) -> io::Result<Option<(File, Vec<u8>)>> {
        let file = match self.open_marker() {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        let before = file.metadata()?;
        if !before.is_file() || before.len() > maximum as u64 {
            return Err(invalid_directory());
        }
        #[cfg(target_os = "android")]
        {
            use std::os::unix::fs::MetadataExt as _;
            if before.mode() & 0o777 == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "backup target marker publication is incomplete",
                ));
            }
        }
        let mut bytes = Vec::new();
        (&file).take(maximum as u64 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > maximum || bytes.len() as u64 != before.len() {
            return Err(invalid_directory());
        }
        Ok(Some((file, bytes)))
    }

    #[cfg(unix)]
    pub(super) fn sync(&self) -> io::Result<()> {
        self.handle.sync_all()
    }

    #[cfg(target_os = "android")]
    pub(super) fn sync_observed_marker_file(&self, expected: &[u8]) -> io::Result<()> {
        let (file, current) = self
            .read_marker(expected.len())?
            .ok_or_else(invalid_directory)?;
        if current.as_slice() != expected {
            return Err(invalid_directory());
        }
        file.sync_all()
    }

    #[cfg(unix)]
    pub(super) fn create_marker(
        &self,
        bytes: &[u8],
        operation_digest: &[u8; 32],
        before_publish: impl Fn() -> io::Result<()>,
    ) -> io::Result<()> {
        use rustix::fs::{AtFlags, Mode, OFlags, fstat, mkdirat, openat, statat, unlinkat};

        let mut staging_name = String::from(".deepseek-marker-");
        use std::fmt::Write as _;
        for byte in operation_digest {
            let _ = write!(staging_name, "{byte:02x}");
        }
        mkdirat(
            &self.handle,
            staging_name.as_str(),
            Mode::RUSR | Mode::WUSR | Mode::XUSR,
        )?;
        let staging = openat(
            &self.handle,
            staging_name.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        let identity = fstat(&staging)?;
        if identity.st_mode & 0o077 != 0 || identity.st_uid != rustix::process::geteuid().as_raw() {
            return Err(invalid_directory());
        }
        let result = (|| {
            let mut file: File = openat(
                &staging,
                "marker",
                OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::RUSR | Mode::WUSR,
            )?
            .into();
            file.write_all(bytes)?;
            file.sync_all()?;
            before_publish()?;
            let publication = self.publish_staged(&staging);
            #[cfg(target_os = "android")]
            let publication = match publication {
                // The ext filesystem family enforces Unix inode permissions.
                // Older Android kernels can lack no-replace rename while the
                // app is prohibited from creating hard links.
                Err(error)
                    if error.kind() == io::ErrorKind::PermissionDenied
                        && rustix::fs::fstatfs(&self.handle)?.f_type == 0xef53 =>
                {
                    self.publish_exclusive_marker(bytes, &before_publish)
                }
                result => result,
            };
            publication?;
            self.sync()
        })();
        let _ = unlinkat(&staging, "marker", AtFlags::empty());
        if statat(
            &self.handle,
            staging_name.as_str(),
            AtFlags::SYMLINK_NOFOLLOW,
        )
        .is_ok_and(|current| current.st_dev == identity.st_dev && current.st_ino == identity.st_ino)
        {
            let _ = unlinkat(&self.handle, staging_name.as_str(), AtFlags::REMOVEDIR);
        }
        result
    }

    #[cfg(any(target_os = "android", all(unix, test)))]
    fn publish_exclusive_marker(
        &self,
        bytes: &[u8],
        before_publish: impl Fn() -> io::Result<()>,
    ) -> io::Result<()> {
        use rustix::fs::{Mode, OFlags, fchmod, openat};
        use std::os::unix::fs::MetadataExt as _;

        before_publish()?;
        let mut file: File = openat(
            &self.handle,
            MARKER_NAME,
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?
        .into();
        // The name belongs to this operation immediately, but ordinary
        // readers cannot access its contents until the complete bytes are
        // durable. An interrupted reservation is never replaced or removed.
        if file.metadata()?.mode() & 0o777 != 0 {
            return Err(invalid_directory());
        }
        file.write_all(bytes)?;
        file.sync_all()?;
        before_publish()?;
        fchmod(&file, Mode::RUSR | Mode::WUSR)?;
        file.sync_all()
    }

    #[cfg(unix)]
    fn publish_staged(&self, staging: &rustix::fd::OwnedFd) -> io::Result<()> {
        #[cfg(any(target_os = "linux", target_os = "android"))]
        {
            use rustix::fs::{RenameFlags, renameat_with};
            use rustix::io::Errno;
            match renameat_with(
                staging,
                "marker",
                &self.handle,
                MARKER_NAME,
                RenameFlags::NOREPLACE,
            ) {
                Ok(()) => return Ok(()),
                Err(Errno::NOSYS | Errno::NOTSUP | Errno::INVAL) => (),
                Err(error) => return Err(error.into()),
            }
        }
        rustix::fs::linkat(
            staging,
            "marker",
            &self.handle,
            MARKER_NAME,
            rustix::fs::AtFlags::empty(),
        )?;
        Ok(())
    }

    #[cfg(windows)]
    pub(super) fn create_marker(
        &self,
        bytes: &[u8],
        _operation_digest: &[u8; 32],
        before_publish: impl FnOnce() -> io::Result<()>,
    ) -> io::Result<()> {
        let mut file = tempfile::NamedTempFile::new_in(&self.path)?;
        file.write_all(bytes)?;
        file.as_file().sync_all()?;
        before_publish()?;
        let published = file.persist_noclobber(self.path.join(MARKER_NAME))?;
        published.sync_all()
    }
}

#[cfg(test)]
#[path = "target_marker_files_tests.rs"]
mod tests;
