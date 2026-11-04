//! Bounded recognition of document keys and validated skipping of unused values.
//! Only a media record (or an explicitly captured publication) becomes a Value.
use super::Records;
use crate::media_stream_io::{DigestReader, DigestWriter};
use serde::{
    Deserialize,
    de::{self, DeserializeOwned, Visitor},
};
use serde_json::Value;
use std::{
    cell::RefCell,
    fmt,
    io::{self, BufRead, BufReader, BufWriter, Read, Seek, SeekFrom, Write},
    rc::Rc,
    sync::OnceLock,
};
use tonic::Status;

const BUFFER: usize = 64 * 1024;
const DEPTH: usize = 128;

#[derive(Debug)]
pub(crate) enum Error {
    Syntax,
    SourceIo,
    InvalidUtf8,
    Storage(Status),
}

#[derive(Default)]
struct InputState {
    source_io: bool,
    utf8: Utf8,
    in_string: bool,
    escaped: bool,
    depth: usize,
    excessive_depth: bool,
}

#[derive(Default)]
struct Utf8 {
    tail: [u8; 4],
    length: usize,
    invalid: bool,
}

impl Utf8 {
    fn accept(&mut self, mut bytes: &[u8]) {
        if self.invalid {
            return;
        }
        if self.length != 0 {
            let width = match self.tail[0] {
                0..=0xdf => 2,
                0xe0..=0xef => 3,
                _ => 4,
            };
            let count = (width - self.length).min(bytes.len());
            self.tail[self.length..self.length + count].copy_from_slice(&bytes[..count]);
            self.length += count;
            bytes = &bytes[count..];
            if self.length < width {
                return;
            }
            if std::str::from_utf8(&self.tail[..width]).is_err() {
                self.invalid = true;
                return;
            }
            self.length = 0;
        }
        if let Err(error) = std::str::from_utf8(bytes) {
            if error.error_len().is_some() {
                self.invalid = true;
            } else {
                let tail = &bytes[error.valid_up_to()..];
                self.tail[..tail.len()].copy_from_slice(tail);
                self.length = tail.len();
            }
        }
    }
}

struct Probe<R> {
    source: R,
    state: Rc<RefCell<InputState>>,
}

impl<R: Read> Read for Probe<R> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        let result = loop {
            match self.source.read(bytes) {
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                result => break result,
            }
        };
        let mut state = self.state.borrow_mut();
        match &result {
            Err(_) => state.source_io = true,
            Ok(0) if !bytes.is_empty() => {
                if state.utf8.length != 0 {
                    state.utf8.invalid = true;
                }
            }
            Ok(count) => {
                state.utf8.accept(&bytes[..*count]);
                if !state.excessive_depth {
                    for byte in &bytes[..*count] {
                        if state.in_string {
                            if state.escaped {
                                state.escaped = false;
                            } else if *byte == b'\\' {
                                state.escaped = true;
                            } else if *byte == b'"' {
                                state.in_string = false;
                            }
                        } else {
                            match byte {
                                b'"' => state.in_string = true,
                                b'[' | b'{' => {
                                    state.depth += 1;
                                    if state.depth >= DEPTH {
                                        state.excessive_depth = true;
                                        break;
                                    }
                                }
                                b']' | b'}' => state.depth = state.depth.saturating_sub(1),
                                _ => {}
                            }
                        }
                    }
                }
            }
        }
        result
    }
}

struct Input<R> {
    source: BufReader<Probe<R>>,
    returned: Option<u8>,
}

