//! Select dictionary winners before interpreting retained scalar representations.
//! Raw leaf spans keep escaped UTF-16 units and non-finite numbers lossless here.
use std::{collections::HashMap, ops::Range};

enum Node {
    Leaf(Range<usize>),
    Array(Vec<usize>),
    Object(Vec<(Range<usize>, usize)>),
}

struct Object {
    fields: Vec<(Range<usize>, usize)>,
    positions: HashMap<Vec<u16>, usize>,
    key: (Range<usize>, Vec<u16>),
}

enum Frame {
    Array(Vec<usize>),
    Object(Object),
}

struct Cursor<'a> {
    bytes: &'a [u8],
    text: &'a str,
    at: usize,
}

impl Cursor<'_> {
    fn space(&mut self) -> Option<u8> {
        while matches!(self.bytes.get(self.at), Some(b' ' | b'\n' | b'\r' | b'\t')) {
            self.at += 1;
        }
        self.bytes.get(self.at).copied()
    }

    fn expect(&mut self, byte: u8) -> Result<(), ()> {
        if self.bytes.get(self.at) != Some(&byte) {
            return Err(());
        }
        self.at += 1;
        Ok(())
    }

    fn string(&mut self, decode_key: bool) -> Result<(Range<usize>, Vec<u16>), ()> {
        let start = self.at;
        self.expect(b'"')?;
        let mut key = Vec::new();
        loop {
            let byte = *self.bytes.get(self.at).ok_or(())?;
            self.at += 1;
            match byte {
                b'"' => return Ok((start..self.at, key)),
                0..=31 => return Err(()),
                b'\\' => {
                    let escape = *self.bytes.get(self.at).ok_or(())?;
                    self.at += 1;
                    let unit = match escape {
                        b'"' | b'\\' | b'/' => u16::from(escape),
                        b'b' => 8,
                        b'f' => 12,
                        b'n' => 10,
                        b'r' => 13,
                        b't' => 9,
                        b'u' => {
                            let mut unit = 0;
                            for _ in 0..4 {
                                let digit = *self.bytes.get(self.at).ok_or(())?;
                                self.at += 1;
                                unit = unit * 16
                                    + match digit {
                                        b'0'..=b'9' => u16::from(digit - b'0'),
                                        b'a'..=b'f' => u16::from(digit - b'a' + 10),
                                        b'A'..=b'F' => u16::from(digit - b'A' + 10),
                                        _ => return Err(()),
                                    };
                            }
                            unit
                        }
                        _ => return Err(()),
                    };
                    if decode_key {
                        key.push(unit);
                    }
                }
                _ if decode_key => {
                    // UTF-8 was checked once over the complete private record.
                    let tail = self.text.get(self.at - 1..).ok_or(())?;
                    let scalar = tail.chars().next().ok_or(())?;
                    let mut units = [0; 2];
                    key.extend_from_slice(scalar.encode_utf16(&mut units));
                    self.at += scalar.len_utf8() - 1;
                }
                _ => {}
            }
        }
    }

    fn digits(&mut self) -> Result<(), ()> {
        let start = self.at;
        while matches!(self.bytes.get(self.at), Some(b'0'..=b'9')) {
            self.at += 1;
        }
        if start == self.at { Err(()) } else { Ok(()) }
    }

    fn atom(&mut self) -> Result<Range<usize>, ()> {
        let start = self.at;
        match self.bytes.get(self.at).copied().ok_or(())? {
            b'"' => {
                self.string(false)?;
            }
            b'n' | b't' | b'f' | b'N' | b'I' => {
                let word: &[u8] = match self.bytes[self.at] {
                    b'n' => b"null",
                    b't' => b"true",
                    b'f' => b"false",
                    b'N' => b"NaN",
                    _ => b"Infinity",
                };
                for byte in word {
                    self.expect(*byte)?;
                }
            }
            b'-' if self.bytes[self.at..].starts_with(b"-Infinity") => {
                self.at += b"-Infinity".len();
            }
            b'-' | b'0'..=b'9' => {
                if self.bytes[self.at] == b'-' {
                    self.at += 1;
                }
                match self.bytes.get(self.at) {
                    Some(b'0') => self.at += 1,
                    Some(b'1'..=b'9') => self.digits()?,
                    _ => return Err(()),
                }
                if self.bytes.get(self.at) == Some(&b'.') {
                    self.at += 1;
                    self.digits()?;
                }
                if matches!(self.bytes.get(self.at), Some(b'e' | b'E')) {
                    self.at += 1;
                    if matches!(self.bytes.get(self.at), Some(b'+' | b'-')) {
                        self.at += 1;
                    }
                    self.digits()?;
                }
            }
            _ => return Err(()),
        }
        Ok(start..self.at)
    }
}

