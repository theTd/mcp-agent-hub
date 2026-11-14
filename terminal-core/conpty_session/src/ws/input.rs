//! What a console socket may send (§6.4 "Input"): `{"type":"input","data":…}`,
//! `{"type":"resize","cols":…,"rows":…,"propagate":…}` and bare text, exactly as
//! `ws_pty_endpoint` handled them in Python.
//!
//! The console only ever writes JSON (`static/js/session-workspace.js`), but a bare text frame stays
//! supported because the endpoint accepted one and a hand-driven client may still send it. A frame is
//! an object only when it is **exactly one** JSON value and that value is an object, which is what
//! `json.loads`-then-`isinstance(msg, dict)` decided: trailing junk, a non-JSON number such as `+1` or
//! `01`, a bad escape, an unterminated string or a lone surrogate pair all make `json.loads` fail, so
//! the raw frame is typed as bare text. Duplicate keys keep the last value, as `json.loads` does.
//!
//! Two divergences from Python, both on frames the console never sends: `"data"` that is not a string
//! (Python wrote `str(data)`; the frame is ignored and the socket logs it once) and a lone surrogate
//! escape, which Python's `json.loads` keeps and a Rust `String` cannot, so each becomes U+FFFD.
//!
//! This is a small reader, not a JSON library: `serde_json` is deliberately not a dependency (§7.4,
//! "there is no wire protocol"). It parses one complete value with a depth bound and a cursor, so a
//! hostile frame costs one pass, no recursion beyond [`MAX_DEPTH`] and no allocation but the frames.

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

/// What one frame means after the endpoint's rules.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Interpretation {
    /// Nothing to do: an empty frame, another `type`, or an `input` whose `data` is absent or null.
    Nothing,
    Message(BrowserMessage),
    /// `{"type":"input"}` whose `data` is a number, boolean, array or object. Python wrote
    /// `str(data)`; the console never sends it, so the frame is dropped and the socket says so once.
    NonStringData,
}

/// The widths `ws_pty_endpoint` used when a resize frame omitted them (`int(msg.get("cols", 120))`).
const DEFAULT_COLS: u16 = 120;
const DEFAULT_ROWS: u16 = 36;
/// Deeper than any frame the console sends; a hostile one costs a constant, not the stack.
const MAX_DEPTH: usize = 64;

/// Reads one message from the socket.
pub fn interpret(message: &str) -> Interpretation {
    // Only an object can carry a command, and `json.loads` accepts leading whitespace. Any other frame
    // is bare text: Python's dict test failed, or the value was not JSON at all.
    if message.trim_start().starts_with('{') {
        if let Some(fields) = parse_frame(message) {
            return interpret_object(&fields);
        }
    }
    if message.is_empty() {
        Interpretation::Nothing
    } else {
        Interpretation::Message(BrowserMessage::Input(message.to_string()))
    }
}

fn interpret_object(fields: &[(String, Json)]) -> Interpretation {
    // `json.loads` keeps the last duplicate, so the lookup runs from the end.
    let kind = field(fields, "type").and_then(Json::as_str);
    match kind {
        Some("input") => match field(fields, "data") {
            None | Some(Json::Null) => Interpretation::Nothing,
            Some(Json::Str(data)) if data.is_empty() => Interpretation::Nothing,
            Some(Json::Str(data)) => {
                Interpretation::Message(BrowserMessage::Input(data.clone()))
            }
            Some(_) => Interpretation::NonStringData,
        },
        Some("resize") => {
            // `int(msg.get("cols", 120))`: absent takes the default, and a value `int()` would refuse
            // makes the whole resize fail, which the endpoint swallowed. `propagate` is Python's
            // truthiness of `msg.get("propagate", True)`.
            let Some(cols) = size_field(fields, "cols", DEFAULT_COLS) else {
                return Interpretation::Nothing;
            };
            let Some(rows) = size_field(fields, "rows", DEFAULT_ROWS) else {
                return Interpretation::Nothing;
            };
            let propagate = field(fields, "propagate").map(Json::truthy).unwrap_or(true);
            Interpretation::Message(BrowserMessage::Resize {
                cols,
                rows,
                propagate,
            })
        }
        _ => Interpretation::Nothing,
    }
}

/// The last value of `key`, or `None` when the object does not carry it.
fn field<'a>(fields: &'a [(String, Json)], key: &str) -> Option<&'a Json> {
    fields
        .iter()
        .rev()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