impl<R: Read> Input<R> {
    fn peek(&mut self) -> Result<Option<u8>, Error> {
        if self.returned.is_some() {
            return Ok(self.returned);
        }
        self.source
            .fill_buf()
            .map(|b| b.first().copied())
            .map_err(|_| Error::SourceIo)
    }
    fn next_byte(&mut self) -> Result<Option<u8>, Error> {
        if let Some(byte) = self.returned.take() {
            return Ok(Some(byte));
        }
        let byte = self.peek()?;
        if byte.is_some() {
            self.source.consume(1);
        }
        Ok(byte)
    }
    fn space(&mut self) -> Result<Option<u8>, Error> {
        while matches!(self.peek()?, Some(b' ' | b'\n' | b'\r' | b'\t')) {
            self.next_byte()?;
        }
        self.peek()
    }
    fn expect(&mut self, byte: u8) -> Result<(), Error> {
        if self.next_byte()? == Some(byte) {
            Ok(())
        } else {
            Err(Error::Syntax)
        }
    }
    fn restore(&mut self, byte: u8) -> Result<(), Error> {
        if self.returned.is_some() {
            return Err(Error::Storage(super::invalid()));
        }
        self.returned = Some(byte);
        Ok(())
    }
}

impl<R: Read> Read for Input<R> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        if let Some(byte) = self.returned.take() {
            bytes[0] = byte;
            return Ok(1);
        }
        self.source.read(bytes)
    }
}

struct Counted<'a, R> {
    input: &'a mut Input<R>,
    bytes: usize,
    last: u8,
}
impl<R: Read> Read for Counted<'_, R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let count = self.input.read(buffer)?;
        self.bytes = self
            .bytes
            .checked_add(count)
            .ok_or_else(|| io::Error::other("media JSON cursor overflow"))?;
        if count != 0 {
            self.last = buffer[count - 1];
        }
        Ok(count)
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Key {
    Media,
    Publication,
    Other,
    Number,
    RawValue,
}

#[derive(Default)]
struct KeyMatch {
    length: usize,
    media: bool,
    publication: bool,
    number: bool,
    raw_value: bool,
}
impl KeyMatch {
    fn new() -> Self {
        Self {
            length: 0,
            media: true,
            publication: true,
            number: true,
            raw_value: true,
        }
    }
    fn character(&mut self, value: u32) {
        if !self.media && !self.publication && !self.number && !self.raw_value {
            return;
        }
        self.media &= b"media"
            .get(self.length)
            .is_some_and(|b| u32::from(*b) == value);
        self.publication &= b"nativePublication"
            .get(self.length)
            .is_some_and(|b| u32::from(*b) == value);
        self.number &= b"$serde_json::private::Number"
            .get(self.length)
            .is_some_and(|b| u32::from(*b) == value);
        self.raw_value &= b"$serde_json::private::RawValue"
            .get(self.length)
            .is_some_and(|b| u32::from(*b) == value);
        // Recognition storage is bounded; arbitrarily long keys remain accepted.
        self.length = (self.length + 1).min(b"$serde_json::private::RawValue".len() + 1);
    }
    fn result(self) -> Key {
        if self.media && self.length == 5 {
            Key::Media
        } else if self.publication && self.length == 17 {
            Key::Publication
        } else if self.number && self.length == b"$serde_json::private::Number".len() {
            Key::Number
        } else if self.raw_value && self.length == b"$serde_json::private::RawValue".len() {
            Key::RawValue
        } else {
            Key::Other
        }
    }
}

fn arbitrary_numbers() -> bool {
    static ARBITRARY: OnceLock<bool> = OnceLock::new();
    *ARBITRARY
        .get_or_init(|| matches!(serde_json::from_str::<Value>("1e400"), Ok(Value::Number(_))))
}

