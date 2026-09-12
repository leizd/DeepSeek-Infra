use std::{env, fs, path::PathBuf};

use protox::prost::Message;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR")?);
    let proto_root = manifest_dir.join("../../../proto");
    let mut pending = vec![proto_root.clone()];
    let mut proto_files = Vec::new();
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            if path.is_dir() {
                if path.file_name().and_then(|name| name.to_str()) != Some("generated") {
                    pending.push(path);
                }
            } else if path.extension().and_then(|extension| extension.to_str()) == Some("proto") {
                proto_files.push(path);
            }
        }
    }
    proto_files.sort();
    if proto_files.is_empty() {
        return Err("no native protobuf sources found".into());
    }

    for proto in &proto_files {
        println!("cargo:rerun-if-changed={}", proto.display());
    }

    let frozen_descriptor_path = proto_root.join("generated/descriptor.pb");
    println!(
        "cargo:rerun-if-changed={}",
        frozen_descriptor_path.display()
    );

    let mut compiler = protox::Compiler::new([&proto_root])?;
    compiler
        .include_imports(true)
        .include_source_info(false)
        .open_files(&proto_files)?;
    let descriptors = compiler.file_descriptor_set();
    let descriptor_bytes = descriptors.encode_to_vec();
    if descriptor_bytes != fs::read(&frozen_descriptor_path)? {
        return Err("Rust descriptor drifted from the checksum-pinned protoc descriptor; run native_codegen.py --write".into());
    }
    let descriptor_path = PathBuf::from(env::var("OUT_DIR")?).join("deepseek.native.v1.bin");
    fs::write(descriptor_path, descriptor_bytes)?;

    tonic_build::configure()
        .build_client(true)
        .build_server(true)
        .message_attribute(".", "#[derive(Eq)]")
        .compile_fds(descriptors)?;
    Ok(())
}
