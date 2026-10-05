use std::path::PathBuf;

fn main() {
    if let Err(failure) = run() {
        eprintln!("{failure}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut manifest = None;
    let mut source = None;
    let mut target = None;
    let mut revoke = false;
    let mut arguments = std::env::args_os().skip(1);
    while let Some(argument) = arguments.next() {
        if argument == "--revoke" {
            if revoke {
                return Err("repeated mirror revoke argument".into());
            }
            revoke = true;
            continue;
        }
        let value = arguments
            .next()
            .ok_or("expected --manifest FILE --source DIR --target DIR")?;
        match argument.to_str() {
            Some("--manifest") if manifest.is_none() => manifest = Some(PathBuf::from(value)),
            Some("--source") if source.is_none() => source = Some(PathBuf::from(value)),
            Some("--target") if target.is_none() => target = Some(PathBuf::from(value)),
            _ => return Err("unknown or repeated mirror import argument".into()),
        }
    }
    let operation = if revoke {
        deepseek_policy::mirror_handoff::revoke_inventory
    } else {
        deepseek_policy::mirror_handoff::import_inventory
    };
    let receipt = operation(
        &manifest.ok_or("--manifest is required")?,
        &source.ok_or("--source is required")?,
        &target.ok_or("--target is required")?,
    )?;
    println!(
        "{}",
        serde_json::to_string(&receipt).map_err(|failure| failure.to_string())?
    );
    Ok(())
}
