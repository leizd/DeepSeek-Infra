//! Bounded recognition of document keys and validated skipping of unused values.
//! Only a media record (or an explicitly captured publication) becomes a Value.
use super::{RawRecords, raw::Capture};
use serde_json::Value;
use std::{
    cell::RefCell,
    io::{self, BufRead, BufReader, Read, Write},
    rc::Rc,
};
use tonic::Status;

const BUFFER: usize = 64 * 1024;

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
            }
        }
        result
    }
}

struct Input<R> {
    source: BufReader<Probe<R>>,
    capture: Option<Capture>,
    capture_value: bool,
}

impl<R: Read> Input<R> {
    fn peek(&mut self) -> Result<Option<u8>, Error> {
        self.source
            .fill_buf()
            .map(|b| b.first().copied())
            .map_err(|_| Error::SourceIo)
    }
    fn next_byte(&mut self) -> Result<Option<u8>, Error> {
        let byte = self.peek()?;
        if let Some(byte) = byte {
            self.source.consume(1);
            if self.capture_value {
                self.capture_bytes(&[byte])?;
            }
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
    fn capture_bytes(&mut self, bytes: &[u8]) -> Result<(), Error> {
        self.capture
            .as_mut()
            .ok_or_else(|| Error::Storage(super::invalid()))?
            .write_all(bytes)
            .map_err(|_| Error::Storage(super::invalid()))
    }
}

impl<R: Read> Read for Input<R> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        self.source.read(bytes)
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Key {
    Media,
    Publication,
    Field(&'static str),
    Other,
}

const KEYS: [&str; 13] = [
    "media",
    "nativePublication",
    "mediaId",
    "projectId",
    "type",
    "title",
    "mimeType",
    "path",
    "source",
    "status",
    "createdAt",
    "updatedAt",
    "metadata",
];

struct KeyMatch {
    length: usize,
    candidates: u16,
}
impl KeyMatch {
    fn new() -> Self {
        Self {
            length: 0,
            candidates: (1 << KEYS.len()) - 1,
        }
    }
    fn character(&mut self, value: u32) {
        if self.candidates == 0 {
            return;
        }
        for (index, name) in KEYS.iter().enumerate() {
            if name
                .as_bytes()
                .get(self.length)
                .is_none_or(|byte| u32::from(*byte) != value)
            {
                self.candidates &= !(1 << index);
            }
        }
        self.length += 1;
    }
    fn result(self) -> Key {
        for (index, name) in KEYS.iter().enumerate() {
            if self.candidates & (1 << index) != 0 && self.length == name.len() {
                return match index {
                    0 => Key::Media,
                    1 => Key::Publication,
                    _ => Key::Field(name),
                };
            }
        }
        Key::Other
    }
}

impl<R: Read> Input<R> {
    fn hex4(&mut self) -> Result<u32, Error> {
        let mut value = 0;
        for _ in 0..4 {
            let digit = match self.next_byte()? {
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
        self.expect(b'"')?;
        let mut key = KeyMatch::new();
        loop {
            let scalar = match self.next_byte()? {
                Some(b'"') => return Ok(key.result()),
                Some(b'\\') => match self.next_byte()? {
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
                            self.expect(b'\\')?;
                            self.expect(b'u')?;
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

    fn number(&mut self, mut accept: impl FnMut(u8)) -> Result<(), Error> {
        if self.peek()? == Some(b'-') {
            accept(self.next_byte()?.ok_or(Error::Syntax)?);
        }
        self.unsigned_number(&mut accept)
    }
    fn unsigned_number(&mut self, accept: &mut impl FnMut(u8)) -> Result<(), Error> {
        match self.peek()? {
            Some(b'0') => accept(self.next_byte()?.ok_or(Error::Syntax)?),
            Some(b'1'..=b'9') => self.number_digits(accept)?,
            _ => return Err(Error::Syntax),
        }
        if self.peek()? == Some(b'.') {
            accept(self.next_byte()?.ok_or(Error::Syntax)?);
            self.number_digits(accept)?;
        }
        if matches!(self.peek()?, Some(b'e' | b'E')) {
            accept(self.next_byte()?.ok_or(Error::Syntax)?);
            if matches!(self.peek()?, Some(b'+' | b'-')) {
                accept(self.next_byte()?.ok_or(Error::Syntax)?);
            }
            self.number_digits(accept)?;
        }
        Ok(())
    }
    fn number_digits(&mut self, accept: &mut impl FnMut(u8)) -> Result<(), Error> {
        if !matches!(self.peek()?, Some(b'0'..=b'9')) {
            return Err(Error::Syntax);
        }
        while matches!(self.peek()?, Some(b'0'..=b'9')) {
            accept(self.next_byte()?.ok_or(Error::Syntax)?);
        }
        Ok(())
    }
    fn literal(&mut self, word: &[u8]) -> Result<(), Error> {
        for byte in word {
            self.expect(*byte)?;
        }
        Ok(())
    }

    /// Original Python accepts non-finite tokens in discarded positions. Validate
    /// their spelling and delimiters without converting or allocating the scalar.
    fn skipped_atom(&mut self) -> Result<(), Error> {
        match self.space()? {
            Some(b'"') => {
                self.string()?;
            }
            Some(b'n') => self.literal(b"null")?,
            Some(b't') => self.literal(b"true")?,
            Some(b'f') => self.literal(b"false")?,
            Some(b'N') => self.literal(b"NaN")?,
            Some(b'I') => self.literal(b"Infinity")?,
            Some(b'-') => {
                self.next_byte()?;
                if self.peek()? == Some(b'I') {
                    self.literal(b"Infinity")?;
                } else {
                    self.unsigned_number(&mut |_| {})?;
                }
            }
            Some(b'0'..=b'9') => self.number(|_| {})?,
            _ => return Err(Error::Syntax),
        }
        Ok(())
    }
    /// Heap frames replace recursive descent; ignored bytes never become Values.
    fn skip(&mut self) -> Result<(), Error> {
        enum Frame {
            Value,
            ArrayStart,
            ArrayTail,
            ObjectStart,
            ObjectTail,
        }
        let mut frames = vec![Frame::Value];
        while let Some(frame) = frames.pop() {
            match frame {
                Frame::Value => match self.space()? {
                    Some(b'[') => {
                        self.next_byte()?;
                        frames.push(Frame::ArrayStart);
                    }
                    Some(b'{') => {
                        self.next_byte()?;
                        frames.push(Frame::ObjectStart);
                    }
                    _ => self.skipped_atom()?,
                },
                Frame::ArrayStart => {
                    if self.space()? == Some(b']') {
                        self.next_byte()?;
                    } else {
                        frames.push(Frame::ArrayTail);
                        frames.push(Frame::Value);
                    }
                }
                Frame::ArrayTail => match self.space()? {
                    Some(b']') => {
                        self.next_byte()?;
                    }
                    Some(b',') => {
                        self.next_byte()?;
                        frames.push(Frame::ArrayTail);
                        frames.push(Frame::Value);
                    }
                    _ => return Err(Error::Syntax),
                },
                Frame::ObjectStart | Frame::ObjectTail => {
                    let first = matches!(frame, Frame::ObjectStart);
                    if self.space()? == Some(b'}') {
                        self.next_byte()?;
                        continue;
                    }
                    if !first {
                        self.expect(b',')?;
                        self.space()?;
                    }
                    self.string()?;
                    self.space()?;
                    self.expect(b':')?;
                    frames.push(Frame::ObjectTail);
                    frames.push(Frame::Value);
                }
            }
        }
        Ok(())
    }
}

#[path = "media_json_deferred.rs"]
mod deferred;
#[path = "media_json_value.rs"]
mod ordinary_value;
pub(super) use ordinary_value::from_slice as decode_value_slice;

pub(super) fn decode_raw_record(bytes: &[u8]) -> Result<Value, Error> {
    if let Ok(value) = ordinary_value::from_slice(bytes) {
        return Ok(value);
    }
    let selected = deferred::select_winners(bytes).map_err(|_| Error::Syntax)?;
    ordinary_value::from_slice(&selected)
}

struct Parser<R> {
    input: Input<R>,
    records: RawRecords,
    capture_publication: bool,
    publication: Option<Value>,
}
impl<R: Read> Parser<R> {
    /// The original normalizer reads exactly these 11 fields. Unknown values are
    /// fully validated and discarded before materialization, including surrogates.
    fn record(&mut self) -> Result<(), Error> {
        self.input.expect(b'{')?;
        if self.input.capture.is_some() {
            return Err(Error::Storage(super::invalid()));
        }
        self.input.capture = Some(self.records.begin_capture().map_err(Error::Storage)?);
        self.input.capture_bytes(b"{")?;
        let mut retained = false;
        if self.input.space()? == Some(b'}') {
            self.input.next_byte()?;
        } else {
            loop {
                self.input.space()?;
                let key = self.input.string()?;
                self.input.space()?;
                self.input.expect(b':')?;
                if let Key::Field(name) = key {
                    if retained {
                        self.input.capture_bytes(b",")?;
                    }
                    self.input.capture_bytes(b"\"")?;
                    self.input.capture_bytes(name.as_bytes())?;
                    self.input.capture_bytes(b"\":")?;
                    self.input.capture_value = true;
                    self.input.skip()?;
                    self.input.capture_value = false;
                    retained = true;
                } else {
                    self.input.skip()?;
                }
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
        self.input.capture_bytes(b"}")?;
        let capture = self
            .input
            .capture
            .take()
            .ok_or_else(|| Error::Storage(super::invalid()))?;
        self.records.finish_capture(capture).map_err(Error::Storage)
    }
    fn media(&mut self) -> Result<(), Error> {
        self.records.clear().map_err(Error::Storage)?;
        if self.input.space()? != Some(b'[') {
            return self.input.skip();
        }
        self.input.next_byte()?;
        if self.input.space()? == Some(b']') {
            self.input.next_byte()?;
            return Ok(());
        }
        loop {
            if self.input.space()? == Some(b'{') {
                self.record()?;
            } else {
                self.input.skip()?;
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
        if self.input.space()? == Some(b'{') {
            self.input.next_byte()?;
            if self.input.space()? == Some(b'}') {
                self.input.next_byte()?;
            } else {
                loop {
                    self.input.space()?;
                    let key = self.input.string()?;
                    self.input.space()?;
                    self.input.expect(b':')?;
                    match key {
                        Key::Media => self.media()?,
                        Key::Publication if self.capture_publication => {
                            self.publication = Some(self.input.value()?)
                        }
                        _ => self.input.skip()?,
                    }
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
            self.input.skip()?;
        }
        if self.input.space()?.is_some() {
            return Err(Error::Syntax);
        }
        Ok(())
    }
}

pub(super) fn read(
    reader: impl Read,
    records: RawRecords,
    capture_publication: bool,
) -> Result<(RawRecords, Option<Value>), Error> {
    read_mode(reader, records, capture_publication)
}

pub(super) fn read_value(
    reader: impl Read,
    records: RawRecords,
    capture_publication: bool,
) -> Result<(RawRecords, Option<Value>), Error> {
    read_mode(reader, records, capture_publication)
}

fn read_mode(
    reader: impl Read,
    records: RawRecords,
    capture_publication: bool,
) -> Result<(RawRecords, Option<Value>), Error> {
    let state = Rc::new(RefCell::new(InputState::default()));
    let source = Probe {
        source: reader,
        state: Rc::clone(&state),
    };
    let mut parser = Parser {
        input: Input {
            source: BufReader::with_capacity(BUFFER, source),
            capture: None,
            capture_value: false,
        },
        records,
        capture_publication,
        publication: None,
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
    result?;
    Ok((parser.records, parser.publication))
}

#[cfg(test)]
#[path = "media_json_document_tests.rs"]
mod tests;
