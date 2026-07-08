#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionInfo {
    pub name: &'static str,
    pub version: &'static str,
}

pub fn version_info() -> VersionInfo {
    VersionInfo {
        name: "deepseek-infra-rust-core",
        version: "0.1.0",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_info_matches_expected() {
        let info = version_info();
        assert_eq!(info.name, "deepseek-infra-rust-core");
        assert_eq!(info.version, "0.1.0");
    }

    #[test]
    fn version_info_is_cloneable_and_equatable() {
        let a = version_info();
        let b = a.clone();
        assert_eq!(a, b);
    }
}
