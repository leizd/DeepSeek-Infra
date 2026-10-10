use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Query {
    pub raw: String,
    pub normalized: String,
    pub tokens: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueryError {
    EmptyQuery,
}

impl fmt::Display for QueryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            QueryError::EmptyQuery => write!(f, "query is empty"),
        }
    }
}

impl std::error::Error for QueryError {}

pub fn normalize_query(input: &str) -> Result<String, QueryError> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err(QueryError::EmptyQuery);
    }
    let mut out = String::with_capacity(trimmed.len());
    let mut prev_space = false;
    for c in trimmed.chars() {
        if c.is_whitespace() {
            if !prev_space {
                out.push(' ');
                prev_space = true;
            }
            continue;
        }
        prev_space = false;
        if c.is_ascii_alphabetic() {
            out.push(c.to_ascii_lowercase());
        } else {
            out.push(c);
        }
    }
    Ok(out)
}

pub fn tokenize_query(input: &str) -> Vec<String> {
    normalize_query(input)
        .map(|n| n.split_whitespace().map(|s| s.to_string()).collect())
        .unwrap_or_default()
}

pub fn parse_query(input: &str) -> Result<Query, QueryError> {
    let normalized = normalize_query(input)?;
    let tokens = normalized
        .split_whitespace()
        .map(|s| s.to_string())
        .collect();
    Ok(Query {
        raw: input.to_string(),
        normalized,
        tokens,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_normalization_handles_ascii() {
        let q = normalize_query("  Hello   WORLD  ").unwrap();
        assert_eq!(q, "hello world");
    }

    #[test]
    fn query_normalization_preserves_cjk() {
        let q = normalize_query("  Rust 语言  ").unwrap();
        assert_eq!(q, "rust 语言");
    }

    #[test]
    fn query_normalization_rejects_empty_query() {
        assert!(matches!(
            normalize_query("   "),
            Err(QueryError::EmptyQuery)
        ));
        assert!(matches!(normalize_query(""), Err(QueryError::EmptyQuery)));
    }

    #[test]
    fn query_tokenization_splits_on_whitespace() {
        let tokens = tokenize_query("hello 世界 rust");
        assert_eq!(tokens, vec!["hello", "世界", "rust"]);
    }

    #[test]
    fn query_parsing_preserves_raw_and_normalized() {
        let q = parse_query("  DeEpSeEk  ").unwrap();
        assert_eq!(q.raw, "  DeEpSeEk  ");
        assert_eq!(q.normalized, "deepseek");
        assert_eq!(q.tokens, vec!["deepseek"]);
    }
}
