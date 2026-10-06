//! Offline/native provisioning; stdout contains public metadata only.
use deepseek_federation::create_control_signer_bundle;
use std::{
    fs,
    io::{self, Read, Write},
    path::Path,
};
use zeroize::Zeroizing;
fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "control signer provisioning rejected",
    )
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path =
        std::env::var("DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE").map_err(|_| invalid())?;
    let password_path =
        std::env::var("DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE").map_err(|_| invalid())?;
    let fleet = std::env::var("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID").map_err(|_| invalid())?;
    let environment =
        std::env::var("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT").map_err(|_| invalid())?;
    let metadata = fs::symlink_metadata(&password_path).map_err(|_| invalid())?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || !(16..=1024).contains(&metadata.len())
    {
        return Err(invalid().into());
    }
    let mut password = Zeroizing::new(Vec::new());
    fs::File::open(&password_path)?
        .take(1025)
        .read_to_end(&mut password)?;
    let bundle =
        create_control_signer_bundle(&password, &fleet, &environment).map_err(|_| invalid())?;
    let raw = serde_json::to_vec(&bundle).map_err(|_| invalid())?;
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut output = options.open(Path::new(&path)).map_err(|_| invalid())?;
    output.write_all(&raw)?;
    output.sync_all()?;
    println!("{}", bundle["binding"]);
    Ok(())
}