/// `int(message.get(key, default))` narrowed to a size: absent keeps the default, a value `int()`
/// would refuse (or one outside `1..=u16::MAX`) is no resize at all.
fn size_field(fields: &[(String, Json)], key: &str, default: u16) -> Option<u16> {
    match field(fields, key) {
        None => Some(default),
        Some(value) => value.as_size(),
    }
}

/// A JSON value as far as the console's frames need it. Containers keep only whether they are empty,
/// which is all `bool()` needs.
#[derive(Clone, Debug, PartialEq)]
enum Json {
    Str(String),
    Num(f64),
    Bool(bool),
    Null,
    Container { empty: bool },
}

impl Json {
    fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(text) => Some(text),
            _ => None,
        }
    }

    /// `bool(value)`: a string, a number, `null` and a container's emptiness.
    fn truthy(&self) -> bool {
        match self {
            Json::Str(text) => !text.is_empty(),
            Json::Num(number) => *number != 0.0,
            Json::Bool(value) => *value,
            Json::Null => false,
            Json::Container { empty } => !empty,
        }
    }

    /// `int()`'s truncation for a number, its integer parse for a string, and `None` for everything
    /// `int()` would raise on — which the endpoint's own `try` swallowed.
    fn as_size(&self) -> Option<u16> {
        let value = match self {
            Json::Num(number) if number.is_finite() => *number as i64,
            Json::Str(text) => parse_int(text)?,
            _ => return None,
        };
        u16::try_from(value).ok().filter(|size| *size >= 1)
    }
}

/// `int(text)` for the ASCII form: surrounding whitespace and an optional sign are accepted, nothing
/// else. Python also takes `_` separators and non-ASCII digits; the console sends neither.
fn parse_int(text: &str) -> Option<i64> {
    let trimmed = text.trim();
    let digits = trimmed.strip_prefix(['+', '-']).unwrap_or(trimmed);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    trimmed.parse::<i64>().ok()
}

