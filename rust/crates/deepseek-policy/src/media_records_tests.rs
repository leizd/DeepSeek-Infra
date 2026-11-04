use super::*;
use serde_json::json;
use std::io::{Cursor, Seek};

fn document(rows: &[Value]) -> Vec<u8> {
    serde_json::to_vec(&json!({"media":rows})).unwrap()
}
fn record(id: &str, title: &str) -> Value {
    json!({"mediaId":id,"title":title,"type":"image","metadata":{},
        "createdAt":"2026-01-01T00:00:00+00:00","updatedAt":"2026-01-01T00:00:00+00:00"})
}
fn retained(records: &Records) -> Vec<Value> {
    let mut bytes = Vec::new();
    records.write_retained_array(&mut bytes).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[test]
fn streaming_parser_matches_legacy_normalization_for_valid_and_malformed_documents() {
    let inputs = [
        document(&[
            record("media_one", "First"),
            record("media_one", "Duplicate"),
            json!({"type":"invalid"}),
            json!(null),
        ]),
        br#"{"media":[],"media":false}"#.to_vec(),
        br#"{"media":[{"mediaId":"media_one"}]} trailing"#.to_vec(),
        br#"{"ignored":{"unicode":"\uD83D\uDE00","number":1e400},"media":[]}"#.to_vec(),
        br#"[null,{"media":[]}]"#.to_vec(),
    ];
    for bytes in inputs {
        assert_eq!(
            retained(&Records::from_reader(bytes.as_slice()).unwrap()),
            legacy_normalized_items(&bytes)
        );
    }
}

#[test]
fn an_early_syntax_error_cannot_hide_a_later_source_io_failure() {
    struct LateError(bool);
    impl Read for LateError {
        fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
            if self.0 {
                return Err(io::Error::other("late source failure"));
            }
            self.0 = true;
            bytes[0] = b'!';
            Ok(1)
        }
    }
    assert!(Records::from_reader(LateError(false)).is_err());
}

#[test]
fn only_the_complete_winning_array_uses_normalization_clocks() {
    let bytes = br#"{"media":[{"mediaId":"media_discarded"}],"ignored":{"nested":[0,null,false]},"media":[{"mediaId":"media_first","type":"image"},{"mediaId":"media_second","type":"image"}]}"#;
    let mut clock = 0;
    let records = Records::parse(
        bytes.as_slice(),
        RawRecords::empty().unwrap(),
        Records::empty().unwrap(),
        |row| {
            clock += 1;
            let mut value = normalize_media_record(row)?;
            value["createdAt"] = json!(clock);
            Ok(value)
        },
    )
    .unwrap();
    assert_eq!(clock, 2);
    assert!(!records.contains("media_discarded").unwrap());
    assert_eq!(
        records.first("media_first").unwrap().unwrap().1["createdAt"],
        1
    );
    assert_eq!(
        records.first("media_second").unwrap().unwrap().1["createdAt"],
        2
    );
    for data in [
        br#"{"media":[{"mediaId":"media_one"}]} trailing"#.as_slice(),
        br#"{"media":[{"mediaId":"media_one"}],"broken":["#.as_slice(),
    ] {
        let mut calls = 0;
        let empty = Records::parse(
            data,
            RawRecords::empty().unwrap(),
            Records::empty().unwrap(),
            |row| {
                calls += 1;
                normalize_media_record(row)
            },
        )
        .unwrap();
        assert_eq!(empty.len().unwrap(), 0);
        assert_eq!(calls, 0);
    }
}

#[test]
fn first_match_delete_all_and_post_mutation_tail_match_the_original_order() {
    let mut input = vec![
        record("media_duplicate", "First"),
        record("media_duplicate", "Second"),
    ];
    for n in 0..1001 {
        input.push(record(&format!("media_{n:04}"), "Other"));
    }
    let mut records = Records::from_reader(document(&input).as_slice()).unwrap();
    let (index, mut first) = records.first("media_duplicate").unwrap().unwrap();
    assert_eq!(first["title"], "First");
    first["title"] = json!("Updated original");
    records.replace(index, &first).unwrap();
    assert_eq!(
        records.first("media_duplicate").unwrap().unwrap().1["title"],
        "Updated original"
    );
    let last = retained(&records);
    assert_eq!(last.len(), 1000);
    assert!(last.iter().all(|row| row["mediaId"] != "media_duplicate"));
    assert_eq!(records.remove_all("media_duplicate").unwrap(), 2);
    assert_eq!(records.remove_all("media_duplicate").unwrap(), 0);
    records.append(&record("media_new", "New")).unwrap();
    let last = retained(&records);
    assert_eq!(last.len(), 1000);
    assert_eq!(last.last().unwrap()["mediaId"], "media_new");
}

struct BrokenRead;
impl Read for BrokenRead {
    fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
        Err(io::Error::other("injected source I/O failure"))
    }
}
struct FullDisk(fs::File);
impl Write for FullDisk {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::Error::from_raw_os_error(if cfg!(windows) {
            112
        } else {
            28
        }))
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
impl Seek for FullDisk {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        self.0.seek(position)
    }
}

struct FlushFailure {
    inner: fs::File,
    successful_flushes: usize,
}
impl Write for FlushFailure {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.inner.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        if self.successful_flushes == 0 {
            return Err(io::Error::other("injected flush-only failure"));
        }
        self.successful_flushes -= 1;
        Ok(())
    }
}
impl Seek for FlushFailure {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        self.inner.seek(position)
    }
}

