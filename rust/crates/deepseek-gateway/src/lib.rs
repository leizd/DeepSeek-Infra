use deepseek_core::version_info;

pub fn gateway_version() -> &'static str {
    version_info().version
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gateway_version_matches_core() {
        assert_eq!(gateway_version(), deepseek_core::version_info().version);
    }
}
