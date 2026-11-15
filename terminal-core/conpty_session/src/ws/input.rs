//! What a console socket may send (§6.4 "Input"): `{"type":"input","data":…}`,
//! `{"type":"resize","cols":…,"rows":…,"propagate":…}` and bare text, exactly as
//! `ws_pty_endpoint` handled them in Python.
//!
//! The console only ever writes JSON (`static/js/session-workspace.js`), but a bare text frame stays
//! supported because the endpoint accepted one and a hand-driven client may still send it. A frame is
//! an object only when it is **exactly one** JSON value and that value is an object, which is what
//! `json.loads`-then-`isinstance(msg, dict)` decided: trailing junk, a non-JSON number such as `+1` or
//! `01`, a bad escape and an unterminated string all fail to parse, so the raw frame is typed as bare
//! text. Duplicate keys keep the last value, as `json.loads` does.
//!
//! Frames are parsed by `serde_json` (decision `json-reader`, owner 2026-11-14). What stays here is the
//! thin layer Python put on top of a parsed value, plus three divergences, all on frames the console
//! never sends:
//! - `NaN`, `Infinity` and `-Infinity`: `json.loads` accepts them, serde_json (like the reader before
//!   it) refuses the frame, so it is bare text.
//! - `"data"` that is not a string: Python wrote `str(data)`; the frame is ignored and the socket logs
//!   it once.
//! - a number outside what an f64 holds (`1e400`): `json.loads` made it `inf`, serde_json refuses the
//!   frame, so it is bare text where it used to parse (and then be no resize at all).
//!
//! A lone surrogate escape is the one value `json.loads` kept and serde_json refuses: a Rust `String`
//! cannot hold it, so such a frame is read a second time with each unpaired `\uD800..\uDFFF` written as
//! U+FFFD, the character the old reader gave it. A well-formed frame, pairs included, is parsed once.

use serde_json::{Map, Value};

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

/// Reads one message from the socket.
pub fn interpret(message: &str) -> Interpretation {
    // Only an object can carry a command, and `json.loads` accepts leading whitespace. Any other frame
    // is bare text: Python's dict test failed, or the value was not JSON at all.
    if message.trim_start().starts_with('{') {
        if let Some(object) = parse_object(message) {
            return interpret_object(&object);
        }
    }
    if message.is_empty() {
        Interpretation::Nothing
    } else {
        Interpretation::Message(BrowserMessage::Input(message.to_string()))
    }
}

fn interpret_object(object: &Map<String, Value>) -> Interpretation {
    // serde_json's map keeps the last duplicate, as `json.loads` does.
    match object.get("type").and_then(Value::as_str) {
        Some("input") => match object.get("data") {
            None | Some(Value::Null) => Interpretation::Nothing,
            Some(Value::String(data)) if data.is_empty() => Interpretation::Nothing,
            Some(Value::String(data)) => {
                Interpretation::Message(BrowserMessage::Input(data.clone()))
            }
            Some(_) => Interpretation::NonStringData,
        },
        Some("resize") => {
            // `int(msg.get("cols", 120))`: absent takes the default, and a value `int()` would refuse
            // makes the whole resize fail, which the endpoint swallowed. `propagate` is Python's
            // truthiness of `msg.get("propagate", True)`.
            let Some(cols) = size_field(object, "cols", DEFAULT_COLS) else {
                return Interpretation::Nothing;
            };
            let Some(rows) = size_field(object, "rows", DEFAULT_ROWS) else {
                return Interpretation::Nothing;
            };
            let propagate = object.get("propagate").is_none_or(truthy);
            Interpretation::Message(BrowserMessage::Resize {
                cols,
                rows,
                propagate,
            })
        }
        _ => Interpretation::Nothing,
    }
}

/// `int(message.get(key, default))` narrowed to a size: absent keeps the default, a value `int()`
/// would refuse (or one outside `1..=u16::MAX`) is no resize at all.
fn size_field(object: &Map<String, Value>, key: &str, default: u16) -> Option<u16> {
    match object.get(key) {
        None => Some(default),
        Some(value) => as_size(value),
    }
}

/// `bool(value)`: a string and a number by their truth, `null` false, a container by its emptiness.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// `int()`'s truncation for a number, its integer parse for a string, and `None` for everything
/// `int()` would raise on — which the endpoint's own `try` swallowed. A `Value::Number` is always
/// finite: serde_json refuses `NaN`, `Infinity` and a literal past `f64::MAX`.
fn as_size(value: &Value) -> Option<u16> {
    let size = match value {
        // An integer stays exact; a float such as `140.7` or `1e3` truncates toward zero.
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64))?,
        Value::String(text) => parse_int(text)?,
        _ => return None,
    };
    u16::try_from(size).ok().filter(|size| *size >= 1)
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

