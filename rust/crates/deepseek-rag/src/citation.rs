use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CitationError {
    InvalidLineRange,
}

impl fmt::Display for CitationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CitationError::InvalidLineRange => write!(f, "invalid line range"),
        }
    }
}

impl std::error::Error for CitationError {}

pub fn format_citation(
    source: &str,
    start_line: Option<u32>,
    end_line: Option<u32>,
) -> Result<String, CitationError> {
    let source = source.trim();
    match (start_line, end_line) {
        (Some(start), Some(end)) if start > end => Err(CitationError::InvalidLineRange),
        (Some(start), Some(end)) => Ok(format!("{}:L{}-L{}", source, start, end)),
        (Some(start), None) => Ok(format!("{}:L{}", source, start)),
        (None, Some(_)) => Ok(source.to_string()),
        (None, None) => Ok(source.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn citation_formats_line_range() {
        let c = format_citation("docs/example.md", Some(10), Some(20)).unwrap();
        assert_eq!(c, "docs/example.md:L10-L20");
    }

    #[test]
    fn citation_falls_back_to_source_only() {
        let c = format_citation("docs/example.md", None, None).unwrap();
        assert_eq!(c, "docs/example.md");
    }

    #[test]
    fn citation_rejects_invalid_range() {
        let result = format_citation("docs/example.md", Some(20), Some(10));
        assert!(matches!(result, Err(CitationError::InvalidLineRange)));
    }

    #[test]
    fn citation_trims_source() {
        let c = format_citation("  docs/example.md  ", Some(5), None).unwrap();
        assert_eq!(c, "docs/example.md:L5");
    }
}