fn raw_values() -> bool {
    static RAW: OnceLock<bool> = OnceLock::new();
    *RAW.get_or_init(|| {
        matches!(
            serde_json::from_str::<Value>(r#"{"$serde_json::private::RawValue":"null"}"#),
            Ok(Value::Null)
        )
    })
}

#[derive(Clone, Copy)]
enum NumericString {
    Start,
    Sign,
    Zero,
    Integer,
    Dot,
    Fraction,
    Exponent,
    ExponentSign,
    ExponentDigits,
    Invalid,
}
impl NumericString {
    fn accept(&mut self, bytes: &[u8]) {
        use NumericString::*;
        for byte in bytes {
            *self = match (*self, byte) {
                (Start, b'-') => Sign,
                (Start | Sign, b'0') => Zero,
                (Start | Sign, b'1'..=b'9') | (Integer, b'0'..=b'9') => Integer,
                (Zero | Integer, b'.') => Dot,
                (Dot | Fraction, b'0'..=b'9') => Fraction,
                (Zero | Integer | Fraction, b'e' | b'E') => Exponent,
                (Exponent, b'+' | b'-') => ExponentSign,
                (Exponent | ExponentSign | ExponentDigits, b'0'..=b'9') => ExponentDigits,
                _ => Invalid,
            };
        }
    }
    fn finish(self) -> Result<(), Error> {
        if matches!(
            self,
            Self::Zero | Self::Integer | Self::Fraction | Self::ExponentDigits
        ) {
            Ok(())
        } else {
            Err(Error::Syntax)
        }
    }
}

struct SealedReader {
    source: Option<DigestReader<std::fs::File>>,
    expected_size: u64,
    expected_hash: Vec<u8>,
}
impl Read for SealedReader {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        let Some(source) = self.source.as_mut() else {
            return Ok(0);
        };
        let count = source.read(buffer)?;
        if count == 0 {
            let source = self
                .source
                .take()
                .ok_or_else(|| io::Error::other("media JSON source already closed"))?;
            let (size, hash) = source.finish();
            if size != self.expected_size || hash != self.expected_hash {
                return Err(io::Error::other("media JSON temporary source changed"));
            }
        }
        Ok(count)
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Discard,
    Document,
    MediaArray,
}

struct Parser<R> {
    input: Input<R>,
    records: Records,
    capture_publication: bool,
    publication: Option<Value>,
    value_semantics: bool,
    mode: Mode,
}

impl<R: Read> Parser<R> {
    fn value<T: DeserializeOwned>(&mut self) -> Result<T, Error> {
        let mut counted = Counted {
            input: &mut self.input,
            bytes: 0,
            last: 0,
        };
        let (result, offset) = {
            let mut values = serde_json::Deserializer::from_reader(&mut counted).into_iter::<T>();
            let value = values.next().ok_or(Error::Syntax)?;
            (value, values.byte_offset())
        };
        let value = result.map_err(|error| {
            if error.is_io() {
                if counted.input.source.get_ref().state.borrow().source_io {
                    Error::SourceIo
                } else {
                    Error::Storage(super::invalid())
                }
            } else {
                Error::Syntax
            }
        })?;
        match counted.bytes.checked_sub(offset) {
            Some(0) => {}
            Some(1) => counted.input.restore(counted.last)?,
            _ => return Err(Error::Storage(super::invalid())),
        }
        Ok(value)
    }

    fn hex4(&mut self) -> Result<u32, Error> {
        let mut value = 0;
        for _ in 0..4 {
            let digit = match self.input.next_byte()? {
                Some(b @ b'0'..=b'9') => u32::from(b - b'0'),
                Some(b @ b'a'..=b'f') => u32::from(b - b'a' + 10),
                Some(b @ b'A'..=b'F') => u32::from(b - b'A' + 10),
                _ => return Err(Error::Syntax),
            };
            value = value * 16 + digit;
        }
        Ok(value)
    }

    fn string(&mut self) -> Result<Key, Error> {
        self.string_with(true, |_| Ok(()))
    }

    fn string_with(
        &mut self,
        discarded: bool,
        mut accept: impl FnMut(&[u8]) -> Result<(), Error>,
    ) -> Result<Key, Error> {
        self.input.expect(b'"')?;
        let mut key = KeyMatch::new();
        loop {
            let scalar = match self.input.next_byte()? {
                Some(b'"') => return Ok(key.result()),
                Some(b'\\') => match self.input.next_byte()? {
                    Some(b @ (b'"' | b'\\' | b'/')) => u32::from(b),
                    Some(b'b') => 8,
                    Some(b'f') => 12,
                    Some(b'n') => 10,
                    Some(b'r') => 13,
                    Some(b't') => 9,
                    Some(b'u') => {
                        let high = self.hex4()?;
                        // Python accepts any escaped UTF-16 code unit. Discarded
                        // strings do not need a UTF-8 representation; a surrogate
                        // cannot match any of the ASCII document field names.
                        if discarded && (0xd800..=0xdfff).contains(&high) {
                            key.character(high);
                            continue;
                        }
                        if (0xd800..=0xdbff).contains(&high) {
                            self.input.expect(b'\\')?;
                            self.input.expect(b'u')?;
                            let low = self.hex4()?;
                            if !(0xdc00..=0xdfff).contains(&low) {
                                return Err(Error::Syntax);
                            }
                            0x10000 + ((high - 0xd800) << 10) + low - 0xdc00
                        } else if (0xdc00..=0xdfff).contains(&high) {
                            return Err(Error::Syntax);
                        } else {
                            high
                        }
                    }
                    _ => return Err(Error::Syntax),
                },
                Some(0..=31) | None => return Err(Error::Syntax),
                Some(byte) => {
                    key.character(u32::from(byte));
                    accept(&[byte])?;
                    continue;
                }
            };
            let mut bytes = [0; 4];
            let encoded = char::from_u32(scalar)
                .ok_or(Error::Syntax)?
                .encode_utf8(&mut bytes)
                .as_bytes();
            for byte in encoded {
                key.character(u32::from(*byte));
            }
            accept(encoded)?;
        }
    }

    fn numeric_string(&mut self) -> Result<(), Error> {
        self.input.space()?;
        let mut numeric = NumericString::Start;
        self.string_with(false, |bytes| {
            numeric.accept(bytes);
            Ok(())
        })?;
        numeric.finish()
    }

    fn decoded_raw(&mut self, mode: Mode) -> Result<(), Error> {
        self.input.space()?;
        let mut file =
            tempfile::NamedTempFile::new().map_err(|_| Error::Storage(super::invalid()))?;
        let (size, hash) = {
            let mut output =
                BufWriter::with_capacity(BUFFER, DigestWriter::new(file.as_file_mut()));
            self.string_with(false, |bytes| {
                output
                    .write_all(bytes)
                    .map_err(|_| Error::Storage(super::invalid()))
            })?;
            output
                .flush()
                .map_err(|_| Error::Storage(super::invalid()))?;
            output
                .into_inner()
                .map_err(|_| Error::Storage(super::invalid()))?
                .finish()
        };
        file.as_file_mut()
            .seek(SeekFrom::Start(0))
            .map_err(|_| Error::Storage(super::invalid()))?;
        let source = SealedReader {
            source: Some(DigestReader::new(
                file.as_file()
                    .try_clone()
                    .map_err(|_| Error::Storage(super::invalid()))?,
            )),
            expected_size: size,
            expected_hash: hash,
        };
        let records = Records::empty().map_err(Error::Storage)?;
        let (records, publication) =
            match read_mode(source, records, self.capture_publication, true, mode) {
                Ok(value) => value,
                Err(Error::SourceIo) => return Err(Error::Storage(super::invalid())),
                Err(Error::InvalidUtf8 | Error::Syntax) => return Err(Error::Syntax),
                Err(Error::Storage(error)) => return Err(Error::Storage(error)),
            };
        if mode != Mode::Discard {
            self.records = records;
            self.publication = publication;
        }
        drop(file);
        Ok(())
    }

    fn digits(&mut self) -> Result<(), Error> {
        if !matches!(self.input.peek()?, Some(b'0'..=b'9')) {
            return Err(Error::Syntax);
        }
        while matches!(self.input.peek()?, Some(b'0'..=b'9')) {
            self.input.next_byte()?;
        }
        Ok(())
    }

    fn number(&mut self) -> Result<(), Error> {
        let arbitrary = arbitrary_numbers();
        if !arbitrary {
            return self.value::<NumberOnly>().map(|_| ());
        }
        // With the caller's existing arbitrary-precision feature, every JSON
        // number spelling is retained by Value; no scalar buffer is necessary.
        if self.input.peek()? == Some(b'-') {
            self.input.next_byte()?;
        }
        match self.input.peek()? {
            Some(b'0') => {
                self.input.next_byte()?;
            }
            Some(b'1'..=b'9') => self.digits()?,
            _ => return Err(Error::Syntax),
        }
        if self.input.peek()? == Some(b'.') {
            self.input.next_byte()?;
            self.digits()?;
        }
        if matches!(self.input.peek()?, Some(b'e' | b'E')) {
            self.input.next_byte()?;
            if matches!(self.input.peek()?, Some(b'+' | b'-')) {
                self.input.next_byte()?;
            }
            self.digits()?;
        }
        Ok(())
    }

    fn skip(&mut self, parents: usize) -> Result<(), Error> {
        self.skip_mode(parents, Mode::Discard)
    }

    fn skip_mode(&mut self, parents: usize, mode: Mode) -> Result<(), Error> {
        match self.input.space()? {
            Some(b'"') => {
                self.string()?;
            }
            Some(b'-' | b'0'..=b'9') => self.number()?,
            Some(first @ (b'n' | b't' | b'f')) => {
                let word: &[u8] = match first {
                    b'n' => b"null",
                    b't' => b"true",
                    _ => b"false",
                };
                for byte in word {
                    self.input.expect(*byte)?;
                }
            }
            Some(first @ (b'[' | b'{')) => {
                if parents + 1 >= DEPTH {
                    return Err(Error::Syntax);
                }
                self.input.next_byte()?;
                let close = if first == b'[' { b']' } else { b'}' };
                if self.input.space()? == Some(close) {
                    self.input.next_byte()?;
                    return Ok(());
                }
                let mut first_member = true;
                loop {
                    if first == b'{' {
                        self.input.space()?;
                        let key = self.string()?;
                        self.input.space()?;
                        self.input.expect(b':')?;
                        if self.value_semantics && first_member {
                            if key == Key::Number && arbitrary_numbers() {
                                self.numeric_string()?;
                                self.input.space()?;
                                self.input.expect(b'}')?;
                                return Ok(());
                            }
                            if key == Key::RawValue && raw_values() {
                                self.decoded_raw(mode)?;
                                self.input.space()?;
                                self.input.expect(b'}')?;
                                return Ok(());
                            }
                        }
                    }
                    self.skip(parents + 1)?;
                    first_member = false;
                    match self.input.space()? {
                        Some(byte) if byte == close => {
                            self.input.next_byte()?;
                            break;
                        }
                        Some(b',') => {
                            self.input.next_byte()?;
                        }
                        _ => return Err(Error::Syntax),
                    }
                }
            }
            _ => return Err(Error::Syntax),
        }
        Ok(())
    }

    fn media(&mut self, parents: usize) -> Result<(), Error> {
        self.records.clear().map_err(Error::Storage)?;
        if self.input.space()? != Some(b'[') {
            return self.skip_mode(parents, Mode::MediaArray);
        }
        self.input.next_byte()?;
        if self.input.space()? == Some(b']') {
            self.input.next_byte()?;
            return Ok(());
        }
        loop {
            if self.input.space()? == Some(b'{') {
                let value = self.value::<Value>()?;
                if value.is_object() {
                    self.records.append_raw(&value).map_err(Error::Storage)?;
                }
            } else {
                self.skip(parents + 1)?;
            }
            match self.input.space()? {
                Some(b']') => {
                    self.input.next_byte()?;
                    break;
                }
                Some(b',') => {
                    self.input.next_byte()?;
                }
                _ => return Err(Error::Syntax),
            }
        }
        Ok(())
    }

    fn document(&mut self) -> Result<(), Error> {
        if self.mode == Mode::MediaArray {
            self.media(0)?;
        } else if self.mode == Mode::Discard {
            self.skip(0)?;
        } else if self.input.space()? == Some(b'{') {
            self.input.next_byte()?;
            if self.input.space()? == Some(b'}') {
                self.input.next_byte()?;
            } else {
                let mut first_member = true;
                loop {
                    self.input.space()?;
                    let key = self.string()?;
                    self.input.space()?;
                    self.input.expect(b':')?;
                    if self.value_semantics
                        && first_member
                        && key == Key::Number
                        && arbitrary_numbers()
                    {
                        self.numeric_string()?;
                        self.input.space()?;
                        self.input.expect(b'}')?;
                        break;
                    }
                    if self.value_semantics && first_member && key == Key::RawValue && raw_values()
                    {
                        self.decoded_raw(Mode::Document)?;
                        self.input.space()?;
                        self.input.expect(b'}')?;
                        break;
                    }
                    match key {
                        Key::Media => self.media(1)?,
                        Key::Publication if self.capture_publication => {
                            self.publication = Some(self.value()?)
                        }
                        _ => self.skip(1)?,
                    }
                    first_member = false;
                    match self.input.space()? {
                        Some(b'}') => {
                            self.input.next_byte()?;
                            break;
                        }
                        Some(b',') => {
                            self.input.next_byte()?;
                        }
                        _ => return Err(Error::Syntax),
                    }
                }
            }
        } else {
            self.skip(0)?;
        }
        if self.input.space()?.is_some() {
            return Err(Error::Syntax);
        }
        Ok(())
    }
}

struct NumberOnly;
impl<'de> Deserialize<'de> for NumberOnly {
    fn deserialize<D: de::Deserializer<'de>>(de: D) -> Result<Self, D::Error> {
        struct Numeric;
        impl Visitor<'_> for Numeric {
            type Value = NumberOnly;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a JSON number")
            }
            fn visit_i64<E: de::Error>(self, _: i64) -> Result<NumberOnly, E> {
                Ok(NumberOnly)
            }
            fn visit_u64<E: de::Error>(self, _: u64) -> Result<NumberOnly, E> {
                Ok(NumberOnly)
            }
            fn visit_f64<E: de::Error>(self, _: f64) -> Result<NumberOnly, E> {
                Ok(NumberOnly)
            }
        }
        de.deserialize_any(Numeric)
    }
}

