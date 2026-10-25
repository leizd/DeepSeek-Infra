use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};

const OUTSIDE: &str = "path must stay within the configured workspace";

fn absolute_root(workspace_root: &Path) -> Result<PathBuf, String> {
    std::path::absolute(workspace_root)
        .map(|root| normalize_lexical(&root))
        .map_err(|_| "configured workspace cannot be resolved".to_string())
}

/// Resolve lexical paths, including relative workspace configuration.
pub fn resolve_workspace_path(workspace_root: &Path, candidate: &str) -> Result<PathBuf, String> {
    if candidate.contains('\0') {
        return Err("path contains a null byte".to_string());
    }
    let root = absolute_root(workspace_root)?;
    let candidate = Path::new(candidate);
    let joined = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        root.join(candidate)
    };
    let resolved = normalize_lexical(&joined);
    if !resolved.starts_with(&root) {
        return Err(OUTSIDE.to_string());
    }
    Ok(resolved)
}

/// Check current filesystem ancestry. This is not a race-resistant OS sandbox.
pub fn resolve_workspace_io_path(
    workspace_root: &Path,
    candidate: &str,
) -> Result<PathBuf, String> {
    let root = absolute_root(workspace_root)?;
    let resolved = resolve_workspace_path(&root, candidate)?;
    let physical_root = root
        .canonicalize()
        .map_err(|_| "configured workspace cannot be resolved".to_string())?;
    let relative = resolved
        .strip_prefix(&root)
        .map_err(|_| OUTSIDE.to_string())?;
    let mut physical = physical_root.clone();
    for component in relative.components() {
        physical.push(component.as_os_str());
        match std::fs::symlink_metadata(&physical) {
            Ok(_) => {
                physical = physical.canonicalize().map_err(|_| OUTSIDE.to_string())?;
                if !physical.starts_with(&physical_root) {
                    return Err(OUTSIDE.to_string());
                }
            }
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(_) => return Err(OUTSIDE.to_string()),
        }
    }
    Ok(resolved)
}

/// Preserve the existing pytest-shaped selector contract and absolute argument.
pub fn resolve_task_target(workspace_root: &Path, target: &str) -> Result<PathBuf, String> {
    let root = absolute_root(workspace_root)?;
    let resolved = resolve_workspace_path(&root, target)?;
    let relative = resolved
        .strip_prefix(&root)
        .map_err(|_| OUTSIDE.to_string())?
        .to_str()
        .ok_or_else(|| "task target cannot be represented as UTF-8".to_string())?;
    let file = relative.split_once("::").map_or(relative, |(file, _)| file);
    resolve_workspace_io_path(&root, file)?;
    Ok(resolved)
}

fn normalize_lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct Fixture {
        directory: PathBuf,
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            static COUNTER: AtomicU64 = AtomicU64::new(0);
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let directory = std::env::temp_dir().join(format!(
                "native-mcp-path-{}-{nanos}-{}",
                std::process::id(),
                COUNTER.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&directory).unwrap();
            let root = directory.join("workspace");
            std::fs::create_dir(&root).unwrap();
            std::fs::write(root.join("allowed.txt"), "inside").unwrap();
            std::fs::write(directory.join("outside.txt"), "outside").unwrap();
            Self { directory, root }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            assert!(self.directory.is_absolute());
            assert_eq!(
                self.directory.parent(),
                Some(std::env::temp_dir().as_path())
            );
            assert!(
                self.directory
                    .file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("native-mcp-path-")
            );
            std::fs::remove_dir_all(&self.directory).unwrap();
        }
    }

    #[test]
    fn relative_workspace_is_anchored_before_lexical_confinement() {
        let root = std::env::current_dir().unwrap();
        assert_eq!(
            resolve_workspace_path(Path::new("."), "Cargo.toml").unwrap(),
            root.join("Cargo.toml")
        );
        assert!(resolve_workspace_path(Path::new("."), "../outside.txt").is_err());
    }

    #[test]
    fn parent_absolute_and_sibling_prefix_targets_are_rejected() {
        let fixture = Fixture::new();
        for target in [
            "../outside.txt".to_string(),
            fixture.directory.join("outside.txt").display().to_string(),
            fixture
                .directory
                .join("workspace-extra/outside.txt")
                .display()
                .to_string(),
        ] {
            assert_eq!(
                resolve_task_target(&fixture.root, &target).unwrap_err(),
                OUTSIDE
            );
        }
    }

    #[test]
    fn valid_paths_and_leading_option_filenames_become_absolute() {
        let fixture = Fixture::new();
        std::fs::create_dir(fixture.root.join("nested")).unwrap();
        std::fs::write(fixture.root.join("--leading.txt"), "inside").unwrap();
        for (target, expected) in [
            ("allowed.txt", "allowed.txt"),
            ("nested/../allowed.txt", "allowed.txt"),
            ("--leading.txt", "--leading.txt"),
        ] {
            assert_eq!(
                resolve_task_target(&fixture.root, target).unwrap(),
                fixture.root.join(expected)
            );
        }
    }

    #[test]
    fn task_selector_is_retained_after_validating_its_file() {
        let fixture = Fixture::new();
        let target = "allowed.txt::Suite::test_case[value]";
        assert_eq!(
            resolve_task_target(&fixture.root, target).unwrap(),
            fixture.root.join(target)
        );
    }

    #[test]
    fn null_bytes_in_paths_or_selectors_are_rejected() {
        let fixture = Fixture::new();
        for target in ["bad\0file", "allowed.txt::bad\0selector"] {
            assert_eq!(
                resolve_task_target(&fixture.root, target).unwrap_err(),
                "path contains a null byte"
            );
        }
    }

    #[test]
    fn missing_descendants_keep_the_existing_program_error_path() {
        let fixture = Fixture::new();
        assert_eq!(
            resolve_task_target(&fixture.root, "missing/test_file.py").unwrap(),
            fixture.root.join("missing/test_file.py")
        );
    }

    #[cfg(unix)]
    #[test]
    fn internal_symlinks_keep_the_requested_absolute_name() {
        let fixture = Fixture::new();
        std::os::unix::fs::symlink("allowed.txt", fixture.root.join("inside.txt")).unwrap();
        assert_eq!(
            resolve_task_target(&fixture.root, "inside.txt").unwrap(),
            fixture.root.join("inside.txt")
        );
    }

    #[cfg(unix)]
    #[test]
    fn external_and_dangling_symlinks_are_rejected_before_io() {
        let fixture = Fixture::new();
        for (name, destination) in [
            ("outside.txt", "../outside.txt"),
            ("dangling.txt", "../missing.txt"),
        ] {
            std::os::unix::fs::symlink(destination, fixture.root.join(name)).unwrap();
            assert_eq!(
                resolve_task_target(&fixture.root, name).unwrap_err(),
                OUTSIDE
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn workspace_colons_are_not_interpreted_as_test_selectors() {
        let fixture = Fixture::new();
        let root = fixture.directory.join("workspace::named");
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("allowed.txt"), "inside").unwrap();
        assert_eq!(
            resolve_task_target(&root, "allowed.txt::case").unwrap(),
            root.join("allowed.txt::case")
        );
    }
}