/// One complete JSON object and nothing after it.
fn parse_frame(text: &str) -> Option<Vec<(String, Json)>> {
    let mut cursor = Cursor {
        bytes: text.as_bytes(),
        pos: 0,
    };
    cursor.skip_whitespace();
    let fields = cursor.object(0)?;
    cursor.skip_whitespace();
    (cursor.pos == cursor.bytes.len()).then_some(fields)
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

    fn object(&mut self, depth: usize) -> Option<Vec<(String, Json)>> {
        if depth > MAX_DEPTH {
            return None;
        }
        self.expect(b'{')?;
        let mut fields = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.bump();
            return Some(fields);
        }
        loop {
            self.skip_whitespace();
            let key = self.string()?;
            self.skip_whitespace();
            self.expect(b':')?;
            self.skip_whitespace();
            let value = self.value(depth + 1)?;
            fields.push((key, value));
            self.skip_whitespace();
            match self.bump()? {
                b',' => continue,
                b'}' => return Some(fields),
                _ => return None,
            }
        }
    }

    fn array(&mut self, depth: usize) -> Option<Json> {
        if depth > MAX_DEPTH {
            return None;
        }
        self.expect(b'[')?;
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.bump();
            return Some(Json::Container { empty: true });
        }
        loop {
            self.skip_whitespace();
            self.value(depth + 1)?;
            self.skip_whitespace();
            match self.bump()? {
                b',' => continue,
                b']' => return Some(Json::Container { empty: false }),
                _ => return None,
            }
        }
    }

    fn value(&mut self, depth: usize) -> Option<Json> {
        match self.peek()? {
            b'"' => Some(Json::Str(self.string()?)),
            b'{' => Some(Json::Container {
                empty: self.object(depth)?.is_empty(),
            }),
            b'[' => self.array(depth),
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

    /// JSON's number grammar, so `+1`, `01`, `1.`, `.5`, `NaN` and `Infinity` are refused and the
    /// frame falls back to bare text, the way `json.loads` refused them.
    fn number(&mut self) -> Option<Json> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek()? {
            b'0' => {
                self.pos += 1;
                if matches!(self.peek(), Some(b'0'..=b'9')) {
                    return None;
                }
            }
            b'1'..=b'9' => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.pos += 1;
                }
            }
            _ => return None,
        }
        if self.peek() == Some(b'.') {
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return None;
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return None;
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        std::str::from_utf8(&self.bytes[start..self.pos])
            .ok()?
            .parse::<f64>()
            .ok()
            .map(Json::Num)
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
                    b'u' => self.unicode_escape(&mut out)?,
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

    /// `\uXXXX`: a high surrogate followed by a low one is one character; either one alone is a lone
    /// surrogate, which `json.loads` accepts and a Rust `String` cannot, so it becomes U+FFFD.
    fn unicode_escape(&mut self, out: &mut String) -> Option<()> {
        let code = self.hex4()?;
        if (0xDC00..0xE000).contains(&code) {
            out.push(char::REPLACEMENT_CHARACTER);
            return Some(());
        }
        if !(0xD800..0xDC00).contains(&code) {
            out.push(char::from_u32(code)?);
            return Some(());
        }
        let pair = self.pos;
        if self.bytes.get(pair..pair + 2) == Some(b"\\u") {
            self.pos += 2;
            let low = self.hex4()?;
            if !(0xDC00..0xE000).contains(&low) {
                // A valid escape, but not the low half: the high one stays lone, and the escape that
                // follows it is read on its own.
                self.pos = pair;
                out.push(char::REPLACEMENT_CHARACTER);
                return Some(());
            }
            let combined = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
            out.push(char::from_u32(combined)?);
            return Some(());
        }
        out.push(char::REPLACEMENT_CHARACTER);
        Some(())
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

    fn input(text: &str) -> Option<BrowserMessage> {
        match interpret(text) {
            Interpretation::Message(message) => Some(message),
            Interpretation::Nothing => None,
            Interpretation::NonStringData => Some(BrowserMessage::Input("<non-string>".into())),
        }
    }

    #[test]
    fn an_input_frame_carries_the_decoded_text() {
        assert_eq!(
            input(r#"{"type":"input","data":"echo hi\r\n"}"#),
            Some(BrowserMessage::Input("echo hi\r\n".to_string()))
        );
        // xterm.js sends arrows and escapes as `\uXXXX` after `JSON.stringify`.
        assert_eq!(
            input(r#"{"type":"input","data":"\u001b[A"}"#),
            Some(BrowserMessage::Input("\u{1b}[A".to_string()))
        );
        // A character outside the BMP arrives as a surrogate pair.
        assert_eq!(
            input(r#"{"type":"input","data":"\ud83d\ude00"}"#),
            Some(BrowserMessage::Input("😀".to_string()))
        );
        // Multi-byte characters are copied whole.
        assert_eq!(
            input(r#"{"type":"input","data":"你好"}"#),
            Some(BrowserMessage::Input("你好".to_string()))
        );
    }

    #[test]
    fn an_empty_input_and_an_unknown_type_do_nothing() {
        assert_eq!(input(r#"{"type":"input"}"#), None);
        assert_eq!(input(r#"{"type":"input","data":""}"#), None);
        assert_eq!(input(r#"{"type":"input","data":null}"#), None);
        assert_eq!(input(r#"{"type":"focus","data":"x"}"#), None);
        assert_eq!(input("{}"), None);
        assert_eq!(input(""), None);
        assert_eq!(input(r#"{"data":"x"}"#), None);
        assert_eq!(input(r#"{"type":7,"data":"x"}"#), None);
    }

    /// The rows the endpoint's `json.loads`/`str(msg.get("data") or "")` rules decide: what is an
    /// object at all, what is bare text, and which `data` reaches the PTY.
    #[test]
    fn the_frame_rules_match_the_endpoint_table() {
        // (frame, expectation)
        let cases: Vec<(&str, Interpretation)> = vec![
            // Not exactly one JSON value: the whole frame is typed, junk included.
            (
                r#"{"type":"input","data":"TRAILING"} junk"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":"TRAILING"} junk"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":"x"}{"type":"input","data":"y"}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":"x"}{"type":"input","data":"y"}"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":+1}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":+1}"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":01}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":01}"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":1.}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":1.}"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":NaN}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":NaN}"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":"\q"}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":"\q"}"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":"x"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":"x"#.into(),
                )),
            ),
            (
                r#"{"type":"input","data":"x","nested":[+1]}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"input","data":"x","nested":[+1]}"#.into(),
                )),
            ),
            // Duplicate keys: the last one wins, as `json.loads` keeps it.
            (
                r#"{"type":"input","data":"first","data":"last"}"#,
                Interpretation::Message(BrowserMessage::Input("last".into())),
            ),
            (
                r#"{"type":"input","type":"resize","cols":90,"rows":30}"#,
                Interpretation::Message(BrowserMessage::Resize {
                    cols: 90,
                    rows: 30,
                    propagate: true,
                }),
            ),
            // `data` of another type: Python wrote `str(data)`, the core drops the frame.
            (r#"{"type":"input","data":0}"#, Interpretation::NonStringData),
            (r#"{"type":"input","data":false}"#, Interpretation::NonStringData),
            (r#"{"type":"input","data":7}"#, Interpretation::NonStringData),
            (r#"{"type":"input","data":{}}"#, Interpretation::NonStringData),
            (r#"{"type":"input","data":[]}"#, Interpretation::NonStringData),
            // Lone surrogates: `json.loads` accepts them, a Rust `String` cannot, so U+FFFD.
            (
                r#"{"type":"input","data":"a\udc00b"}"#,
                Interpretation::Message(BrowserMessage::Input("a\u{fffd}b".into())),
            ),
            (
                r#"{"type":"input","data":"\ud800\u0041"}"#,
                Interpretation::Message(BrowserMessage::Input("\u{fffd}A".into())),
            ),
            // Another type or no `type`: nothing, and either way not bare text.
            (
                r#"{"type":"resize","cols":null}"#,
                Interpretation::Nothing,
            ),
            // A JSON value that is not an object is typed raw, quotes included.
            (
                r#""quoted""#,
                Interpretation::Message(BrowserMessage::Input("\"quoted\"".into())),
            ),
            (
                "   ",
                Interpretation::Message(BrowserMessage::Input("   ".into())),
            ),
        ];
        for (frame, expected) in cases {
            assert_eq!(interpret(frame), expected, "frame {frame:?}");
        }
    }

    #[test]
    fn a_resize_frame_defaults_its_missing_fields_as_the_endpoint_did() {
        assert_eq!(
            input(r#"{"type":"resize","cols":140,"rows":44,"propagate":false}"#),
            Some(BrowserMessage::Resize {
                cols: 140,
                rows: 44,
                propagate: false
            })
        );
        assert_eq!(
            input(r#"{"type":"resize"}"#),
            Some(BrowserMessage::Resize {
                cols: DEFAULT_COLS,
                rows: DEFAULT_ROWS,
                propagate: true
            })
        );
        assert_eq!(
            input(r#"{"type":"resize","cols":"90","rows":" 30 "}"#),
            Some(BrowserMessage::Resize {
                cols: 90,
                rows: 30,
                propagate: true
            })
        );
        // `int(140.7)` truncates; `1e3` is an integer value.
        assert_eq!(
            input(r#"{"type":"resize","cols":140.7,"rows":1e3}"#),
            Some(BrowserMessage::Resize {
                cols: 140,
                rows: 1000,
                propagate: true
            })
        );
        // `bool()` of the value, as `bool(msg.get("propagate", True))`.
        assert_eq!(
            input(r#"{"type":"resize","cols":90,"rows":30,"propagate":0}"#),
            Some(BrowserMessage::Resize {
                cols: 90,
                rows: 30,
                propagate: false
            })
        );
        assert_eq!(
            input(r#"{"type":"resize","cols":90,"rows":30,"propagate":[]}"#),
            Some(BrowserMessage::Resize {
                cols: 90,
                rows: 30,
                propagate: false
            })
        );
        // A size `int()` would refuse is no resize, not the default one.
        assert_eq!(input(r#"{"type":"resize","cols":"abc","rows":30}"#), None);
        assert_eq!(input(r#"{"type":"resize","cols":0,"rows":30}"#), None);
        assert_eq!(input(r#"{"type":"resize","cols":70000,"rows":30}"#), None);
    }

    #[test]
    fn bare_text_is_input_and_a_broken_object_is_too() {
        assert_eq!(
            input("ls -la\r"),
            Some(BrowserMessage::Input("ls -la\r".to_string()))
        );
        assert_eq!(
            input("{not json"),
            Some(BrowserMessage::Input("{not json".to_string()))
        );
        // A literal that is not one of JSON's three is not a value.
        assert_eq!(
            input(r#"{"type":"input","data":tru}"#),
            Some(BrowserMessage::Input(r#"{"type":"input","data":tru}"#.into()))
        );
    }

    #[test]
    fn unknown_and_nested_fields_are_skipped() {
        assert_eq!(
            input(r#"{"nested":{"a":[1,2,{"b":"}"}]},"type":"input","data":"x","extra":null}"#),
            Some(BrowserMessage::Input("x".to_string()))
        );
    }

    #[test]
    fn a_deeply_nested_frame_is_refused_without_recursing_away_the_stack() {
        let depth = 10_000;
        let frame = format!(
            "{{\"type\":\"input\",\"data\":\"x\",\"deep\":{}{}}}",
            "[".repeat(depth),
            "]".repeat(depth)
        );
        assert!(matches!(interpret(&frame), Interpretation::Message(BrowserMessage::Input(_))));
    }
}
