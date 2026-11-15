//! The terminal WebSocket server (§6.4): the listener, the ticket, one thread per console socket.
//!
//! Python no longer owns a terminal socket: the console fetches a ticket from the hub and connects
//! here, so no frame passes through the hub's event loop (§5.2, step 4). The id is the address — a
//! resume replaces the `Session` under it and the sockets keep receiving (`rtc-sinks-per-id`).
//!
//! The surface is [`WsServer`]; `input` and `ticket` are here because their rules are worth a unit test
//! each, and `connection` because one socket's thread is the only place the wire order lives.

mod connection;
mod entropy;
mod input;
mod listener;
mod ticket;

pub use listener::WsServer;
