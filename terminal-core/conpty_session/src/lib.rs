//! Windows terminal sessions for the hub's Rust terminal core (docs/rust-terminal-core.md).
//!
//! The plain-Rust half of the extension: command line parity with pywinpty, system ConPTY spawn, pipe
//! I/O, job objects, one reader/writer pair per session, and the console's terminal WebSocket server.
//! No Python types and no parser here, so `cargo test` covers it without an interpreter;
//! `hub_terminal_core` is the thin PyO3 binding on top.

pub mod command_line;
pub mod conpty;
pub mod core;
/// One console socket's bounded queue (§5.4): step 4's socket sink, drained from Python until then.
mod frame_queue;
pub mod handles;
pub mod job;
pub mod job_tree;
pub mod session;
/// The delivery state that belongs to a session id (§6.4 A1); internals of the core, not its surface.
mod sink_fanout;
/// The console's terminal WebSockets (§6.4): the loopback listener, the ticket and one thread per
/// socket. Its surface is [`ws::WsServer`]; the rest is the core's own.
pub mod ws;

/// Test-only knobs the integration tests need; never part of a release build.
#[cfg(feature = "test-hooks")]
pub mod test_hooks;

pub use core::{
    Core, CoreError, CoreOptions, SocketAttach, SpawnReply, SpawnRequest, Stats, TextRequest,
};
pub use frame_queue::{FrameQueue, Frames};
pub use session::{
    DrainRecord, RawReply, ResizeRequest, ScreenReply, SessionFlags, SessionSnapshot, TextReply,
};
