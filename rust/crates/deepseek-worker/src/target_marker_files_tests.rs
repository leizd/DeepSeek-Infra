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