fn parse(bytes: &[u8]) -> Result<(Vec<Node>, usize), ()> {
    let text = std::str::from_utf8(bytes).map_err(|_| ())?;
    let mut cursor = Cursor { bytes, text, at: 0 };
    let mut nodes = Vec::new();
    let mut frames = Vec::new();
    loop {
        let node = match cursor.space().ok_or(())? {
            b'[' => {
                cursor.at += 1;
                if cursor.space() == Some(b']') {
                    cursor.at += 1;
                    Some(Node::Array(Vec::new()))
                } else {
                    frames.push(Frame::Array(Vec::new()));
                    None
                }
            }
            b'{' => {
                cursor.at += 1;
                if cursor.space() == Some(b'}') {
                    cursor.at += 1;
                    Some(Node::Object(Vec::new()))
                } else {
                    let key = cursor.string(true)?;
                    cursor.space();
                    cursor.expect(b':')?;
                    frames.push(Frame::Object(Object {
                        fields: Vec::new(),
                        positions: HashMap::new(),
                        key,
                    }));
                    None
                }
            }
            _ => Some(Node::Leaf(cursor.atom()?)),
        };
        let Some(node) = node else {
            continue;
        };
        let mut id = nodes.len();
        nodes.push(node);
        loop {
            let Some(frame) = frames.last_mut() else {
                if cursor.space().is_some() {
                    return Err(());
                }
                return Ok((nodes, id));
            };
            let close = match frame {
                Frame::Array(values) => {
                    values.push(id);
                    b']'
                }
                Frame::Object(object) => {
                    let span = object.key.0.clone();
                    let key = std::mem::take(&mut object.key.1);
                    if let Some(slot) = object.positions.get(&key) {
                        object.fields[*slot].1 = id;
                    } else {
                        object.positions.insert(key, object.fields.len());
                        object.fields.push((span, id));
                    }
                    b'}'
                }
            };
            match cursor.space() {
                Some(byte) if byte == close => {
                    cursor.at += 1;
                    let node = match frames.pop().ok_or(())? {
                        Frame::Array(values) => Node::Array(values),
                        Frame::Object(object) => Node::Object(object.fields),
                    };
                    id = nodes.len();
                    nodes.push(node);
                }
                Some(b',') => {
                    cursor.at += 1;
                    if let Some(Frame::Object(object)) = frames.last_mut() {
                        cursor.space();
                        object.key = cursor.string(true)?;
                        cursor.space();
                        cursor.expect(b':')?;
                    }
                    break;
                }
                _ => return Err(()),
            }
        }
    }
}

/// Emit only dictionary winners, keeping first insertion order and raw leaves.
pub(super) fn select_winners(bytes: &[u8]) -> Result<Vec<u8>, ()> {
    enum Emit {
        Node(usize),
        Array(usize, usize),
        Object(usize, usize),
    }
    let (nodes, root) = parse(bytes)?;
    let mut output = Vec::new();
    output.try_reserve_exact(bytes.len()).map_err(|_| ())?;
    let mut pending = vec![Emit::Node(root)];
    while let Some(item) = pending.pop() {
        match item {
            Emit::Node(id) => match &nodes[id] {
                Node::Leaf(span) => output.extend_from_slice(&bytes[span.clone()]),
                Node::Array(_) => {
                    output.push(b'[');
                    pending.push(Emit::Array(id, 0));
                }
                Node::Object(_) => {
                    output.push(b'{');
                    pending.push(Emit::Object(id, 0));
                }
            },
            Emit::Array(id, index) => {
                let Node::Array(values) = &nodes[id] else {
                    return Err(());
                };
                if let Some(value) = values.get(index) {
                    if index != 0 {
                        output.push(b',');
                    }
                    pending.push(Emit::Array(id, index + 1));
                    pending.push(Emit::Node(*value));
                } else {
                    output.push(b']');
                }
            }
            Emit::Object(id, index) => {
                let Node::Object(fields) = &nodes[id] else {
                    return Err(());
                };
                if let Some((key, value)) = fields.get(index) {
                    if index != 0 {
                        output.push(b',');
                    }
                    output.extend_from_slice(&bytes[key.clone()]);
                    output.push(b':');
                    pending.push(Emit::Object(id, index + 1));
                    pending.push(Emit::Node(*value));
                } else {
                    output.push(b'}');
                }
            }
        }
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dictionary_winners_precede_scalar_interpretation() {
        assert_eq!(
            select_winners(br#"{"title":"\uD800","title":"ok","metadata":{"x":NaN,"x":1}}"#)
                .unwrap(),
            br#"{"title":"ok","metadata":{"x":1}}"#
        );
    }

    #[test]
    fn literal_and_surrogate_pair_keys_have_python_equality() {
        let text = "{\"\\uD83D\\uDE00\":NaN,\"😀\":1,\"tail\":2}";
        assert_eq!(
            select_winners(text.as_bytes()).unwrap(),
            br#"{"\uD83D\uDE00":1,"tail":2}"#
        );
        assert_eq!(
            select_winners(br#"{"\uD800":NaN,"\ud800":1}"#).unwrap(),
            br#"{"\uD800":1}"#
        );
    }

    #[test]
    fn complete_syntax_and_utf8_are_still_required() {
        for text in [
            b"{\"x\":1,}".as_slice(),
            b"[1,]",
            b"{\"x\":01}",
            b"[1e+]",
            b"{\"x\":1} false",
            b"\"\xff\"",
        ] {
            assert!(select_winners(text).is_err());
        }
    }
}
