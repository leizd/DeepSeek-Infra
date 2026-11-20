use super::*;

#[test]
fn streamed_image_keeps_exact_bytes_and_rejects_changed_retained_content() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("nested/custody/action/before.json");
    let bytes = b"{\"media\":[{\"mediaId\":\"media_one\"}]}";
    let image = Image::build(|output| {
        output.write_all(bytes).map_err(|_| invalid())?;
        Ok(())
    })
    .unwrap();
    let retained = image.retain(&path).unwrap();
    assert!(same(&image, &retained).unwrap());
    assert_eq!(retained.bytes_for_test(), bytes);
    assert!(image.retain(&path).is_ok());
    let mut changed = fs::read(&path).unwrap();
    changed[2] ^= 1;
    fs::write(&path, &changed).unwrap();
    assert!(retained.verify().is_err());
    assert!(image.retain(&path).is_err());
    assert!(same(&image, &retained).is_err());
    assert!(Image::Legacy(b"different".to_vec()).retain(&path).is_err());
    // Descriptor equality is immutable content identity, not physical presence.
    fs::remove_file(&path).unwrap();
    assert_eq!(retained, retained.clone());
    assert!(retained.verify().is_err());
}

#[test]
fn missing_empty_and_nonregular_source_images_preserve_the_original_semantics() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("library.json");
    let missing = Image::capture(&path).unwrap();
    assert_eq!(missing.identity(), Identity::of(b""));
    fs::write(&path, b"").unwrap();
    let empty = Image::capture(&path).unwrap();
    assert!(same(&missing, &empty).unwrap());
    assert!(Image::capture(root.path()).is_err());
    let bad = Image::Retained {
        path: path.clone(),
        identity: Identity {
            bytes: 1,
            sha256: vec![0; 32],
        },
    };
    assert!(bad.reader().is_err());
    let bad = Image::Retained {
        path,
        identity: Identity {
            bytes: 0,
            sha256: vec![0; 31],
        },
    };
    assert!(bad.reader().is_err());
}

#[test]
fn large_image_capture_does_not_use_the_old_library_byte_cutoff() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("large");
    let mut file = fs::File::create(&path).unwrap();
    let chunk = vec![b'x'; 1024 * 1024];
    let mut hash = Sha256::new();
    for _ in 0..129 {
        file.write_all(&chunk).unwrap();
        hash.update(&chunk);
    }
    drop(file);
    let image = Image::capture(&path).unwrap();
    assert_eq!(
        image.identity(),
        Identity {
            bytes: 129 * 1024 * 1024,
            sha256: hash.finalize().to_vec()
        }
    );
    let mut count = 0;
    let mut buffer = vec![0; 64 * 1024];
    let mut reader = image.reader().unwrap();
    loop {
        let n = reader.read(&mut buffer).unwrap();
        if n == 0 {
            break;
        }
        count += n as u64;
    }
    assert_eq!(count, 129 * 1024 * 1024);
}
