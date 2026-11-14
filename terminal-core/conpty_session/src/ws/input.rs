//! What a console socket may send (§6.4 "Input"): `{"type":"input","data":…}`,
//! `{"type":"resize","cols":…,"rows":…,"propagate":…}` and bare text, exactly as
//! `ws_pty_endpoint` handled them in Python.
//!
//! The console only ever writes JSON (`static/js/session-workspace.js`), but a bare text frame stays
//! supported because the endpoint accepted one and a hand-driven client may still send it. Anything
//! that is not a JSON *object* is bare text — the same rule Python's `json.loads`-then-`isinstance`
//! pair had — and an object with an unknown `type` is ignored.
//!
//! This is a small reader, not a JSON library: `serde_json` is deliberately not a dependency (§7.4,
//! "there is no wire protocol"), and the two shapes above are all the wire carries. It reads one
//! object of scalar values, skips anything nested, and decodes the string escapes the browser emits —
//! including the surrogate pairs `JSON.stringify` writes for characters outside the BMP.

/// One frame the console may have sent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BrowserMessage {
    /// Text for the session's PTY, still carrying any OSC reply the caller must strip.
    Input(String),
    /// A size the console asked for. The core records it; the hub applies its clamp and propagation.
    Resize {
        cols: u16,
        rows: u16,
        propagate: bool,
    },
}

/// The widths `ws_pty_endpoint` used when a resize frame omitted them (`int(msg.get("cols", 120))`).
const DEFAULT_COLS: u16 = 120;
const DEFAULT_ROWS: u16 = 36;

/// Reads one message from the socket. `None` means "nothing to do": an empty frame, an object of
/// another type, or a frame whose `input` data is empty.
pub fn interpret(message: &str) -> Option<BrowserMessage> {
    if message.trim_start().starts_with('{') {
        if let Some(fields) = parse_object(message) {
            let kind = fields
                .iter()
                .find(|(key, _)| key == "type")
                .and_then(|(_, value)| value.as_str());
            return match kind {
                Some("input") => {
                    let data = fields
                        .iter()
                        .find(|(key, _)| key == "data")
                        .map(|(_, value)| value.as_scalar_text())
                        .unwrap_or_default();
                    (!data.is_empty()).then_some(BrowserMessage::Input(data))
                }
                Some("resize") => {
                    let cols = field_u16(&fields, "cols").unwrap_or(DEFAULT_COLS);
                    let rows = field_u16(&fields, "rows").unwrap_or(DEFAULT_ROWS);
                    let propagate = fields
                        .iter()
                        .find(|(key, _)| key == "propagate")
                        .and_then(|(_, value)| value.as_bool())
                        .unwrap_or(true);
                    Some(BrowserMessage::Resize {
                        cols,
                        rows,
                        propagate,
                    })
                }
                _ => None,
            };
        }
        // A `{` that is not JSON: Python's `json.loads` failed and the endpoint typed the raw frame.
        return Some(BrowserMessage::Input(message.to_string()));
    }
    (!message.is_empty()).then_some(BrowserMessage::Input(message.to_string()))
}

fn field_u16(fields: &[(String, Json)], key: &str) -> Option<u16> {
    fields
        .iter()
        .find(|(name, _)| name == key)
        .and_then(|(_, value)| value.as_u16())
}

/// A JSON value as far as the console's frames need it. Arrays and objects are recognised only so they
/// can be skipped: nothing in the protocol nests.
#[derive(Clone, Debug, PartialEq)]
enum Json {
    Str(String),
    Num(f64),
    Bool(bool),
    Null,
    Other,
}

impl Json {
    fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(text) => Some(text),
            _ => None,
        }
    }

    /// `str(msg.get("data") or "")` for a non-string value: Python wrote the number's text to the PTY,
    /// so a numeric `data` is its literal, and everything else is nothing.
    fn as_scalar_text(&self) -> String {
        match self {
            Json::Str(text) => text.clone(),
            Json::Num(number) => format_number(*number),
            Json::Bool(value) => value.to_string(),
            Json::Null | Json::Other => String::new(),
        }
    }

    fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(value) => Some(*value),
            _ => None,
        }
    }

    /// `int(msg.get("cols", …))` narrowed to a size: a fraction truncates, a string parses, and a
    /// value outside `1..=u16::MAX` is no size at all (so the caller's default stands).
    fn as_u16(&self) -> Option<u16> {
        let value = match self {
            Json::Num(number) if number.is_finite() => *number as i64,
            Json::Str(text) => text.trim().parse::<i64>().ok()?,
            _ => return None,
        };
        u16::try_from(value).ok().filter(|size| *size >= 1)
    }
}

