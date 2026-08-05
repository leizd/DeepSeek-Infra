use std::env;
use std::io::{self, BufReader, Read, Write};
use std::str::FromStr;

use age::secrecy::{ExposeSecret, SecretString};
use age::{Decryptor, Encryptor, Identity, Recipient};

const MAX_SECRET_BYTES: u64 = 1024 * 1024;

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

#[cfg(unix)]
fn secret_reader(raw: &str) -> io::Result<std::fs::File> {
    use std::os::fd::FromRawFd;

    let descriptor = raw
        .parse::<i32>()
        .map_err(|_| invalid("invalid secret descriptor"))?;
    // SAFETY: the parent creates a dedicated inheritable pipe for this child and
    // transfers ownership of the read end to the helper.
    Ok(unsafe { std::fs::File::from_raw_fd(descriptor) })
}

#[cfg(windows)]
fn secret_reader(raw: &str) -> io::Result<std::fs::File> {
    use std::os::windows::io::FromRawHandle;

    let handle = raw
        .parse::<usize>()
        .map_err(|_| invalid("invalid secret handle"))?;
    if handle == 0 {
        return Err(invalid("invalid secret handle"));
    }
    // SAFETY: the parent creates a dedicated inheritable pipe for this child and
    // transfers ownership of the read handle to the helper.
    Ok(unsafe { std::fs::File::from_raw_handle(handle as *mut std::ffi::c_void) })
}

fn read_secret(raw_handle: &str) -> io::Result<String> {
    let mut value = String::new();
    secret_reader(raw_handle)?
        .take(MAX_SECRET_BYTES + 1)
        .read_to_string(&mut value)?;
    if value.len() as u64 > MAX_SECRET_BYTES {
        return Err(invalid("secret exceeds maximum size"));
    }
    while value.ends_with('\r') || value.ends_with('\n') {
        value.pop();
    }
    if value.is_empty() {
        return Err(invalid("secret is empty"));
    }
    Ok(value)
}

fn encrypt_passphrase<R: Read, W: Write>(
    input: R,
    output: W,
    passphrase: String,
) -> io::Result<()> {
    let encryptor = Encryptor::with_user_passphrase(SecretString::from(passphrase));
    let mut encrypted = encryptor
        .wrap_output(output)
        .map_err(|error| invalid(error.to_string()))?;
    io::copy(&mut BufReader::new(input), &mut encrypted)?;
    encrypted
        .finish()
        .map_err(|error| invalid(error.to_string()))?;
    Ok(())
}

fn recipients(values: &[String]) -> io::Result<Vec<age::x25519::Recipient>> {
    if values.is_empty() {
        return Err(invalid("at least one recipient is required"));
    }
    values
        .iter()
        .map(|value| {
            age::x25519::Recipient::from_str(value).map_err(|error| invalid(error.to_string()))
        })
        .collect()
}

fn encrypt_recipients<R: Read, W: Write>(input: R, output: W, values: &[String]) -> io::Result<()> {
    let parsed = recipients(values)?;
    let dynamic: Vec<&dyn Recipient> = parsed.iter().map(|value| value as &dyn Recipient).collect();
    let encryptor = Encryptor::with_recipients(dynamic.into_iter())
        .map_err(|error| invalid(error.to_string()))?;
    let mut encrypted = encryptor
        .wrap_output(output)
        .map_err(|error| invalid(error.to_string()))?;
    io::copy(&mut BufReader::new(input), &mut encrypted)?;
    encrypted
        .finish()
        .map_err(|error| invalid(error.to_string()))?;
    Ok(())
}

fn parse_identities(value: &str) -> io::Result<Vec<age::x25519::Identity>> {
    let parsed: Vec<_> = value
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .map(|line| {
            age::x25519::Identity::from_str(line).map_err(|error| invalid(error.to_string()))
        })
        .collect::<io::Result<_>>()?;
    if parsed.is_empty() {
        return Err(invalid("no age identity was provided"));
    }
    Ok(parsed)
}

fn decrypt_passphrase<R: Read, W: Write>(
    input: R,
    mut output: W,
    passphrase: String,
) -> io::Result<()> {
    let decryptor =
        Decryptor::new(BufReader::new(input)).map_err(|_| invalid("unable to unlock backup"))?;
    let identity = age::scrypt::Identity::new(SecretString::from(passphrase));
    let mut plaintext = decryptor
        .decrypt(std::iter::once(&identity as &dyn Identity))
        .map_err(|_| invalid("unable to unlock backup"))?;
    io::copy(&mut plaintext, &mut output).map_err(|_| invalid("unable to unlock backup"))?;
    Ok(())
}

