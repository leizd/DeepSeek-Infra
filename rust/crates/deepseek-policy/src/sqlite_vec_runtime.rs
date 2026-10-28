//! Statically linked sqlite-vec for native index readers and the fenced writer.
//! No Python package, external extension path or loadable library is required.
use rusqlite::{Error, ffi};
use std::ffi::{c_char, c_int};
use std::sync::OnceLock;

type ExtensionEntry = unsafe extern "C" fn(
    *mut ffi::sqlite3,
    *mut *mut c_char,
    *const ffi::sqlite3_api_routines,
) -> c_int;

/// Register the built-in module once, before opening a RAG connection.
/// Registration changes no database and does not grant write authority.
pub fn register() -> Result<(), Error> {
    static RESULT: OnceLock<c_int> = OnceLock::new();
    let code = *RESULT.get_or_init(|| {
        // SAFETY: the pinned crate statically compiles sqlite-vec.c with
        // SQLITE_CORE. Its Rust declaration erases the standard SQLite extension
        // entry signature; the upstream entry point has exactly ExtensionEntry's
        // ABI and is registered with this same bundled SQLite implementation.
        // SQLite owns registration; no borrowed pointer or external library can
        // outlive its allocation. OnceLock prevents concurrent registration.
        unsafe {
            let entry = std::mem::transmute::<*const (), ExtensionEntry>(
                sqlite_vec::sqlite3_vec_init as *const (),
            );
            ffi::sqlite3_auto_extension(Some(entry))
        }
    });
    if code == ffi::SQLITE_OK {
        Ok(())
    } else {
        Err(Error::SqliteFailure(
            ffi::Error::new(code),
            Some("SQLITE_VEC_REGISTRATION_FAILED".into()),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    #[test]
    fn embedded_vector_module_is_available_on_each_native_connection() {
        register().unwrap();
        let threads: Vec<_> = (0..4)
            .map(|_| {
                std::thread::spawn(|| {
                    register().unwrap();
                    let connection = Connection::open_in_memory().unwrap();
                    let version: String = connection
                        .query_row("SELECT vec_version()", [], |row| row.get(0))
                        .unwrap();
                    assert_eq!(version, "v0.1.9");
                    connection
                        .execute_batch("CREATE VIRTUAL TABLE vectors USING vec0(value float[2]);")
                        .unwrap();
                    connection
                        .execute(
                            "INSERT INTO vectors(rowid,value) VALUES(7, '[1,0]'),(8,'[0,1]')",
                            [],
                        )
                        .unwrap();
                    let nearest: (i64, f64) = connection
                        .query_row(
                            "SELECT rowid,distance FROM vectors WHERE value MATCH '[1,0]' AND k=1",
                            [],
                            |row| Ok((row.get(0)?, row.get(1)?)),
                        )
                        .unwrap();
                    assert_eq!(nearest, (7, 0.0));
                    connection
                        .execute("DELETE FROM vectors WHERE rowid=7", [])
                        .unwrap();
                    let retained: i64 = connection
                        .query_row("SELECT rowid FROM vectors", [], |row| row.get(0))
                        .unwrap();
                    assert_eq!(retained, 8);
                })
            })
            .collect();
        for thread in threads {
            thread.join().unwrap();
        }
    }
}
