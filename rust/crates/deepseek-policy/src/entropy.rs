//! Injected entropy: identifiers and the wall clock's millisecond reading.
//!
//! First needed by the reminders store (`secrets.token_hex(8)` and
//! `int(time.time() * 1000)`), then by the projects store, which mints ids inside a
//! **read** path — `read_project` calls `normalize_skill_run`, which generates
//! `f"run-{secrets.token_hex(8)}"` whenever a stored run has no id. So this is a
//! general source, not a reminder concept, and two users are enough to give it its
//! own module.
//!
//! # Why there is no weak fallback
//!
//! `secrets` is explicitly the *secure* option, and these ids reach the model in
//! tool output. [`SystemEntropy`] therefore reports a failure rather than falling
//! back to a seeded but non-cryptographic generator.

use crate::app_error::AppError;

/// The non-deterministic inputs, injected so stores are testable and the parity
/// probes can pin them.
pub trait Entropy {
    /// Mirrors `secrets.token_hex(8)` — 16 lowercase hex characters.
    fn new_id(&self) -> Result<String, AppError>;
    /// Mirrors `int(time.time() * 1000)`.
    fn now_millis(&self) -> i64;
}

/// The production source: an OS CSPRNG and the wall clock.
pub struct SystemEntropy;

impl Entropy for SystemEntropy {
    fn new_id(&self) -> Result<String, AppError> {
        let mut bytes = [0u8; 8];
        os_random(&mut bytes)?;
        Ok(crate::core_utils::encode_lower_hex(&bytes))
    }

    fn now_millis(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_millis() as i64)
            .unwrap_or(0)
    }
}

/// Fill `buffer` from the OS CSPRNG.
///
/// `secrets.token_hex` is explicitly a *secure* source, so this does not fall back
/// to a seeded but non-cryptographic generator: a reminder id is handed to the
/// model in tool output, and making it guessable would be a real regression.
#[cfg(unix)]
fn os_random(buffer: &mut [u8]) -> Result<(), AppError> {
    use std::io::Read;
    let mut file = std::fs::File::open("/dev/urandom")
        .map_err(|error| AppError::invalid_payload(format!("no secure random source: {error}")))?;
    file.read_exact(buffer)
        .map_err(|error| AppError::invalid_payload(format!("no secure random source: {error}")))
}

#[cfg(windows)]
fn os_random(buffer: &mut [u8]) -> Result<(), AppError> {
    use std::ffi::c_void;

    #[link(name = "bcrypt")]
    unsafe extern "C" {
        fn BCryptGenRandom(algorithm: *mut c_void, buffer: *mut u8, length: u32, flags: u32)
        -> i32;
    }

    // BCRYPT_USE_SYSTEM_PREFERRED_RNG
    const USE_SYSTEM_PREFERRED_RNG: u32 = 0x0000_0002;
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status != 0 {
        return Err(AppError::invalid_payload(format!(
            "no secure random source: BCryptGenRandom returned {status}"
        )));
    }
    Ok(())
}
