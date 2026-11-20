use super::*;

#[test]
fn media_process_scopes_index_replacement_and_retains_exact_prior_column_and_vector_codecs() {
    let (root, key, mut journal, created) = ready(
        json!({"type":"webpage","projectId":"process_current","html":"<h1>scoped process</h1>","process":true}),
    );
    let id = created.intent.as_ref().unwrap().media_id.clone();
    let db = deepseek_policy::memory_index::local_rag_db(root.path());
    let c = Connection::open(&db).unwrap();
    let (original, original_vector_id, scope): (String, i64, String) = c
        .query_row(
            "SELECT item_id,vector_id,scope FROM rag_items WHERE source_id=?1",
            [&id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    let vector: bool = c
        .query_row(
            "SELECT COUNT(*)>0 FROM sqlite_schema WHERE name='rag_vec'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let embedding = vector.then(|| {
        c.query_row(
            "SELECT embedding FROM rag_vec WHERE rowid=?1",
            [original_vector_id],
            |r| r.get::<_, Vec<u8>>(0),
        )
        .unwrap()
    });
    let padded = " { \"originalStoredCodec\": true } ";
    c.execute(
        "UPDATE rag_items SET metadata=?1,chunk_index=-7 WHERE item_id=?2",
        params![padded, original],
    )
    .unwrap();
    let mut free = 0i64;
    for (item, collection, project) in [
        ("foreign_project", "media", "process_other"),
        ("foreign_collection", "memory", "process_current"),
    ] {
        while c
            .query_row(
                "SELECT COUNT(*) FROM rag_items WHERE vector_id=?1",
                [free],
                |r| r.get::<_, i64>(0),
            )
            .unwrap()
            != 0
        {
            free += 1;
        }
        c.execute("INSERT INTO rag_items SELECT ?1,?2,?3,source_id,?4,chunk_index,name,kind,scope,text,embedding,metadata,updated_at FROM rag_items WHERE item_id=?5",params![item,free,collection,project,original]).unwrap();
        if let Some(embedding) = &embedding {
            c.execute("INSERT INTO rag_vec(rowid,item_id,collection,source_id,project_id,scope,embedding) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![free,item,collection,id,project,scope,embedding]).unwrap();
        }
        free += 1;
    }
    let snapshot = |item: &str| {
        let row: Vec<rusqlite::types::Value> = c
            .query_row("SELECT * FROM rag_items WHERE item_id=?1", [item], |r| {
                (0usize..13).map(|n| r.get(n)).collect()
            })
            .unwrap();
        let vector_id: i64 = c
            .query_row(
                "SELECT vector_id FROM rag_items WHERE item_id=?1",
                [item],
                |r| r.get(0),
            )
            .unwrap();
        let vector = if vector {
            c.query_row("SELECT rowid,item_id,collection,source_id,project_id,scope,embedding FROM rag_vec WHERE rowid=?1",[vector_id],|r|(0usize..7).map(|n|r.get::<_,rusqlite::types::Value>(n)).collect::<Result<Vec<_>,_>>()).optional().unwrap()
        } else {
            None
        };
        (row, vector)
    };
    let foreign_project = snapshot("foreign_project");
    let foreign_collection = snapshot("foreign_collection");
    let claim = process_claim(created, true, 'a');
    let p = journal.prepare(&claim).unwrap();
    let row = journal
        .retained(&claim.intent.as_ref().unwrap().operation_id)
        .unwrap()
        .unwrap();
    let prepared =
        crate::media_process::retained(journal.creation_staging(), &claim, &row.planned).unwrap();
    assert_eq!(prepared.before_index_items.len(), 1);
    assert_eq!(prepared.before_index_items[0].metadata_json, padded);
    assert_eq!(prepared.before_index_items[0].chunk_index, -7);
    assert_eq!(
        prepared.before_index_items[0]
            .record
            .as_ref()
            .unwrap()
            .chunk_index,
        0
    );
    assert_eq!(prepared.before_index_items[0].vector.is_some(), vector);
    let result = journal.publish(&claim, &p.digest, &key).unwrap();
    assert_eq!(result.http_status, 200);
    assert_eq!(result.processed.unwrap().indexed, 1);
    assert_eq!(snapshot("foreign_project"), foreign_project);
    assert_eq!(snapshot("foreign_collection"), foreign_collection);
    assert_eq!(
        c.query_row(
            "SELECT COUNT(*) FROM rag_items WHERE item_id=?1",
            [&original],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
    assert_eq!(
        c.query_row(
            "SELECT COUNT(*) FROM rag_items WHERE collection='media' AND source_id=?1",
            [&id],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        2
    );
}
