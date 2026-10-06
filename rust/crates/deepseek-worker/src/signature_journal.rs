//! Append-only Rust custody receipts. This is neither a Go database nor an
//! authority source: issuing a signature never installs/advances an epoch.
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use std::{fs, io::Read, path::Path, time::Duration};
use tonic::Status;

const APPLICATION_ID: u32 = 0x44534353; // DSCS
const SCHEMA: [&str; 7] = [
    "CREATE TABLE signatures (request_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, document BLOB NOT NULL) STRICT",
    "CREATE TRIGGER signatures_no_update BEFORE UPDATE ON signatures BEGIN SELECT RAISE(ABORT,'immutable signatures'); END",
    "CREATE TRIGGER signatures_no_delete BEFORE DELETE ON signatures BEGIN SELECT RAISE(ABORT,'immutable signatures'); END",
    "CREATE TRIGGER signatures_no_replace BEFORE INSERT ON signatures WHEN EXISTS (SELECT 1 FROM signatures WHERE request_id=NEW.request_id OR nonce=NEW.nonce) BEGIN SELECT RAISE(ABORT,'immutable signatures'); END",
    "CREATE TRIGGER identity_no_update BEFORE UPDATE ON identity BEGIN SELECT RAISE(ABORT,'immutable identity'); END",
    "CREATE TRIGGER identity_no_delete BEFORE DELETE ON identity BEGIN SELECT RAISE(ABORT,'immutable identity'); END",
    "CREATE TRIGGER identity_no_replace BEFORE INSERT ON identity WHEN EXISTS (SELECT 1 FROM identity) BEGIN SELECT RAISE(ABORT,'immutable identity'); END",
];
pub(crate) struct SignatureJournal(Connection);
fn error() -> Status {
    Status::failed_precondition("CONTROL_SIGNER_JOURNAL_INVALID")
}

impl SignatureJournal {
    pub(crate) fn open(root: &Path, binding: &str) -> Result<Self, Status> {
        let directory = root.join("rust-worker");
        if !directory.is_dir()
            || fs::symlink_metadata(&directory)
                .map_err(|_| error())?
                .file_type()
                .is_symlink()
        {
            return Err(error());
        }
        let path = directory.join("control-signatures.sqlite3");
        if path.exists()
            && fs::symlink_metadata(&path)
                .map_err(|_| error())?
                .file_type()
                .is_symlink()
        {
            return Err(error());
        }

        for suffix in ["-journal", "-wal", "-shm"] {
            let auxiliary = directory.join(format!("control-signatures.sqlite3{suffix}"));
            match fs::symlink_metadata(&auxiliary) {
                Ok(metadata) if !metadata.is_file() || metadata.file_type().is_symlink() => {
                    return Err(error());
                }
                Ok(_) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(_) => return Err(error()),
            }
        }
        let fresh = match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(_) => true,
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => false,
            Err(_) => return Err(error()),
        };
        if !fresh {
            let mut header = [0u8; 100];
            fs::File::open(&path)
                .and_then(|mut f| f.read_exact(&mut header))
                .map_err(|_| error())?;
            if &header[..16] != b"SQLite format 3\0"
                || header[68..72] != APPLICATION_ID.to_be_bytes()
            {
                return Err(error());
            }
        }
        let mut connection = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(|_| error())?;
        connection
            .busy_timeout(Duration::from_millis(250))
            .map_err(|_| error())?;
        connection
            .pragma_update(None, "synchronous", "FULL")
            .map_err(|_| error())?;
        let tx = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| error())?;
        if fresh {
            tx.execute_batch("CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL) STRICT").map_err(|_| error())?;
            tx.execute("INSERT INTO identity VALUES(1,?1)", [binding])
                .map_err(|_| error())?;
            for sql in SCHEMA {
                tx.execute_batch(sql).map_err(|_| error())?;
            }
            tx.pragma_update(None, "application_id", APPLICATION_ID)
                .map_err(|_| error())?;
            tx.pragma_update(None, "user_version", 1)
                .map_err(|_| error())?;
        } else {
            let actual: String = tx
                .query_row("SELECT binding FROM identity WHERE id=1", [], |r| r.get(0))
                .map_err(|_| error())?;
            if actual != binding
                || tx
                    .pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))
                    .map_err(|_| error())?
                    != 1
            {
                return Err(error());
            }
            let mut statement = tx
                .prepare(
                    "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name",
                )
                .map_err(|_| error())?;
            let rows = statement
                .query_map([], |r| r.get::<_, String>(0))
                .map_err(|_| error())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|_| error())?;
            let mut expected = SCHEMA
                .iter()
                .map(|sql| sql.trim_end_matches(';').to_string())
                .collect::<Vec<_>>();
            expected.push("CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL) STRICT".into());
            expected.sort();
            let mut actual = rows;
            actual.sort();
            if actual != expected {
                return Err(error());
            }
            let integrity: String = tx
                .query_row("PRAGMA quick_check", [], |r| r.get(0))
                .map_err(|_| error())?;
            if integrity != "ok" {
                return Err(error());
            }
        }
        tx.commit().map_err(|_| error())?;
        Ok(Self(connection))
    }

    pub(crate) fn existing(&self, request_id: &str) -> Result<Option<Vec<u8>>, Status> {
        self.0
            .query_row(
                "SELECT document FROM signatures WHERE request_id=?1",
                [request_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|_| error())
    }

    pub(crate) fn record(
        &mut self,
        request_id: &str,
        nonce: &str,
        document: &[u8],
    ) -> Result<(), Status> {
        let tx = self
            .0
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| error())?;
        let previous: Option<(String, Vec<u8>)> = tx
            .query_row(
                "SELECT nonce,document FROM signatures WHERE request_id=?1",
                [request_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|_| error())?;
        if let Some((prior_nonce, prior_document)) = previous {
            if prior_nonce != nonce || prior_document != document {
                return Err(Status::already_exists("CONTROL_SIGNER_REQUEST_REPLAY"));
            }
        } else {
            let used: bool = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM signatures WHERE nonce=?1)",
                    [nonce],
                    |r| r.get(0),
                )
                .map_err(|_| error())?;
            if used {
                return Err(Status::already_exists("CONTROL_SIGNER_NONCE_REUSE"));
            }
            tx.execute(
                "INSERT INTO signatures VALUES(?1,?2,?3)",
                params![request_id, nonce, document],
            )
            .map_err(|_| error())?;
        }
        tx.commit().map_err(|_| error())?;
        Ok(())
    }
}
