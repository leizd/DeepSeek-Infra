//! The `backup-crypto` streaming age CLI.
//!
//! Everything in this binary is a command-line shell over
//! [`backup_crypto`](crate), which is also what the native data plane links against.
//! Keeping one implementation is deliberate: the mirror's sealing round trip is the
//! only proof that a generation is restorable, so it must exercise the same encrypt
//! and decrypt code the production CLI runs.

use std::env;
use std::io;

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if let Err(error) = backup_crypto::run(&args, io::stdin().lock(), io::stdout().lock()) {
        eprintln!("backup crypto operation failed: {error}");
        std::process::exit(2);
    }
}