/// One complete JSON object and nothing after it, read by serde_json, which accepts the leading and
/// trailing whitespace `json.loads` did and refuses trailing junk.
fn parse_object(text: &str) -> Option<Map<String, Value>> {
    match serde_json::from_str::<Value>(text) {
        Ok(Value::Object(object)) => Some(object),
        Ok(_) => None,
        // A lone surrogate escape is the only value `json.loads` kept and serde_json refuses on an
        // otherwise valid frame, and it needs a `\u` in the text at all: every other refusal is final.
        Err(_) if text.contains("\\u") => {
            let repaired = without_lone_surrogates(text)?;
            match serde_json::from_str::<Value>(&repaired) {
                Ok(Value::Object(object)) => Some(object),
                _ => None,
            }
        }
        Err(_) => None,
    }
}

/// A copy of `text` in which every unpaired `\uD800..\uDFFF` escape is written as `\ufffd`, the
/// character the reader always gave one; `None` when the frame has no such escape. Paired escapes are
/// copied untouched, so only a frame serde_json refused reaches this and each run is one pass.
fn without_lone_surrogates(text: &str) -> Option<String> {
    fn hex4(bytes: &[u8], at: usize) -> Option<u32> {
        let mut value = 0u32;
        for digit in bytes.get(at..at + 4)? {
            value = value * 16 + char::from(*digit).to_digit(16)?;
        }
        Some(value)
    }

    let bytes = text.as_bytes();
    let mut repaired = Vec::with_capacity(bytes.len());
    let mut pos = 0;
    let mut changed = false;
    while let Some(&byte) = bytes.get(pos) {
        if byte != b'\\' {
            repaired.push(byte);
            pos += 1;
            continue;
        }
        // Every escape but `\u` is copied two bytes at a time, so an escaped backslash in front of a
        // `u` (`"\\ud800"`) never looks like a surrogate escape.
        if bytes.get(pos + 1) != Some(&b'u') {
            let end = (pos + 2).min(bytes.len());
            repaired.extend_from_slice(&bytes[pos..end]);
            pos = end;
            continue;
        }
        // Not `\uXXXX`: another escape or a truncated one, which stays exactly as written because
        // the frame is refused either way.
        let Some(code) = hex4(bytes, pos + 2) else {
            repaired.extend_from_slice(&bytes[pos..]);
            break;
        };
        let low_follows = bytes.get(pos + 6) == Some(&b'\\')
            && bytes.get(pos + 7) == Some(&b'u')
            && matches!(hex4(bytes, pos + 8), Some(low) if (0xDC00..0xE000).contains(&low));
        if (0xD800..0xDC00).contains(&code) && low_follows {
            // A pair is one character and stays as written.
            repaired.extend_from_slice(&bytes[pos..pos + 12]);
            pos += 12;
        } else if (0xD800..0xE000).contains(&code) {
            repaired.extend_from_slice(b"\\ufffd");
            pos += 6;
            changed = true;
        } else {
            repaired.extend_from_slice(&bytes[pos..pos + 6]);
            pos += 6;
        }
    }
    changed.then(|| String::from_utf8(repaired).ok()).flatten()
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
            // A well-paired escape in the same frame stays one character: only the lone one changes,
            // so the second pass must not rewrite the pair.
            (
                r#"{"type":"input","data":"\ud83d\ude00\ud800"}"#,
                Interpretation::Message(BrowserMessage::Input("😀\u{fffd}".into())),
            ),
            // `\\ud800` is a literal backslash and the text `ud800`: only the escape after it is lone,
            // so the repair must not read an escaped backslash as the start of one.
            (
                r#"{"type":"input","data":"\\ud800\ud800"}"#,
                Interpretation::Message(BrowserMessage::Input("\\ud800\u{fffd}".into())),
            ),
            // serde_json refuses a number past `f64::MAX` where `json.loads` made `inf`: the frame is
            // bare text, and it was no resize either way.
            (
                r#"{"type":"resize","cols":1e400,"rows":30}"#,
                Interpretation::Message(BrowserMessage::Input(
                    r#"{"type":"resize","cols":1e400,"rows":30}"#.into(),
                )),
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
