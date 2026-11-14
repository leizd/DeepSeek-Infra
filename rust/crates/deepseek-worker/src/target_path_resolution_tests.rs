use super::*;

#[test]
fn target_path_resolution_preserves_missing_suffixes_and_non_directory_parents() {
    let root = tempfile::tempdir().unwrap();
    let base = fs::canonicalize(root.path()).unwrap();
    fs::create_dir(base.join("directory")).unwrap();
    fs::write(base.join("file"), b"retained").unwrap();
    for (input, expected) in [
        (base.join("missing/child"), base.join("missing/child")),
        (base.join("file/child"), base.join("file/child")),
        (base.join("missing/../directory"), base.join("directory")),
        (base.join("directory/./../file"), base.join("file")),
    ] {
        assert_eq!(
            Path::new(&resolve(input.to_str().unwrap()).unwrap()),
            expected
        );
    }
    assert_eq!(fs::read(base.join("file")).unwrap(), b"retained");
    assert!(!base.join("missing").exists());
    assert_eq!(fs::read_dir(&base).unwrap().count(), 2);
}

#[test]
fn target_path_resolution_rejects_invalid_wire_paths() {
    for raw in ["relative", "", "/embedded\0path"] {
        assert_eq!(
            resolve(raw).unwrap_err().code(),
            tonic::Code::InvalidArgument
        );
    }
    let raw = std::env::temp_dir().join("a".repeat(MAX_PATH));
    assert_eq!(
        resolve(raw.to_str().unwrap()).unwrap_err().code(),
        tonic::Code::InvalidArgument
    );
}

#[cfg(unix)]
#[test]
fn target_path_resolution_follows_links_before_parent_components_and_retains_broken_links() {
    use std::os::unix::fs::symlink;
    let root = tempfile::tempdir().unwrap();
    let base = fs::canonicalize(root.path()).unwrap();
    fs::create_dir_all(base.join("real/nested")).unwrap();
    symlink("real/nested", base.join("alias")).unwrap();
    symlink("missing/child", base.join("broken")).unwrap();
    symlink(base.join("real/nested"), base.join("absolute")).unwrap();
    for (input, expected) in [
        ("alias/../file", "real/file"),
        ("alias/missing/../child", "real/nested/child"),
        ("absolute/../file", "real/file"),
        ("broken/leaf", "missing/child/leaf"),
        ("broken/../leaf", "missing/leaf"),
        ("alias/../../alias", "real/nested"),
    ] {
        assert_eq!(
            Path::new(&resolve(base.join(input).to_str().unwrap()).unwrap()),
            base.join(expected)
        );
    }
    assert!(
        fs::symlink_metadata(base.join("broken"))
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert!(!base.join("missing").exists());
    assert_eq!(fs::read_dir(&base).unwrap().count(), 4);
}

#[cfg(unix)]
#[test]
fn target_path_resolution_detects_live_loops_without_rejecting_a_cancelled_loop_suffix() {
    use std::os::unix::fs::symlink;
    let root = tempfile::tempdir().unwrap();
    let base = fs::canonicalize(root.path()).unwrap();
    symlink("loop", base.join("loop")).unwrap();
    symlink("second", base.join("first")).unwrap();
    symlink("first", base.join("second")).unwrap();
    for path in ["loop", "loop/child", "first", "second/child"] {
        assert_eq!(
            resolve(base.join(path).to_str().unwrap())
                .unwrap_err()
                .code(),
            tonic::Code::Unavailable
        );
    }
    assert_eq!(
        Path::new(&resolve(base.join("loop/..").to_str().unwrap()).unwrap()),
        base
    );
    assert_eq!(fs::read_dir(&base).unwrap().count(), 3);
}

#[cfg(unix)]
#[test]
fn target_path_resolution_handles_long_acyclic_chains_without_stack_recursion() {
    use std::os::unix::fs::symlink;
    let root = tempfile::tempdir().unwrap();
    let base = fs::canonicalize(root.path()).unwrap();
    fs::create_dir(base.join("actual")).unwrap();
    for index in 0..300 {
        let target = if index == 299 {
            "actual".into()
        } else {
            format!("link-{}", index + 1)
        };
        symlink(target, base.join(format!("link-{index}"))).unwrap();
    }
    assert_eq!(
        Path::new(&resolve(base.join("link-0/child").to_str().unwrap()).unwrap()),
        base.join("actual/child")
    );
    assert_eq!(fs::read_dir(&base).unwrap().count(), 301);
}

#[cfg(windows)]
#[test]
fn target_path_resolution_windows_retains_real_case_and_normalizes_before_links() {
    let root = tempfile::tempdir().unwrap();
    fs::create_dir(root.path().join("ActualCase")).unwrap();
    let expected = fs::canonicalize(root.path().join("ActualCase")).unwrap();
    let input = root.path().join("actualcase/missing/../child");
    let actual = resolve(input.to_str().unwrap()).unwrap();
    let actual = Path::new(&actual);
    assert_eq!(
        fs::canonicalize(actual.parent().unwrap()).unwrap(),
        expected
    );
    assert_eq!(actual.file_name().unwrap(), "child");
    assert!(!root.path().join("ActualCase/child").exists());
}

#[cfg(unix)]
#[test]
fn target_path_resolution_bounds_adversarial_link_expansion_without_effects() {
    use std::os::unix::fs::symlink;
    let root = tempfile::tempdir().unwrap();
    let base = fs::canonicalize(root.path()).unwrap();
    for index in 0..=MAX_LINKS {
        symlink(
            format!("budget-{}", index + 1),
            base.join(format!("budget-{index}")),
        )
        .unwrap();
    }
    assert_eq!(
        resolve(base.join("budget-0").to_str().unwrap())
            .unwrap_err()
            .code(),
        tonic::Code::Unavailable
    );
    assert_eq!(fs::read_dir(&base).unwrap().count(), MAX_LINKS + 1);
    assert!(!base.join(format!("budget-{}", MAX_LINKS + 1)).exists());
}
