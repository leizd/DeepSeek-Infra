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

    #[cfg(unix)]
    fn secret_handle(value: &[u8]) -> String {
        use std::os::fd::IntoRawFd;
        use std::sync::atomic::{AtomicU64, Ordering};

        static NEXT_SECRET: AtomicU64 = AtomicU64::new(0);
        let path = env::temp_dir().join(format!(
            "deepseek-backup-crypto-{}-{}",
            std::process::id(),
            NEXT_SECRET.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::write(&path, value).unwrap();
        let file = std::fs::File::open(&path).unwrap();
        std::fs::remove_file(path).unwrap();
        file.into_raw_fd().to_string()
    }

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

    #[test]
    fn identity_generation_and_derivation_are_consistent() {
        let mut generated = Vec::new();
        generate_identity(&mut generated).unwrap();
        let generated = String::from_utf8(generated).unwrap();
        assert!(generated.contains("AGE-SECRET-KEY-"));
        assert!(generated.contains("age1"));

        let identity = age::x25519::Identity::generate();
        let recipient = identity.to_public().to_string();
        let mut derived = Vec::new();
        derive_recipient(
            identity.to_string().expose_secret().to_owned(),
            &mut derived,
        )
        .unwrap();
        assert_eq!(
            String::from_utf8(derived).unwrap(),
            format!("{{\"recipients\":[\"{recipient}\"]}}\n")
        );
        assert!(derive_recipient("# comments only\n".to_owned(), Vec::new()).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn secret_reader_and_command_dispatch_cover_success_and_error_paths() {
        assert_eq!(
            read_secret(&secret_handle(b"trimmed-secret\r\n")).unwrap(),
            "trimmed-secret"
        );
        assert!(read_secret(&secret_handle(b"\r\n")).is_err());
        assert!(secret_reader("not-a-descriptor").is_err());

        let passphrase = b"command-passphrase";
        let encrypt_args = vec![
            "encrypt-passphrase".to_owned(),
            "--secret-handle".to_owned(),
            secret_handle(passphrase),
        ];
        let mut encrypted = Vec::new();
        run(&encrypt_args, &b"command payload"[..], &mut encrypted).unwrap();

        let decrypt_args = vec![
            "decrypt-passphrase".to_owned(),
            "--secret-handle".to_owned(),
            secret_handle(passphrase),
        ];
        let mut decrypted = Vec::new();
        run(&decrypt_args, &encrypted[..], &mut decrypted).unwrap();
        assert_eq!(decrypted, b"command payload");

        let identity = age::x25519::Identity::generate();
        let recipient = identity.to_public().to_string();
        let recipient_args = vec!["encrypt-age".to_owned(), recipient.clone()];
        let mut recipient_ciphertext = Vec::new();
        run(
            &recipient_args,
            &b"recipient payload"[..],
            &mut recipient_ciphertext,
        )
        .unwrap();

        let identity_args = vec![
            "decrypt-age".to_owned(),
            "--secret-handle".to_owned(),
            secret_handle(identity.to_string().expose_secret().as_bytes()),
        ];
        let mut identity_plaintext = Vec::new();
        run(
            &identity_args,
            &recipient_ciphertext[..],
            &mut identity_plaintext,
        )
        .unwrap();
        assert_eq!(identity_plaintext, b"recipient payload");

        let mut header = Vec::new();
        run(
            &["inspect-header".to_owned()],
            &recipient_ciphertext[..],
            &mut header,
        )
        .unwrap();
        assert_eq!(header, b"{\"age\":true,\"passphrase\":false}\n");

        let mut derived = Vec::new();
        run(
            &[
                "derive-recipient".to_owned(),
                "--secret-handle".to_owned(),
                secret_handle(identity.to_string().expose_secret().as_bytes()),
            ],
            io::empty(),
            &mut derived,
        )
        .unwrap();
        assert!(String::from_utf8(derived).unwrap().contains(&recipient));

        let mut generated = Vec::new();
        run(
            &["generate-identity".to_owned()],
            io::empty(),
            &mut generated,
        )
        .unwrap();
        assert!(
            String::from_utf8(generated)
                .unwrap()
                .contains("AGE-SECRET-KEY-")
        );

        let no_args: Vec<String> = Vec::new();
        assert!(run(&no_args, io::empty(), Vec::new()).is_err());
        assert!(run(&["unknown".to_owned()], io::empty(), Vec::new()).is_err());
        assert!(run(&["encrypt-age".to_owned()], io::empty(), Vec::new()).is_err());
        assert!(run(&["decrypt-age".to_owned()], io::empty(), Vec::new()).is_err());
        assert!(
            run(
                &["decrypt-age".to_owned(), "--secret-handle".to_owned()],
                io::empty(),
                Vec::new(),
            )
            .is_err()
        );
    }
}
