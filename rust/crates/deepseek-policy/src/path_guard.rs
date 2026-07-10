use std::path::{Path, PathBuf};

use crate::capability::{Capability, RiskLevel};
use crate::{PolicyDecision, codes};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathPolicy;

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
        return deny(
            codes::PATH_TRAVERSAL,
            "parent directory traversal is not allowed",
        );
    }

    if requested.is_absolute() && !requested.starts_with(root) {
        return deny(
            codes::PROTECTED_PATH,
            "path is outside the allowed workspace",
        );
    }

    let resolved = root.join(requested);

    for component in resolved.components() {
        if let std::path::Component::Normal(name) = component {
            if let Some(name_str) = name.to_str() {
                if FORBIDDEN_PATH_COMPONENTS
                    .iter()
                    .any(|forbidden| name_str.eq_ignore_ascii_case(forbidden))
                    || name_str.contains("Windows")
                    || name_str.contains("System32")
                    || name_str.contains("System")
                {
                    return deny(codes::PROTECTED_PATH, "path targets a protected location");
                }
            }
        }
    }

    PolicyDecision::allow(Capability::ReadFile, RiskLevel::Low)
}

fn deny(code: &str, reason: &str) -> PolicyDecision {
    PolicyDecision::deny(code, reason, Capability::ReadFile, RiskLevel::High)
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
        let policy = PathPolicy;
        let decision = validate_workspace_path(&root(), Path::new("project/file.txt"), &policy);
        assert!(decision.is_allowed());
    }

    #[test]
    fn path_guard_denies_parent_traversal() {
        let policy = PathPolicy;
        let decision = validate_workspace_path(&root(), Path::new("../secret.txt"), &policy);
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, codes::PATH_TRAVERSAL);
    }

    #[test]
    fn path_guard_denies_multi_parent_traversal() {
        let policy = PathPolicy;
        let decision = validate_workspace_path(&root(), Path::new("../../etc/passwd"), &policy);
        assert!(!decision.is_allowed());
    }

    #[test]
    fn path_guard_denies_git_directory() {
        let policy = PathPolicy;
        let decision = validate_workspace_path(&root(), Path::new("project/.git/config"), &policy);
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, codes::PROTECTED_PATH);
    }

    #[test]
    fn path_guard_denies_windows_system32() {
        let policy = PathPolicy;
        let decision =
            validate_workspace_path(&root(), Path::new("C:\\Windows\\System32"), &policy);
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, codes::PROTECTED_PATH);
        assert!(!decision.reason.contains("Windows"));
    }

    #[test]
    fn path_guard_allows_nested_normal_directory() {
        let policy = PathPolicy;
        let decision = validate_workspace_path(&root(), Path::new("a/b/c/d/file.txt"), &policy);
        assert!(decision.is_allowed());
    }
}
