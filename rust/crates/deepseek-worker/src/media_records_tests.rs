use super::*;

#[test]
fn shared_records_match_original_worker_normalizer() {
    let bytes = br#"{"media":[
        {"mediaId":"media_one","type":"image","title":"first","createdAt":"2026-01-01","updatedAt":"2026-01-01"},
        {"mediaId":"media_one","type":"image","title":"duplicate","createdAt":"2026-01-01","updatedAt":"2026-01-01"},
        {"mediaId":"media_two","type":"video","title":"second","createdAt":"2026-01-01","updatedAt":"2026-01-01"}
    ]}"#;
    let expected = crate::media_library::normalized_items(bytes);
    assert_eq!(
        expected.len(),
        3,
        "all positive worker fixtures must be retained"
    );
    let records = Records::from_reader(&bytes[..]).unwrap();
    assert_eq!(records.len().unwrap(), expected.len() as i64);
    for id in ["media_one", "media_two"] {
        let value = expected.iter().find(|row| row["mediaId"] == id).unwrap();
        assert_eq!(records.first(id).unwrap().unwrap().1, *value);
    }
}
