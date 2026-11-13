//! Windows terminal sessions for the hub's Rust terminal core (docs/rust-terminal-core.md).
//!
//! The plain-Rust half of the extension: command line parity with pywinpty, system ConPTY spawn, pipe
//! I/O, job objects, and one reader/writer pair per session. No Python types and no parser here, so
//! `cargo test` covers it without an interpreter; `hub_terminal_core` is the thin PyO3 binding on top.

pub mod command_line;
pub mod conpty;
pub mod core;
pub mod handles;
pub mod job;
pub mod job_tree;
pub mod session;

/// Test-only knobs the integration tests need; never part of a release build.
#[cfg(feature = "test-hooks")]
pub mod test_hooks;

pub use core::{
    Core, CoreError, CoreOptions, SpawnReply, SpawnRequest, Stats, TextRequest,
};
pub use session::{
    DrainRecord, RawReply, ScreenReply, SessionFlags, SessionSnapshot, TextReply,
};