#[test]
fn source_spool_and_index_failures_never_become_an_empty_store() {
    assert!(Records::from_reader(BrokenRead).is_err());
    let bytes = document(&[record("media_one", "Retained")]);
    for normalized_failure in [false, true] {
        let mut raw = RawRecords::empty().unwrap();
        let mut normalized = Records::empty().unwrap();
        let broken = if normalized_failure {
            &mut normalized
        } else {
            &mut raw.inner
        };
        broken.writer = Box::new(FullDisk(broken.data.as_file().try_clone().unwrap()));
        assert!(Records::parse(bytes.as_slice(), raw, normalized, normalize_media_record).is_err());
    }
    for normalized_failure in [false, true] {
        let raw = RawRecords::empty().unwrap();
        let normalized = Records::empty().unwrap();
        let broken = if normalized_failure {
            &normalized
        } else {
            &raw.inner
        };
        broken.connection.execute("DROP TABLE records", []).unwrap();
        assert!(Records::parse(bytes.as_slice(), raw, normalized, normalize_media_record).is_err());
    }
    // Bytes can reach the sink while its later flush fails. Neither raw nor
    // normalized capture may report an empty/successful original store.
    for normalized_failure in [false, true] {
        let mut raw = RawRecords::empty().unwrap();
        let mut normalized = Records::empty().unwrap();
        let broken = if normalized_failure {
            &mut normalized
        } else {
            &mut raw.inner
        };
        broken.writer = Box::new(FlushFailure {
            inner: broken.data.as_file().try_clone().unwrap(),
            successful_flushes: if normalized_failure { 0 } else { 2 },
        });
        assert!(Records::parse(bytes.as_slice(), raw, normalized, normalize_media_record).is_err());
    }
    let mut raw = RawRecords::empty().unwrap();
    raw.inner.writer = Box::new(FlushFailure {
        inner: raw.inner.data.as_file().try_clone().unwrap(),
        successful_flushes: 1,
    });
    let mut capture = raw.begin_capture().unwrap();
    capture.write_all(b"{}").unwrap();
    assert!(raw.finish_capture(capture).is_err());
    let count: i64 = raw
        .inner
        .connection
        .query_row("SELECT COUNT(*) FROM records", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 0, "flush failure cannot publish a raw index entry");
}

#[test]
fn discarded_values_still_validate_numbers_utf8_and_original_json_depth() {
    let valid_record = serde_json::to_string(&record("media_one", "Retained")).unwrap();
    // Actual original Python goldens cover both feature graphs, non-finite
    // discarded scalars and accepted nesting. Do not make serde the oracle.
    for ignored in [
        "1e400".to_string(),
        "1234567890123456789012345678901234567890".to_string(),
        "NaN".to_string(),
        format!("{}0{}", "[".repeat(140), "]".repeat(140)),
    ] {
        let bytes = format!(r#"{{"media":[{valid_record}],"ignored":{ignored}}}"#);
        let records = Records::from_reader(bytes.as_bytes()).unwrap();
        assert_eq!(records.len().unwrap(), 1);
        assert_eq!(
            retained(&records),
            legacy_normalized_items(document(&[record("media_one", "Retained")]).as_slice())
        );
    }
    for ignored in ["1e+", "01"] {
        let bytes = format!(r#"{{"media":[{valid_record}],"ignored":{ignored}}}"#);
        assert_eq!(
            Records::from_reader(bytes.as_bytes())
                .unwrap()
                .len()
                .unwrap(),
            0
        );
    }
    for suffix in [
        b",\"ignored\":\"\xff\"}".as_slice(),
        b",\"ignored\":{\"\xff\":0}}".as_slice(),
    ] {
        let mut bytes = format!(r#"{{"media":[{valid_record}]"#).into_bytes();
        bytes.extend_from_slice(suffix);
        assert!(serde_json::from_slice::<Value>(&bytes).is_err());
        // Original read_text(encoding="utf-8") raises UnicodeDecodeError;
        // this source must never admit an empty-store mutation plan.
        assert!(Records::from_reader(bytes.as_slice()).is_err());
    }
    for value in ["null", "true", "3", "\"text\"", "{}", "[]"] {
        let bytes = format!(r#"{{"media":[{{"mediaId":"media_discarded"}}],"media":{value}}}"#);
        assert_eq!(
            Records::from_reader(Cursor::new(bytes))
                .unwrap()
                .len()
                .unwrap(),
            0
        );
    }
}

#[test]
fn changed_private_spool_bytes_are_detected_before_emission_or_lookup() {
    let records =
        Records::from_reader(document(&[record("media_one", "Retained")]).as_slice()).unwrap();
    let mut file = fs::OpenOptions::new()
        .write(true)
        .open(records.data.path())
        .unwrap();
    file.write_all(b"{\"tampered\":true}").unwrap();
    assert!(records.first("media_one").is_err());
    assert!(records.write_retained_array(&mut Vec::new()).is_err());
    let raw =
        Records::from_raw_reader(document(&[record("media_one", "Retained")]).as_slice()).unwrap();
    let mut file = fs::OpenOptions::new()
        .write(true)
        .open(raw.inner.data.path())
        .unwrap();
    file.write_all(b"{\"tampered\":true}").unwrap();
    assert!(matches!(
        raw.visit_source(|_| Ok::<_, std::convert::Infallible>(())),
        Err(VisitError::Storage(_))
    ));
}

// Frozen full-value worker oracle; only its name differs.
fn legacy_normalized_items(before: &[u8]) -> Vec<Value> {
    let document: Value = serde_json::from_slice(before).unwrap_or(Value::Null);
    document
        .get("media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|v| v.is_object())
        .filter_map(|v| normalize_media_record(v).ok())
        .collect()
}
