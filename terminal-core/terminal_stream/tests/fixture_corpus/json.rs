//! A small JSON reader for the fixture corpus (`meta.json`, `chunks.jsonl`, `trigger.json`).
//!
//! The crate has no JSON dependency on purpose (doc §7.4: there is no wire protocol), so the fixture
//! harness carries the ~150 lines it needs. Values are read-only; objects keep their key order.

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(f64),
    Text(String),
    Array(Vec<Value>),
    Object(BTreeMap<String, Value>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(map) => map.get(key),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Text(text) => Some(text),
            _ => None,
        }
    }

    pub fn as_usize(&self) -> Option<usize> {
        match self {
            Value::Number(number) if *number >= 0.0 => Some(*number as usize),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(items) => Some(items),
            _ => None,
        }
    }
}

pub fn parse(text: &str) -> Result<Value, String> {
    let mut reader = Reader {
        bytes: text.as_bytes(),
        at: 0,
    };
    let value = reader.value()?;
    reader.skip_space();
    if reader.at != reader.bytes.len() {
        return Err(format!("trailing data at byte {}", reader.at));
    }
    Ok(value)
}

struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Reader<'a> {
    fn skip_space(&mut self) {
        while self.at < self.bytes.len() && matches!(self.bytes[self.at], b' ' | b'\t' | b'\r' | b'\n') {
            self.at += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    fn expect(&mut self, byte: u8) -> Result<(), String> {
        if self.peek() == Some(byte) {
            self.at += 1;
            Ok(())
        } else {
            Err(format!("expected {:?} at byte {}", byte as char, self.at))
        }
    }

    fn value(&mut self) -> Result<Value, String> {
        self.skip_space();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Value::Text(self.string()?)),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'n') => self.literal("null", Value::Null),
            Some(byte) if byte == b'-' || byte.is_ascii_digit() => self.number(),
            other => Err(format!("unexpected {:?} at byte {}", other, self.at)),
        }
    }

    fn literal(&mut self, word: &str, value: Value) -> Result<Value, String> {
        if self.bytes[self.at..].starts_with(word.as_bytes()) {
            self.at += word.len();
            Ok(value)
        } else {
            Err(format!("expected {word} at byte {}", self.at))
        }
    }

    fn object(&mut self) -> Result<Value, String> {
        self.expect(b'{')?;
        let mut map = BTreeMap::new();
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(Value::Object(map));
        }
        loop {
            self.skip_space();
            let key = self.string()?;
            self.skip_space();
            self.expect(b':')?;
            let value = self.value()?;
            map.insert(key, value);
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    return Ok(Value::Object(map));
                }
                other => return Err(format!("unexpected {:?} in object at byte {}", other, self.at)),
            }
        }
    }

    fn array(&mut self) -> Result<Value, String> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_space();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    return Ok(Value::Array(items));
                }
                other => return Err(format!("unexpected {:?} in array at byte {}", other, self.at)),
            }
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            let Some(byte) = self.peek() else {
                return Err("unterminated string".to_string());
            };
            self.at += 1;
            match byte {
                b'"' => return Ok(out),
                b'\\' => {
                    let Some(escape) = self.peek() else {
                        return Err("unterminated escape".to_string());
                    };
                    self.at += 1;
                    match escape {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let first = self.hex4()?;
                            let char = if (0xD800..0xDC00).contains(&first) {
                                // A surrogate pair: the corpus has none, but the reader stays total.
                                if self.peek() == Some(b'\\') {
                                    self.at += 1;
                                    self.expect(b'u')?;
                                    let second = self.hex4()?;
                                    let code = 0x10000
                                        + ((first - 0xD800) << 10)
                                        + (second.wrapping_sub(0xDC00));
                                    char::from_u32(code).unwrap_or('\u{FFFD}')
                                } else {
                                    '\u{FFFD}'
                                }
                            } else {
                                char::from_u32(first).unwrap_or('\u{FFFD}')
                            };
                            out.push(char);
                        }
                        other => return Err(format!("bad escape {:?}", other as char)),
                    }
                }
                _ => {
                    // A byte of a multi-byte character: copy the whole character.
                    let start = self.at - 1;
                    let mut end = self.at;
                    while end < self.bytes.len() && (self.bytes[end] & 0xC0) == 0x80 {
                        end += 1;
                    }
                    let text = std::str::from_utf8(&self.bytes[start..end])
                        .map_err(|error| format!("invalid utf-8 in string: {error}"))?;
                    out.push_str(text);
                    self.at = end;
                }
            }
        }
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let slice = self
            .bytes
            .get(self.at..self.at + 4)
            .ok_or_else(|| "short \\u escape".to_string())?;
        let text = std::str::from_utf8(slice).map_err(|error| error.to_string())?;
        let value = u32::from_str_radix(text, 16).map_err(|error| error.to_string())?;
        self.at += 4;
        Ok(value)
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.at;
        while let Some(byte) = self.peek() {
            if byte.is_ascii_digit() || matches!(byte, b'-' | b'+' | b'.' | b'e' | b'E') {
                self.at += 1;
            } else {
                break;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.at]).map_err(|e| e.to_string())?;
        text.parse::<f64>()
            .map(Value::Number)
            .map_err(|error| format!("bad number {text:?}: {error}"))
    }
}
