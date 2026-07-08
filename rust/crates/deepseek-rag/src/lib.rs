use deepseek_core::version_info;

pub fn rag_version() -> &'static str {
    version_info().version
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rag_version_matches_core() {
        assert_eq!(rag_version(), deepseek_core::version_info().version);
    }
}
