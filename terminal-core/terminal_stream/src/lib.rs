//! VT parsing, screen rendering, the output strips, the W9 history ring and output coalescing for the
//! hub's terminal core (docs/rust-terminal-core.md §5.4, §7.2, §8).
//!
//! No Windows API and no Python types here: `conpty_session` owns the PTY and calls [`TerminalStream`] on
//! its reader thread, `hub_terminal_core` binds the results.

pub mod coalesce;
pub mod decoder;
pub mod esc;
pub mod history;
pub mod screen;
pub mod stream;
pub mod width;

pub use coalesce::{Coalescer, FrameQueue, FrameSink, QueueSink, SINK_QUEUE_BYTES};
pub use esc::{strip_color_queries, strip_terminal_replies, Esc3jStrip};
pub use history::{trim_history_text, HistoryRing, BANNER_LINE_MAX_BYTES, TRUNCATED_PREFIX};
pub use screen::Screen;
pub use stream::{StreamOptions, TerminalStream};