fn decrypt_identity<R: Read, W: Write>(input: R, mut output: W, secret: String) -> io::Result<()> {
    let parsed = parse_identities(&secret)?;
    let dynamic: Vec<&dyn Identity> = parsed.iter().map(|value| value as &dyn Identity).collect();
    let decryptor =
        Decryptor::new(BufReader::new(input)).map_err(|_| invalid("unable to unlock backup"))?;
    let mut plaintext = decryptor
        .decrypt(dynamic.into_iter())
        .map_err(|_| invalid("unable to unlock backup"))?;
    io::copy(&mut plaintext, &mut output).map_err(|_| invalid("unable to unlock backup"))?;
    Ok(())
}

fn generate_identity(mut output: impl Write) -> io::Result<()> {
    let identity = age::x25519::Identity::generate();
    let encoded = identity.to_string();
    writeln!(
        output,
        "{{\"identity\":\"{}\",\"recipient\":\"{}\"}}",
        encoded.expose_secret(),
        identity.to_public()
    )
}

fn derive_recipient(secret: String, mut output: impl Write) -> io::Result<()> {
    let identities = parse_identities(&secret)?;
    let values = identities
        .iter()
        .map(|identity| format!("\"{}\"", identity.to_public()))
        .collect::<Vec<_>>()
        .join(",");
    writeln!(output, "{{\"recipients\":[{values}]}}")
}

fn inspect_header(input: impl Read, mut output: impl Write) -> io::Result<()> {
    let decryptor =
        Decryptor::new(BufReader::new(input)).map_err(|_| invalid("not an age file"))?;
    writeln!(
        output,
        "{{\"age\":true,\"passphrase\":{}}}",
        decryptor.is_scrypt()
    )
}

fn required_secret_handle(args: &[String]) -> io::Result<&str> {
    let position = args
        .iter()
        .position(|value| value == "--secret-handle")
        .ok_or_else(|| invalid("--secret-handle is required"))?;
    args.get(position + 1)
        .map(String::as_str)
        .ok_or_else(|| invalid("--secret-handle requires a value"))
}

fn run(args: &[String], input: impl Read, output: impl Write) -> io::Result<()> {
    let command = args
        .first()
        .map(String::as_str)
        .ok_or_else(|| invalid("command is required"))?;
    match command {
        "encrypt-passphrase" => {
            encrypt_passphrase(input, output, read_secret(required_secret_handle(args)?)?)
        }
        "encrypt-age" => {
            let recipients = args
                .iter()
                .skip(1)
                .take_while(|value| value.as_str() != "--secret-handle")
                .cloned()
                .collect::<Vec<_>>();
            encrypt_recipients(input, output, &recipients)
        }
        "decrypt-passphrase" => {
            decrypt_passphrase(input, output, read_secret(required_secret_handle(args)?)?)
        }
        "decrypt-age" => {
            decrypt_identity(input, output, read_secret(required_secret_handle(args)?)?)
        }
        "generate-identity" => generate_identity(output),
        "derive-recipient" => derive_recipient(read_secret(required_secret_handle(args)?)?, output),
        "inspect-header" => inspect_header(input, output),
        _ => Err(invalid("unknown command")),
    }
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if let Err(error) = run(&args, io::stdin().lock(), io::stdout().lock()) {
        eprintln!("backup crypto operation failed: {error}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passphrase_round_trip_and_tamper_rejection() {
        let plaintext = b"manifest.json must stay private";
        let mut encrypted = Vec::new();
        encrypt_passphrase(&plaintext[..], &mut encrypted, "correct horse".to_owned()).unwrap();
        assert!(!encrypted.windows(8).any(|window| window == b"manifest"));

        let mut decrypted = Vec::new();
        decrypt_passphrase(&encrypted[..], &mut decrypted, "correct horse".to_owned()).unwrap();
        assert_eq!(decrypted, plaintext);

        let last = encrypted.len() - 1;
        encrypted[last] ^= 1;
        assert!(
            decrypt_passphrase(&encrypted[..], Vec::new(), "correct horse".to_owned()).is_err()
        );
    }

    #[test]
    fn x25519_round_trip_and_recipient_derivation() {
        let identity = age::x25519::Identity::generate();
        let encoded = identity.to_string();
        let recipient = identity.to_public().to_string();
        let mut encrypted = Vec::new();
        encrypt_recipients(&b"portable"[..], &mut encrypted, &[recipient]).unwrap();
        let mut decrypted = Vec::new();
        decrypt_identity(
            &encrypted[..],
            &mut decrypted,
            encoded.expose_secret().to_owned(),
        )
        .unwrap();
        assert_eq!(decrypted, b"portable");
    }

    #[test]
    fn age_header_reports_protection_kind() {
        let mut encrypted = Vec::new();
        encrypt_passphrase(&b"payload"[..], &mut encrypted, "secret value".to_owned()).unwrap();
        let mut report = Vec::new();
        inspect_header(&encrypted[..], &mut report).unwrap();
        assert_eq!(report, b"{\"age\":true,\"passphrase\":true}\n");
    }
}
