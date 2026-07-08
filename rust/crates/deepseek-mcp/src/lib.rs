use deepseek_core::version_info;

pub fn mcp_version() -> &'static str {
    version_info().version
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mcp_version_matches_core() {
        assert_eq!(mcp_version(), deepseek_core::version_info().version);
    }
}
