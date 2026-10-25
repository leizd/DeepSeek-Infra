use std::io;

pub(super) fn new_instance_id() -> io::Result<String> {
    with_entropy(|bytes| {
        getrandom::getrandom(bytes).map_err(|error| {
            io::Error::other(format!("instance identity entropy unavailable: {error}"))
        })
    })
}

fn with_entropy(fill: impl FnOnce(&mut [u8; 16]) -> io::Result<()>) -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    fill(&mut bytes)?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(format!(
        "{:08x}-{:04x}-{:04x}-{:04x}-{:012x}",
        u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]),
        u16::from_be_bytes([bytes[4], bytes[5]]),
        u16::from_be_bytes([bytes[6], bytes[7]]),
        u16::from_be_bytes([bytes[8], bytes[9]]),
        u64::from_be_bytes([
            0, 0, bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
        ]),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uuid_v4_preserves_entropy_except_required_version_and_variant_bits() {
        for (seed, expected) in [
            (
                [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
                "00010203-0405-4607-8809-0a0b0c0d0e0f",
            ),
            ([0_u8; 16], "00000000-0000-4000-8000-000000000000"),
            ([255_u8; 16], "ffffffff-ffff-4fff-bfff-ffffffffffff"),
        ] {
            let actual = with_entropy(|bytes| {
                *bytes = seed;
                Ok(())
            })
            .unwrap();
            assert_eq!(actual, expected);
        }
    }

    #[test]
    fn unavailable_entropy_never_returns_a_fallback_identity() {
        let error = with_entropy(|_| {
            Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "entropy test refusal",
            ))
        })
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(error.to_string(), "entropy test refusal");
    }
}