fn format_number(number: f64) -> String {
    if number.fract() == 0.0 && number.abs() < 1e15 {
        format!("{}", number as i64)
    } else {
        format!("{number}")
    }
}

fn parse_object(text: &str) -> Option<Vec<(String, Json)>> {
    let mut cursor = Cursor {
        bytes: text.as_bytes(),
        pos: 0,
    };
    cursor.skip_whitespace();
    cursor.expect(b'{')?;
    let mut fields = Vec::new();
    cursor.skip_whitespace();
    if cursor.peek() == Some(b'}') {
        cursor.bump();
        return Some(fields);
    }
    loop {
        cursor.skip_whitespace();
        let key = cursor.string()?;
        cursor.skip_whitespace();
        cursor.expect(b':')?;
        cursor.skip_whitespace();
        let value = cursor.value()?;
        fields.push((key, value));
        cursor.skip_whitespace();
        match cursor.bump()? {
            b',' => continue,
            b'}' => return Some(fields),
            _ => return None,
        }
    }
}

struct Cursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl Cursor<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn bump(&mut self) -> Option<u8> {
        let byte = self.peek()?;
        self.pos += 1;
        Some(byte)
    }

    fn expect(&mut self, byte: u8) -> Option<()> {
        (self.bump()? == byte).then_some(())
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn value(&mut self) -> Option<Json> {
        match self.peek()? {
            b'"' => Some(Json::Str(self.string()?)),
            b'{' => {
                self.skip_container(b'{', b'}')?;
                Some(Json::Other)
            }
            b'[' => {
                self.skip_container(b'[', b']')?;
                Some(Json::Other)
            }
            b't' => self.literal("true").map(|_| Json::Bool(true)),
            b'f' => self.literal("false").map(|_| Json::Bool(false)),
            b'n' => self.literal("null").map(|_| Json::Null),
            _ => self.number(),
        }
    }

    fn literal(&mut self, word: &str) -> Option<()> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Some(())
        } else {
            None
        }
    }

    fn number(&mut self) -> Option<Json> {
        let start = self.pos;
        while matches!(
            self.peek(),
            Some(b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E')
        ) {
            self.pos += 1;
        }
        if start == self.pos {
            return None;
        }
        std::str::from_utf8(&self.bytes[start..self.pos])
            .ok()?
            .parse::<f64>()
            .ok()
            .map(Json::Num)
    }

    /// Skips a nested value of any shape, strings included, so a key the protocol does not use cannot
    /// break the frame.
    fn skip_container(&mut self, open: u8, close: u8) -> Option<()> {
        self.expect(open)?;
        let mut depth = 1usize;
        while depth > 0 {
            match self.bump()? {
                b'"' => {
                    self.skip_string()?;
                }
                byte if byte == open => depth += 1,
                byte if byte == close => depth -= 1,
                _ => {}
            }
        }
        Some(())
    }

    fn skip_string(&mut self) -> Option<()> {
        loop {
            match self.bump()? {
                b'"' => return Some(()),
                b'\\' => {
                    self.bump()?;
                }
                _ => {}
            }
        }
    }

    fn string(&mut self) -> Option<String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            let byte = self.bump()?;
            match byte {
                b'"' => return Some(out),
                b'\\' => match self.bump()? {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'b' => out.push('\u{8}'),
                    b'f' => out.push('\u{c}'),
                    b'n' => out.push('\n'),
                    b'r' => out.push('\r'),
                    b't' => out.push('\t'),
                    b'u' => out.push(self.unicode_escape()?),
                    _ => return None,
                },
                // A raw control byte is not valid JSON text.
                byte if byte < 0x20 => return None,
                byte if byte < 0x80 => out.push(byte as char),
                byte => {
                    // The frame is a `&str`, so a multi-byte character is whole in `bytes`.
                    let width = match byte {
                        0xF0..=0xFF => 4,
                        0xE0..=0xEF => 3,
                        _ => 2,
                    };
                    let start = self.pos - 1;
                    let end = start + width;
                    let text = std::str::from_utf8(self.bytes.get(start..end)?).ok()?;
                    out.push_str(text);
                    self.pos = end;
                }
            }
        }
    }

    /// `\uXXXX`, pairing a high surrogate with the low one that must follow it.
    fn unicode_escape(&mut self) -> Option<char> {
        let high = self.hex4()?;
        let code = if (0xD800..0xDC00).contains(&high) {
            self.expect(b'\\')?;
            self.expect(b'u')?;
            let low = self.hex4()?;
            if !(0xDC00..0xE000).contains(&low) {
                return None;
            }
            0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
        } else if (0xDC00..0xE000).contains(&high) {
            return None;
        } else {
            high
        };
        char::from_u32(code)
    }

    fn hex4(&mut self) -> Option<u32> {
        let mut value = 0u32;
        for _ in 0..4 {
            let digit = (self.bump()? as char).to_digit(16)?;
            value = value * 16 + digit;
        }
        Some(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_input_frame_carries_the_decoded_text() {
        assert_eq!(
            interpret(r#"{"type":"input","data":"echo hi\r\n"}"#),
            Some(BrowserMessage::Input("echo hi\r\n".to_string()))
        );
        // xterm.js sends arrows and escapes as `\uXXXX` after `JSON.stringify`.
        assert_eq!(
            interpret(r#"{"type":"input","data":"\u001b[A"}"#),
            Some(BrowserMessage::Input("\u{1b}[A".to_string()))
        );
        // A character outside the BMP arrives as a surrogate pair.
        assert_eq!(
            interpret(r#"{"type":"input","data":"\ud83d\ude00"}"#),
            Some(BrowserMessage::Input("😀".to_string()))
        );
        // Multi-byte characters are copied whole.
        assert_eq!(
            interpret(r#"{"type":"input","data":"你好"}"#),
            Some(BrowserMessage::Input("你好".to_string()))
        );
    }

    #[test]
    fn an_empty_input_and_an_unknown_type_do_nothing() {
        assert_eq!(interpret(r#"{"type":"input"}"#), None);
        assert_eq!(interpret(r#"{"type":"input","data":""}"#), None);
        assert_eq!(interpret(r#"{"type":"focus","data":"x"}"#), None);
        assert_eq!(interpret("{}"), None);
        assert_eq!(interpret(""), None);
        assert_eq!(interpret(r#"{"data":"x"}"#), None);
    }

    #[test]
    fn a_resize_frame_defaults_its_missing_fields_as_the_endpoint_did() {
        assert_eq!(
            interpret(r#"{"type":"resize","cols":140,"rows":44,"propagate":false}"#),
            Some(BrowserMessage::Resize {
                cols: 140,
                rows: 44,
                propagate: false
            })
        );
        assert_eq!(
            interpret(r#"{"type":"resize"}"#),
            Some(BrowserMessage::Resize {
                cols: DEFAULT_COLS,
                rows: DEFAULT_ROWS,
                propagate: true
            })
        );
        assert_eq!(
            interpret(r#"{"type":"resize","cols":"90","rows":30}"#),
            Some(BrowserMessage::Resize {
                cols: 90,
                rows: 30,
                propagate: true
            })
        );
        // A size that is not a u16 is no size: the default stands.
        assert_eq!(
            interpret(r#"{"type":"resize","cols":0,"rows":70000}"#),
            Some(BrowserMessage::Resize {
                cols: DEFAULT_COLS,
                rows: DEFAULT_ROWS,
                propagate: true
            })
        );
    }

    #[test]
    fn bare_text_is_input_and_a_broken_object_is_too() {
        assert_eq!(
            interpret("ls -la\r"),
            Some(BrowserMessage::Input("ls -la\r".to_string()))
        );
        assert_eq!(
            interpret("{not json"),
            Some(BrowserMessage::Input("{not json".to_string()))
        );
        // A JSON value that is not an object: the endpoint typed the raw frame.
        assert_eq!(
            interpret(r#""quoted""#),
            Some(BrowserMessage::Input("\"quoted\"".to_string()))
        );
        assert_eq!(interpret("   "), Some(BrowserMessage::Input("   ".to_string())));
    }

    #[test]
    fn unknown_and_nested_fields_are_skipped() {
        assert_eq!(
            interpret(
                r#"{"nested":{"a":[1,2,{"b":"}"}]},"type":"input","data":"x","extra":null}"#
            ),
            Some(BrowserMessage::Input("x".to_string()))
        );
        // A numeric `data` is its literal text, as `str(value)` was in Python.
        assert_eq!(
            interpret(r#"{"type":"input","data":7}"#),
            Some(BrowserMessage::Input("7".to_string()))
        );
    }

    #[test]
    fn a_truncated_or_malformed_object_is_bare_text_not_a_crash() {
        for broken in [
            r#"{"type":"input""#,
            r#"{"type":"input","data":}"#,
            r#"{"type":"input","data":"unterminated}"#,
        ] {
            assert!(
                matches!(interpret(broken), Some(BrowserMessage::Input(_))),
                "{broken} should fall back to bare text"
            );
        }
        // A lone low surrogate is not text: the frame is refused as JSON and typed as it arrived.
        assert!(matches!(
            interpret(r#"{"type":"input","data":"\udc00"}"#),
            Some(BrowserMessage::Input(_))
        ));
    }
}
