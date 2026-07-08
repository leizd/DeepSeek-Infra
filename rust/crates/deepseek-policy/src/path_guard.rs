use std::path::{Path, PathBuf};

use crate::PolicyDecision;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathPolicy;

impl Default for PathPolicy {
    fn default() -> Self {
        PathPolicy
    }
}

const FORBIDDEN_PATH_COMPONENTS: &[&str] = &[
    ".git", "Windows", "System32", "System", "etc", "bin", "sbin", "boot", "dev", "proc", "sys",
    "usr",
];

pub fn validate_workspace_path(
    root: &Path,
    requested: &Path,
    _policy: &PathPolicy,
) -> PolicyDecision {
    if requested
        .components()
        .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return PolicyDecision::Deny {
            reason: "parent directory traversal".to_string(),
        };
    }

    if requested.is_absolute() && !requested.starts_with(root) {
        return PolicyDecision::Deny {
            reason: "absolute path outside workspace root".to_string(),
        };
    }

    let resolved = root.join(requested);

    for component in resolved.components() {
        if let std::path::Component::Normal(name) = component {
            if let Some(name_str) = name.to_str() {
                if FORBIDDEN_PATH_COMPONENTS.contains(&name_str) {
                    return PolicyDecision::Deny {
                        reason: format!("forbidden path component: {name_str}"),
                    };
                }
            }
        }
    }

    PolicyDecision::Allow
}

pub fn normalize_path(path: &Path) -> PathBuf {
    path.components().collect::<PathBuf>()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root() -> PathBuf {
        PathBuf::from("/workspace")
    }

    #[test]
    fn path_guard_allows_workspace_relative_file() {
        let policy = PathPolicy::default();
        let decision = validate_workspace_path(&root(), Path::new("project/file.txt"), &policy);
        assert!(decision.is_allowed());
    }

    #[test]
    fn path_guard_denies_parent_traversal() {
        let policy = PathPolicy::default();
        let decision = validate_workspace_path(&root(), Path::new("../secret.txt"), &policy);
        assert!(!decision.is_allowed());
    }

    #[test]
    fn path_guard_denies_multi_parent_traversal() {
        let policy = PathPolicy::default();
        let decision = validate_workspace_path(&root(), Path::new("../../etc/passwd"), &policy);
        assert!(!decision.is_allowed());
    }

    #[test]
    fn path_guard_denies_git_directory() {
        let policy = PathPolicy::default();
        let decision = validate_workspace_path(&root(), Path::new("project/.git/config"), &policy);
        assert!(!decision.is_allowed());
    }

    #[test]
    fn path_guard_denies_windows_system32() {
        let policy = PathPolicy::default();
        let decision =
            validate_workspace_path(&root(), Path::new("C:\\Windows\\System32"), &policy);
        assert!(!decision.is_allowed());
    }

    #[test]
    fn path_guard_allows_nested_normal_directory() {
        let policy = PathPolicy::default();
        let decision = validate_workspace_path(&root(), Path::new("a/b/c/d/file.txt"), &policy);
        assert!(decision.is_allowed());
    }

    #[test]
    fn normalize_path_collapses_current_dir() {
        let path = Path::new("./a/./b");
        let normalized = normalize_path(path);
        assert!(!normalized.to_string_lossy().contains("./"));
    }
}
