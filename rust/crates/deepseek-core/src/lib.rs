use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RequestId {
    String(String),
    Number(i64),
    Null,
}

impl RequestId {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            RequestId::String(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_number(&self) -> Option<i64> {
        match self {
            RequestId::Number(n) => Some(*n),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, RequestId::Null)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TraceId(pub String);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct UnixTimestampMillis(pub u64);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeepseekError {
    pub code: String,
    pub message: String,
}

pub type DeepseekResult<T> = Result<T, DeepseekError>;

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

    #[test]
    fn request_id_string_roundtrips() {
        let id = RequestId::String("req-123".to_string());
        let json = serde_json::to_string(&id).unwrap();
        assert_eq!(json, "\"req-123\"");
        let parsed: RequestId = serde_json::from_str(&json).unwrap();
        assert_eq!(id, parsed);
        assert_eq!(parsed.as_str(), Some("req-123"));
    }

    #[test]
    fn request_id_number_roundtrips() {
        let id = RequestId::Number(42);
        let json = serde_json::to_string(&id).unwrap();
        assert_eq!(json, "42");
        let parsed: RequestId = serde_json::from_str(&json).unwrap();
        assert_eq!(id, parsed);
        assert_eq!(parsed.as_number(), Some(42));
    }

    #[test]
    fn request_id_null_roundtrips() {
        let id = RequestId::Null;
        let json = serde_json::to_string(&id).unwrap();
        assert_eq!(json, "null");
        let parsed: RequestId = serde_json::from_str(&json).unwrap();
        assert_eq!(id, parsed);
        assert!(parsed.is_null());
    }

    #[test]
    fn trace_id_roundtrips() {
        let trace = TraceId("trace-abc".to_string());
        let json = serde_json::to_string(&trace).unwrap();
        let parsed: TraceId = serde_json::from_str(&json).unwrap();
        assert_eq!(trace, parsed);
    }

    #[test]
    fn unix_timestamp_millis_roundtrips() {
        let ts = UnixTimestampMillis(1_700_000_000_000);
        let json = serde_json::to_string(&ts).unwrap();
        let parsed: UnixTimestampMillis = serde_json::from_str(&json).unwrap();
        assert_eq!(ts, parsed);
    }

    #[test]
    fn deepseek_error_roundtrips() {
        let err = DeepseekError {
            code: "E001".to_string(),
            message: "something went wrong".to_string(),
        };
        let json = serde_json::to_string(&err).unwrap();
        let parsed: DeepseekError = serde_json::from_str(&json).unwrap();
        assert_eq!(err, parsed);
    }
}