pub(super) fn read(
    reader: impl Read,
    records: Records,
    capture_publication: bool,
) -> Result<(Records, Option<Value>), Error> {
    read_mode(reader, records, capture_publication, false, Mode::Document)
}

pub(super) fn read_value(
    reader: impl Read,
    records: Records,
    capture_publication: bool,
) -> Result<(Records, Option<Value>), Error> {
    read_mode(reader, records, capture_publication, true, Mode::Document)
}

fn read_mode(
    reader: impl Read,
    records: Records,
    capture_publication: bool,
    value_semantics: bool,
    mode: Mode,
) -> Result<(Records, Option<Value>), Error> {
    let state = Rc::new(RefCell::new(InputState::default()));
    let source = Probe {
        source: reader,
        state: Rc::clone(&state),
    };
    let mut parser = Parser {
        input: Input {
            source: BufReader::with_capacity(BUFFER, source),
            returned: None,
        },
        records,
        capture_publication,
        publication: None,
        value_semantics,
        mode,
    };
    let result = parser.document();
    // Private spool failures retain priority over source/UTF8 fallbacks.
    if let Err(Error::Storage(error)) = result {
        return Err(Error::Storage(error));
    }
    if matches!(result, Err(Error::Syntax)) {
        // Preserve complete source-read/UTF8 precedence even after early syntax.
        let _ = io::copy(&mut parser.input, &mut io::sink());
    }
    let state = state.borrow();
    if state.source_io {
        return Err(Error::SourceIo);
    }
    if state.utf8.invalid {
        return Err(Error::InvalidUtf8);
    }
    if state.excessive_depth {
        return Err(Error::Syntax);
    }
    result?;
    Ok((parser.records, parser.publication))
}

#[cfg(test)]
#[path = "media_json_document_tests.rs"]
mod tests;
