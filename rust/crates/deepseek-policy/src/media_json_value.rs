//! Ordinary JSON Value construction, without serde's private-tag map protocol.
//! Explicit heap frames also avoid a decoder recursion limit for retained rows.
use super::{BUFFER, Error, Input, InputState, Probe};
use serde_json::{Map, Number, Value};
use std::{
    cell::RefCell,
    io::{BufReader, Read},
    rc::Rc,
    str::FromStr,
};

enum Frame {
    Array(Vec<Value>),
    Object(Map<String, Value>, String),
}

impl<R: Read> Input<R> {
    fn decoded_string(&mut self) -> Result<String, Error> {
        let mut bytes = Vec::new();
        self.string_with(false, |part| {
            bytes.extend_from_slice(part);
            Ok(())
        })?;
        String::from_utf8(bytes).map_err(|_| Error::Syntax)
    }

    pub(super) fn value(&mut self) -> Result<Value, Error> {
        let mut frames = Vec::new();
        loop {
            let value = match self.space()? {
                Some(b'"') => Some(Value::String(self.decoded_string()?)),
                Some(b'n') => {
                    self.literal(b"null")?;
                    Some(Value::Null)
                }
                Some(b't') => {
                    self.literal(b"true")?;
                    Some(Value::Bool(true))
                }
                Some(b'f') => {
                    self.literal(b"false")?;
                    Some(Value::Bool(false))
                }
                Some(b'-' | b'0'..=b'9') => {
                    let mut bytes = Vec::new();
                    self.number(|byte| bytes.push(byte))?;
                    let text = std::str::from_utf8(&bytes).map_err(|_| Error::Syntax)?;
                    Some(Value::Number(
                        Number::from_str(text).map_err(|_| Error::Syntax)?,
                    ))
                }
                Some(b'[') => {
                    self.next_byte()?;
                    if self.space()? == Some(b']') {
                        self.next_byte()?;
                        Some(Value::Array(Vec::new()))
                    } else {
                        frames.push(Frame::Array(Vec::new()));
                        None
                    }
                }
                Some(b'{') => {
                    self.next_byte()?;
                    if self.space()? == Some(b'}') {
                        self.next_byte()?;
                        Some(Value::Object(Map::new()))
                    } else {
                        let key = self.decoded_string()?;
                        self.space()?;
                        self.expect(b':')?;
                        frames.push(Frame::Object(Map::new(), key));
                        None
                    }
                }
                _ => return Err(Error::Syntax),
            };
            let Some(mut value) = value else {
                continue;
            };
            loop {
                let Some(frame) = frames.last_mut() else {
                    return Ok(value);
                };
                let close = match frame {
                    Frame::Array(values) => {
                        values.push(value);
                        b']'
                    }
                    Frame::Object(fields, key) => {
                        fields.insert(std::mem::take(key), value);
                        b'}'
                    }
                };
                match self.space()? {
                    Some(byte) if byte == close => {
                        self.next_byte()?;
                        value = match frames.pop().ok_or(Error::Syntax)? {
                            Frame::Array(values) => Value::Array(values),
                            Frame::Object(fields, _) => Value::Object(fields),
                        };
                    }
                    Some(b',') => {
                        self.next_byte()?;
                        if let Some(Frame::Object(_, key)) = frames.last_mut() {
                            self.space()?;
                            // Avoid overlapping mutable borrows of the input and frame.
                            let next_key = self.decoded_string()?;
                            self.space()?;
                            self.expect(b':')?;
                            *key = next_key;
                        }
                        break;
                    }
                    _ => return Err(Error::Syntax),
                }
            }
        }
    }
}

/// The digest-checked private record spool uses the same ordinary map semantics.
/// Complete UTF-8 and EOF remain mandatory before accepting the stored value.
pub(crate) fn from_slice(bytes: &[u8]) -> Result<Value, Error> {
    let state = Rc::new(RefCell::new(InputState::default()));
    let mut input = Input {
        source: BufReader::with_capacity(
            BUFFER,
            Probe {
                source: bytes,
                state: Rc::clone(&state),
            },
        ),
        returned: None,
    };
    let value = input.value()?;
    if input.space()?.is_some() {
        return Err(Error::Syntax);
    }
    if state.borrow().utf8.invalid {
        return Err(Error::InvalidUtf8);
    }
    Ok(value)
}
