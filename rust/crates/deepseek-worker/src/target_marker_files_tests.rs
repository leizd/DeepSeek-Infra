use super::*;

#[test]
fn target_marker_files_publish_complete_bytes_and_do_not_replace_a_marker() {
    let root = tempfile::tempdir().unwrap();
    let directory = MarkerDirectory::open(&root.path().canonicalize().unwrap()).unwrap();
    let identity = directory.identity().unwrap();
    assert_eq!(directory.marker(4096).unwrap(), None);
    directory
        .create_marker(b"first marker", &[3; 32], || Ok(()))
        .unwrap();
    assert_eq!(
        directory.marker(4096).unwrap(),
        Some(b"first marker".to_vec())
    );
    assert_eq!(
        directory
            .create_marker(b"second marker", &[4; 32], || Ok(()))
            .unwrap_err()
            .kind(),
        io::ErrorKind::AlreadyExists
    );
    assert_eq!(
        directory.marker(4096).unwrap(),
        Some(b"first marker".to_vec())
    );
    assert_eq!(identity, directory.identity().unwrap());
}

#[cfg(unix)]
#[test]
fn target_marker_files_do_not_follow_links_to_a_different_directory() {
    use std::{fs, os::unix::fs::symlink};
    let root = tempfile::tempdir().unwrap();
    let actual = root.path().join("actual");
    let alias = root.path().join("alias");
    fs::create_dir(&actual).unwrap();
    symlink(&actual, &alias).unwrap();
    assert!(MarkerDirectory::open(&alias).is_err());
    let directory = MarkerDirectory::open(&actual.canonicalize().unwrap()).unwrap();
    let marker = actual.join(MARKER_NAME);
    let other = root.path().join("other");
    fs::write(&other, b"untouched").unwrap();
    symlink(&other, &marker).unwrap();
    assert!(directory.marker(4096).is_err());
    assert!(
        directory
            .create_marker(b"invalid update", &[6; 32], || Ok(()))
            .is_err()
    );
    assert_eq!(fs::read(&other).unwrap(), b"untouched");
    assert!(marker.symlink_metadata().unwrap().file_type().is_symlink());
}

#[cfg(any(target_os = "linux", target_os = "android"))]
#[test]
fn target_marker_files_accept_search_only_ancestors() {
    use std::{fs, os::unix::fs::PermissionsExt};

    let root = tempfile::tempdir().unwrap();
    let parent = root.path().canonicalize().unwrap().join("search-only");
    let target = parent.join("target");
    fs::create_dir_all(&target).unwrap();
    let permissions = fs::metadata(&parent).unwrap().permissions();
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o100)).unwrap();
    let observed = MarkerDirectory::open(&target).and_then(|directory| {
        directory.create_marker(b"original", &[7; 32], || Ok(()))?;
        directory.marker(4096)
    });
    fs::set_permissions(&parent, permissions).unwrap();
    assert_eq!(observed.unwrap(), Some(b"original".to_vec()));
}

#[cfg(unix)]
#[test]
fn target_marker_files_exclusive_publication_releases_only_complete_bytes() {
    use std::{fs, os::unix::fs::MetadataExt as _};

    let root = tempfile::tempdir().unwrap();
    let directory = MarkerDirectory::open(&root.path().canonicalize().unwrap()).unwrap();
    let marker = root.path().join(MARKER_NAME);
    let approved = || {
        if let Ok(metadata) = fs::metadata(&marker) {
            assert_eq!(metadata.mode() & 0o777, 0);
            assert_eq!(metadata.len(), b"complete marker".len() as u64);
            if rustix::process::geteuid().as_raw() != 0 {
                assert_eq!(
                    directory.marker(4096).unwrap_err().kind(),
                    io::ErrorKind::PermissionDenied
                );
            }
        }
        Ok(())
    };
    directory
        .publish_exclusive_marker(b"complete marker", approved)
        .unwrap();
    assert_eq!(fs::metadata(&marker).unwrap().mode() & 0o777, 0o600);
    assert_eq!(fs::read(&marker).unwrap(), b"complete marker");
    assert_eq!(
        directory
            .publish_exclusive_marker(b"replacement", || Ok(()))
            .unwrap_err()
            .kind(),
        io::ErrorKind::AlreadyExists
    );
    assert_eq!(fs::read(&marker).unwrap(), b"complete marker");
}

#[cfg(unix)]
#[test]
fn target_marker_files_exclusive_publication_leaves_interruption_unresolved() {
    use std::{fs, os::unix::fs::MetadataExt as _};

    let root = tempfile::tempdir().unwrap();
    let directory = MarkerDirectory::open(&root.path().canonicalize().unwrap()).unwrap();
    let marker = root.path().join(MARKER_NAME);
    let expired_before_visibility = || {
        if marker.exists() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "permit expired",
            ));
        }
        Ok(())
    };
    assert_eq!(
        directory
            .publish_exclusive_marker(b"original", expired_before_visibility)
            .unwrap_err()
            .kind(),
        io::ErrorKind::PermissionDenied
    );
    let before = fs::metadata(&marker).unwrap();
    assert_eq!(before.mode() & 0o777, 0);
    if rustix::process::geteuid().as_raw() != 0 {
        assert_eq!(
            directory.marker(4096).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }
    assert_eq!(
        directory
            .publish_exclusive_marker(b"new claim", || Ok(()))
            .unwrap_err()
            .kind(),
        io::ErrorKind::AlreadyExists
    );
    let after = fs::metadata(&marker).unwrap();
    assert_eq!(before.ino(), after.ino());
    assert_eq!(before.mode(), after.mode());
    assert_eq!(before.len(), after.len());
}
