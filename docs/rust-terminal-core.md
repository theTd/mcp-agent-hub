# Rust terminal core

Design for moving the per-output-chunk work out of Python so the hub can run 50 printing sessions at
once. This document is the plan; nothing here is implemented yet. Line numbers refer to the tree at the
time of writing and are anchors, not contracts.

Target: 50 concurrent sessions that all print, with the hub's CPU, thread count and event-loop latency
staying close to today's 12-session numbers. Engine logic, MCP tools, transcripts, approvals, waiting
and persistence stay in Python.

## 1. Goals and non-goals

### Goals

- Own the PTY for every session in Rust: spawn, read, write, resize, kill.
- Parse the terminal stream in Rust and own both surface models Python consumes today:
  the screen (80×visible + 3000 history rows) and the raw ANSI history (512 KB cap, W9 trim).
- Coalesce output into frames per session before it crosses back into Python, and make the frame rate
  independent of the engine's write rate.
- Keep every existing Python consumer's behaviour, including the invariants in §8.
- Leave Python with no work proportional to output chunks: no per-chunk parse, no per-chunk history
  append, no per-chunk `call_soon_threadsafe`, no per-session reader thread.

### Non-goals

- Changing what the console shows, how sessions are launched, approved, observed or persisted.
- Making sessions survive a hub restart. Today a restart ends every CLI process tree
  (DECISIONS.md `shutdown-ends-cli`) and sessions are resumed through their native records; the core
  keeps that (§6).
- Moving transcript parsing, MCP, HTTP or WebSocket *business* logic to Rust.
- Porting to macOS/Linux. The PTY layer, `conpty.dll` and the job objects are Windows-only; a POSIX
  backend is out of scope.

## 2. What the hub does today

Measured on the live hub (2026-11-13, ~12 sessions printing), and confirmed in code:

| fact | value | source |
|---|---|---|
| server CPU | 0.4–0.7 cores | profile |
| thread wakeups | ~4,500/s, ~90 hub threads | profile |
| Python-held GIL share | ~5% of samples | profile |
| per printing session | 3–6% of a core, 200–770 wakeups/s | profile |
| idle session | ~0 | profile |
| output chunks | ~870/s across sessions | profile |
| linear projection at 50 sessions | 2–3 cores, ~19,000 wakeups/s, ~250 threads | profile |

Per chunk, today:

```
winpty/ptyprocess.py _read_in_thread   read(blocking) -> loopback TCP send -> sleep(1 ms)
  -> AgentSession._read_loop            session_manager.py:2280  (one thread per session)
  -> AgentSession._on_pty_output        session_manager.py:2325
       lock -> pyte feed -> generic-engine tool-line scan -> raw_history append + W9 trim check
            -> snapshot of ws_queues
       unlock
     -> one loop.call_soon_threadsafe per chunk PER WebSocket
  -> ws_pty_endpoint sender             server.py:1308  one WebSocket frame per chunk
```

`pywinpty` 3.1.2 is a C extension built on **winpty-rs 1.0.8** (the crate path is embedded in
`_winpty.cp314-win_amd64.pyd`) that calls the bundled `conpty.dll` exports
`ConptyCreatePseudoConsole`, `ConptyResizePseudoConsole`, `ConptyClosePseudoConsole`,
`ConptyReleasePseudoConsole`, `ConptyPackPseudoConsole`, `ConptyReparentPseudoConsole`,
`ConptyShowHidePseudoConsole`, `ConptyClearPseudoConsole`. Its Python layer
(`winpty/ptyprocess.py`) is the loopback socket plus 1 ms sleep loop above — that is where two of the
five hub threads and most of the small-chunk overhead come from.

The pure-Python parser is pyte 0.8.2 through two subclasses: `HubTerminalScreen`
(`session_manager.py:1792`, private-DSR guard and safe `display`) and `HubTerminalStream`
(`:1832`, one bad escape sequence must not abandon the rest of a chunk).

## 3. What moves to Rust

1. **ConPTY lifecycle**: spawn (argv, cwd, env, cols×rows), write, resize, kill the process tree,
   report the exit code and the pid. `unwrap_windows_cmd_shim` (`session_manager.py:1864`) moves with
   it: it is argv surgery in front of the spawn, and it needs the same PATH lookup and npm-shim regex;
   its table becomes a Rust unit test fed by the same fixtures.
2. **VT parsing**: screen (cols×rows, cursor, alt screen, wide/CJK cells), 3000-line history, and text
   rendering that matches `screen.display` plus `rstrip`/blank-line trimming.
3. **Raw history**: the byte-capped ring with W9 trim (`trim_history_text`, `session_manager.py:630`),
   the W8 `ESC[3J` strip for resumed sessions (`_strip_saved_lines_erase`, `:2390`) and the
   `OSC_COLOR_QUERY_RE` strip (`:1852`), with their split-sequence carry buffers.
4. **Per-frame batching**: one coalesced frame per session per flush window, instead of one frame per
   engine write.
5. **Terminal WebSockets** (phase 3, optional but recommended): the core serves `/ws/pty` itself and
   Python is out of the output path entirely.
6. **Activity tracking**: `last_output_at`, bytes/chunks per session, screen revision, liveness — as
   counters pushed at a fixed low rate instead of being accumulated in Python.

Kept in Python: env assembly and policy (`TERM`, `COLORTERM`, `PYTHONHOME` removal, model injection),
the 20/5 col/row clamp and viewport propagation (`resize_session_from_client`, `:3161`), TuiWriterQueue
ordering policy (`:1951`), approval/dialog detection and every parser of screen text
(`parse_codex_approval_prompt`, `_tui_input_line`, `sanitize_tui_screen_text`), the DSH virtual
session's echo emulation (`start_virtual`/`write_input` fallback, `:2200`/`:2463`), persistence and the
console-facing HTTP API.

## 4. Consumer map

Every current reader of the pyte screen or the raw history, what it needs, and its new call.

| consumer | anchor | needs | new call |
|---|---|---|---|
| terminal WebSocket (console) | `server.py:1308` | replay + live raw chunks with Q11 atomicity, input, resize | `subscribe`, `inject_frame`, `write`, `resize`; phase 3: console talks to the core |
| `read_subagent_screen` (MCP) | `server.py:631` | exact screen, ≤80 lines | `screen` |
| screen-based approval / dialog / composer detection | `:7133`, `:7392`, `:10220`, `:10260`, `:10390`, `:10414`, `:10461`, `:10739`, `:11796`, `:11829`, `:12656` | exact screen, `max(38, rows)` lines | `screen` |
| Codex empty-composer check | `:10209` | `cursor.x`, cols, rows | `screen` reply carries cursor and size |
| Kimi banner session id, Codex "is archived", Claude spinner | `:8742`, `:8273`, `:9667` | screen, 35 lines | `screen` |
| generic-engine TUI tool lines | `:2345` | every changed visible row | `watch_screen` snapshot (informational only) |
| exit snapshot for `final_response` | `:2305` | scrollback 80 lines, once | `exit` event body or `scrollback` |
| `persist_session` scrollback text | `:4369` | scrollback 250 lines | `scrollback` |
| `_last_screen_text` / archive `last_screen` | `:4382`, `:4526` | screen 80 lines → last 20 non-empty | `screen` |
| `_scrollback_blob` | `:4437` | joined raw history, already trimmed, plus a revision | `raw` (trimmed core-side, `rev` from core) |
| submit diagnostics | `:10193` | raw ANSI tail, 64 KB | `raw_tail` |
| readiness banner may have scrolled | `:10625` | scrollback N rows | `scrollback` |
| "did the task echo / is it quiet" wait | `:12643`, `:12656` | `last_output_at`, screen 15 lines | `activity` event + `watch_screen` |
| `_crash_tail` | `:10861` | last screen | `screen` |
| `is_process_alive` (B9) | `:2562` | process alive, not status | `alive` flag from `exit`/`activity`, no IPC |
| `terminate` | `:2579` | scrollback snapshot, tree kill | `raw`, then `kill` (job object) |
| `to_dict["pid"]` | `:2709` | pid | `spawn` reply |
| `resize` | `:2486` | PTY + screen resize | `resize` |
| `write_input`, live message injection | `:2463`, call sites `:6718-10759` | ordered keystrokes | `write` |
| DSH virtual session | `:2188`, `:2213` | screen fed from text, no PTY | `attach_text` + `feed` |
| archived screen / scrollback reads | `server.py:669`, `server.py:1032` | the DB row only | unchanged; its input is `raw`/`screen` at persist time |

## 5. Interface between the core and Python

### 5.1 Process and channels

One core process per hub, spawned by the hub at start, with two named pipes:

```
hub --ctl pipe-->  core    request/response, JSON payloads, deadline per request
core --evt pipe--> hub     events, binary header + UTF-8 text
```

- Pipe names: `\\.\pipe\mcp-hub-terminal-<hub_pid>-<16 random hex>`, owner-only DACL (the exact
  `CreateNamedPipeW` security attributes to be fixed at implementation), the suffix also handed to the
  hub on stdout at start. When `HUB_TERMINAL_CORE_PIPE` is set the hub attaches to an existing core
  instead of spawning one (tests, debugging).
- Framing: `u32 len | u8 kind | u8 flags | u16 reserved | payload`.
- `ctl` is strictly one request in flight, guarded by a lock, answered by a dedicated core thread that
  never waits behind output processing. Deadlines: 50 ms for screen/scrollback/raw (they are read at a
  few Hz on the event loop), 2 s for spawn, 2 s for kill, 8 s total budget honoured by `snapshot_all`.
- `evt` is fire-and-forget and never blocks the core: bounded ring per subscriber, overflow drops whole
  frames and emits `overflow`.
- The core's lifetime is bound to the hub's pipes: `ERROR_BROKEN_PIPE` on `evt` ends every session
  (job-object terminate) and exits the core. No orphan agents that lost their MCP endpoint.
- Logging: the core writes stderr; the hub captures it to `<data>/terminal-core.log` (rotating, capped)
  next to `hub.log`.

### 5.2 Commands

Every request carries `id`; every reply carries `ok`/`error`.

| command | arguments | reply | replaces |
|---|---|---|---|
| `hello` | `proto`, hub version | proto, core version, pid | version gate at start |
| `spawn` | session_id, argv, cwd, env, cols, rows, flags, seed_history | pid, cols, rows | `PtyProcess.spawn` (`:2262`) |
| `attach_text` | session_id, cols, rows, flags | ok | `HubTerminalScreen` for DSH virtual sessions |
| `feed` | session_id, text | rev | `_on_pty_output` for virtual sessions |
| `write` | session_id, text | queue depth | `pty.write` (`:2465`) |
| `resize` | session_id, cols, rows | cols, rows | `pty.setwinsize` + `screen.resize` (`:2490`) |
| `kill` | session_id, grace_ms | exit_code | `taskkill /T /F` (`:2593`) |
| `screen` | session_id, max_lines | text, cursor, cols, rows, rev | `get_clean_screen_text` (`:2499`) |
| `scrollback` | session_id, max_lines | text, rev | `get_full_scrollback_text` (`:2516`) |
| `raw` | session_id | text, bytes, rev | `"".join(raw_history)` (`:4454`) |
| `raw_tail` | session_id, max_bytes | text | `raw_ansi_tail` (`:10194`) |
| `subscribe` / `unsubscribe` | session_id, sub_id | replay, rev | `attach_ws_queue` / `detach_ws_queue` (`:2425`) |
| `inject_frame` | session_id, text | ok | `push_ws_frame` (A1 resume separator) |
| `watch_screen` / `unwatch` | session_id, max_lines, interval_ms | ok | none (new, replaces fixed polling) |
| `snapshot_all` | max_lines, max_bytes | per-session blob | graceful shutdown (`graceful-budget`) |
| `kill_all` | grace_ms | ok | shutdown (`shutdown-ends-cli`) |
| `stats` | — | counters | diagnostics |

`flags` for `spawn`/`attach_text`: `strip_esc3j_on_output` (true when `resume_count > 0`),
`strip_osc_color_queries` (always true today), `history_rows` (3000), `raw_cap_bytes` (512 KiB),
`raw_slack_bytes` (64 KiB). `seed_history` sets the raw ring before the first byte of the new process
(today `sess.raw_history = [seed]`, `:4864`) and clears the `ESC[3J` carry.

### 5.3 Events

| event | rate | body |
|---|---|---|
| `output` | coalesced, see 5.4 | session_id, seq, at_unix, screen_rev, text |
| `activity` | 4/s per printing session, none when idle | session_id, last_output_at, bytes_total, chunks_total, screen_rev, alive |
| `screen` | only while watched, ≤10/s | session_id, rev, cols, rows, cursor, text |
| `exit` | once | session_id, exit_code, at_unix, final screen tail |
| `overflow` | on drop | session_id, sub_id, dropped_bytes |
| `core_stats` | 1/s | chunks_in, frames_out, bytes_in, bytes_out, sessions |
| `fatal` | once | reason |

`output.text` is already W8-stripped and OSC-stripped and already decoded: the core owns a per-session
UTF-8 decoder with up to 3 carried bytes, so a multi-byte character split across two ConPTY reads stays
intact. Python never sees a partial character, which keeps `str` semantics identical to
`pty.read()`.

### 5.4 Coalescing and backpressure

Per session the core appends output to a batch and flushes it as one frame when any of: 32 KiB
accumulated, 20 ms since the batch's first byte, or 5 ms of silence after at least one byte. That caps a
session at 50 frames/s while a busy session (≈72 chunks/s today) still delivers every byte in order.

A frame is dropped only when a subscriber's ring is full (2 MB): the oldest whole frames go and an
`overflow` event follows. This is the same failure mode as today's `put_nowait` on a full
`asyncio.Queue(maxsize=500)`, except that it is counted and visible, and a reconnect replays history.

### 5.5 Threading

| today | after |
|---|---|
| pywinpty reader thread + loopback socket per session | 1 blocking ConPTY read thread per session in the core (native, no GIL, no socket) |
| `_read_loop` thread per session | gone |
| `call_soon_threadsafe` per chunk per socket | 1 per coalesced frame while the hub still fans out; 0 in phase 3 |
| `taskkill` process spawn per terminate | `TerminateJobObject` on the session's job |
| ~5 hub threads per session | hub: +1 event reader thread +1 control lock; core: 4 fixed (flush timer, control, stats, IOCP/lifecycle) + 1 reader per session |

The core's readers are native threads that spend their life blocked in `ReadFile`, so they cost no
scheduling. An optional later backend (overlapped pipes on a single IOCP) would collapse them, and is
worth adding only if the load test shows thread count, not CPU, is the limit.

### 5.6 How the per-chunk Python work disappears

- **Parse**: 3,600 chunks/s at 50 sessions × ~3 µs of payload — one Rust thread, no GIL, no
  allocation per chunk beyond the parse itself. Python's cost per chunk becomes zero.
- **History**: the append, the byte count, the slack check and the joined-buffer trim happen inside the
  core; Python reads a trimmed snapshot every 60 s (`RESUME_SCROLLBACK_INTERVAL_SEC`) with the core's
  `rev`, so the existing "changed?" gate in `_scrollback_blob` keeps working unchanged.
- **Fan-out**: one frame per ≤50 ms per session instead of one schedule per chunk per socket. At 50
  sessions that is ≤2,500 `call_soon_threadsafe` per second worst case, and ~200 events/s in phase 3.
- **Polling**: `screen` round trips are ≤200 µs on a local pipe and run at a few Hz per session, the
  same class as today's in-process `screen.display` rebuild. Any informational reader switches to
  `watch_screen` snapshots, whose staleness bound (≤ interval) is stated at the call site.

## 6. Hosting options

| criterion | (a) PyO3 extension in the hub | (b) sidecar process | (c) inside the Tauri app |
|---|---|---|---|
| crash isolation | none: a Rust panic aborts the hub (unwinding across FFI is UB, so every entry point needs `catch_unwind`) and takes every session, MCP stream and wait handle with it | a core crash kills the core's sessions only; the hub stays up, archives them as crashed with their last snapshot, and can respawn the core for new sessions | same as (b) for the hub, but the core is not a process the hub supervises |
| sessions survive a hub restart | no | only if the core outlives the hub — rejected here (§1, `shutdown-ends-cli`); the pipe contract ends sessions with the hub | yes, and that silently contradicts `shutdown-ends-cli`: agents would keep running with a dead MCP endpoint |
| portable build packaging | a compiled wheel; ABI-tagged per Python minor, installed from `requirements.lock` into the venv the app creates | one ~3–4 MB EXE, embedded like the other payload entries (`desktop/portable/payload.txt`, `FILES` in `portable_runtime.rs`) and added to the privacy scan | no new binary, but the core only exists when the app started the hub |
| running from source | needs a build step before `Start-AgentHub.ps1` works — the same cost as (b), but as an import failure instead of a missing EXE with a documented fallback | `cargo build -p hub-terminal-core --release` once; without the EXE the hub logs a line and keeps using pywinpty | broken by design: the hub is also run by hand |
| development and test loop | requires rebuilding the hub (`cargo build` + import) for every change; unit tests are `cargo test` plus Python; a bad build makes the hub unstartable | `cargo test` for parser/trim/shim, `cargo run -- --selftest` for a real ConPTY, hub tests use a fake client or the core with `HUB_TERMINAL_CORE_PIPE` | independent Rust build, but every test needs the desktop app running |
| latency | in-process, ~10 µs | one local pipe hop, ~100–200 µs per control call and ~1 µs per output frame; invisible next to engine reaction times | same as (b) plus Tauri IPC |
| complexity | lowest on the wire, highest on lifecycle: no separate process to supervise, but the hub can never be restarted separately from the core, and GIL/`unsendable` rules leak into the API | a protocol, a supervisor, two pipes, and a version handshake — all small, all explicit | a third owner for the panic domain; the hub must work with and without it; two hubs (4444/4445) share one app process |
| failure modes | hub crash = everything lost; no way to degrade; a blocked Rust call holds the GIL and the loop | wedged core → control deadlines, sessions marked unknown, hub keeps answering; crashed core → respawn; protocol mismatch → refuse and fall back | app crash → hub dies too (already true via the job object); app not running → no core |

**Recommendation: (b) standalone sidecar.** It is the only option that keeps the hub serving when the
new code fails, which is the actual risk of a 50-session design written in a language with `panic`, and
it is the only one that can later take the terminal WebSockets out of Python (§3.5) — an in-process
extension can never remove the hub's own event loop from the output path. It behaves identically when
the hub runs from source and from the portable build, and it adds no Python ABI dependency. Cost: one
protocol, one supervisor, one binary in the payload.

(a) is the right answer only if the owner prefers the least code and accepts that a panic in the parser
takes the hub down; it also makes the 50-session goal depend on GIL behaviour again on the control path.

(c) is worth revisiting *in addition to* (b) later: the app can spawn the core and pass its pipe name to
the hub through `HUB_TERMINAL_CORE_PIPE`, which would keep terminal state alive across a hub restart if
the owner ever wants that. It must not be the only path.

## 7. Crates

### 7.1 ConPTY

| choice | why / why not |
|---|---|
| `winpty-rs` 1.0.8 | **start here.** Exactly what pywinpty 3.1.2 is built on (crate path embedded in `_winpty.cp314-win_amd64.pyd`), so the byte stream, resize and close behaviour are the ones today's captures were taken from. Blocking read API — one native thread per session, which is fine (§5.5). Pin the version pywinpty used. |
| `windows` 0.62 directly | already a dependency of `desktop/src-tauri`; needed anyway for job objects, named pipes and (later) overlapped pipe reads on one IOCP. Use it for everything except the pseudoconsole itself in phase 2. |
| direct calls into the bundled `conpty.dll` | the export names are known (`ConptyCreatePseudoConsole`, `ConptyResizePseudoConsole`, `ConptyReleasePseudoConsole`, `ConptyClosePseudoConsole`, …, verified by scanning the shipped DLL). Only worth it if `winpty-rs` cannot express something (e.g. `ConptyReparentPseudoConsole` for a future Tauri-hosted core). |
| `portable-pty` | not used: (unverified, from memory) it spends a reader thread per PTY with blocking reads and no overlapped I/O, carries a winpty fallback the hub does not need (ConPTY exists on every supported Windows build), and hides the job-object control `kill` depends on. |

### 7.2 VT parsing

| choice | why / why not |
|---|---|
| `alacritty_terminal` (its `vte` parser plus `Term`/`Grid`) | **recommended.** The `vte` state machine cannot raise on bad input — which is precisely the K4 failure (`HubTerminalStream`, `:1832`) and removes the need for the `HubTerminalScreen.report_device_status` guard (`:1792`): unhandled CSI, including `CSI ? 996 n`, is ignored and nothing is written back to the PTY. Wide-character and combining-mark handling is the reason the `display` workaround for orphaned CJK stubs exists; alacritty's grid stores cells by column with the wide flag, so the workaround disappears. Cost: a larger dependency set and a manual line renderer (§8). |
| `vt100` | smaller, screen + scrollback + cursor; (unverified, from memory) fewer tested sequences and a screen model that has lost content under rapid resize/alt-screen switching. Acceptable fallback if `Term`'s API fights the history-row semantics; then the parser is `vte` + our own screen model. |
| `termwiz` | correct and rich, used by WezTerm; its `Surface`/`Scrollback` model is display-oriented, so reproducing `history.top` row semantics is more code, and the dependency tree is the heaviest of the three. |

Rendering rule: the core renders rows exactly as `get_clean_screen_text`/`get_full_scrollback_text`
expect them — each row `rstrip`ped, trailing blank rows dropped from the screen, history walked
newest-first and stopped at `max_lines`. Padding cells render as spaces; a wide cell renders once.

### 7.3 Bundled `conpty.dll` / `OpenConsole.exe`

Keep them. They are the ConPTY implementation today's captures came from, they do not change with the
OS build, and both files already ship with pywinpty. The core ships its own copy beside the EXE (do not
depend on pywinpty's directory). If the DLL is missing, fall back to `kernel32!CreatePseudoConsole` and
log one line; that path is untested here and must be treated as degraded (rendering of cursor/erase
sequences may differ).

## 8. Semantics that must survive migration

| invariant | where it lives now | how the core keeps it |
|---|---|---|
| Q11: a new WebSocket sees every chunk exactly once (replay/register atomic) | `attach_ws_queue` `:2425` under `_lock` | `subscribe` returns the replay snapshot and starts streaming at `seq = replay_seq + 1` on the core's own thread; there is no Python lock left to race |
| A1: a resumed session keeps its sockets, and the resume separator lands between spawn and first output | `adopt_ws_frontends` `:2447`, `push_ws_frame` `:2458`, `:2270` | hub re-`subscribe`s the existing `sub_id` under the new session object and sends `inject_frame` immediately after `spawn` returns; the core writes the separator before it reads the first byte |
| A1: `raw_history` is seeded with the archived scrollback on resume | `:4864` | `spawn.seed_history` |
| W8: `ESC[3J` (erase saved lines) is dropped on resumed sessions only, with a split-sequence carry | `_strip_saved_lines_erase` `:2390` | same algorithm in the core, `strip_esc3j_on_output` flag, per-session carry ≤3 bytes |
| OSC 10/11/12/4 colour queries never reach the console or the history | `OSC_COLOR_QUERY_RE` `:1852` `:2334` `:2437` | same regex in the core, applied before append and before replay |
| OSC replies from the browser never reach the PTY | `strip_osc_terminal_replies` `:1856`, used in `ws_pty_endpoint` | stays in Python (it filters incoming socket text before `write`) |
| W9: raw history capped at 512 KiB, 64 KiB slack, trimmed at a line break, newest `── 已恢复（第 N 次）──` banner kept, `[较早的输出已截断]` prefix | `trim_history_text` `:630`, `_scrollback_blob` `:4437` | byte-identical Rust implementation, proven by golden fixtures (§10) |
| history sizes: screen `history=3000`, `snapshot_all`/`scrollback` caps | `:2157`, `:4369`, `:2516` | `spawn.flags.history_rows` |
| K4: one unparseable sequence costs only itself | `HubTerminalStream` `:1832`, `HubTerminalScreen` `:1792` | `vte` cannot fail; `catch_unwind` around the feed so a handler bug costs one chunk, not the session |
| B9: liveness is the process, never the status string | `is_process_alive` `:2562` | `alive` in `activity`, set false by `exit`; no syscall, no IPC for the common case |
| D7/D1–D13: approval keys are byte-faithful | `write_input` `:2463`, call sites `:6718-7276` | `write` preserves arrival order per session through one writer thread; no interpretation, no translation |
| graceful shutdown ends every CLI and captures its scrollback inside the 8 s budget | `graceful-budget`, `:5671`, `:5082` | `snapshot_all` then `kill_all`, both with deadlines; on timeout the hub logs and archives what it has |
| scrollback is written at most every 60 s, only when changed | `_scrollback_blob` `:4443` | `raw.rev` replaces `raw_history_rev`; `_scrollback_written_at`/`_scrollback_rev_written` logic unchanged |
| `last_screen` = last 20 non-empty screen lines | `_last_screen_text` `:4379` | `screen` reply, same trimming in Python (policy stays there) |
| viewport clamp (cols ≥ 20, rows ≥ 5) and single-window propagation | `:2487`, `:3161` | Python clamps before calling `resize`; the core clamps only defensively |
| DSH virtual sessions have a screen without a PTY | `start_virtual` `:2188`, `emit_virtual_ansi` `:2213` | `attach_text` + `feed`; the echo emulation stays in Python |

No recorded decision is reversed. Two are touched: `shutdown-ends-cli` is preserved by the pipe-bound
lifetime plus `kill_all`, and `retention`'s 512 KB scrollback cap is preserved byte-for-byte by W9 in
the core.

## 9. Migration plan

Global flag `HUB_TERMINAL_CORE` = `off` (default) | `shadow` | `on`, plus an optional per-profile
`terminal_core: true|false` honoured only while the env allows it (`on` forces the core, `shadow` forces
shadow, `off` ignores the profile). Rollback at any point is one env change; sessions that already run
keep their owner until they end, and a resume re-evaluates the flag like D9 re-evaluates args. No data
migration: `raw`/`scrollback`/`last_screen` keep their formats.

| step | work | rollback | size |
|---|---|---|---|
| 0. Freeze behaviour | a temporary env-guarded dump in `_on_pty_output` captures a few minutes of raw chunks per engine; generate golden fixtures (rendered screen, scrollback 250, `raw` after trim, `raw_tail`) into `tools/fixtures/`; write the Python oracle tests against those fixtures; delete the dump | files only, no runtime change | S, 1 d |
| 1. Crate + protocol, `shadow` | `terminal-core/` crate: pipes, protocol, `attach_text`/`feed`, ConPTY spawn/read/write/resize/kill, W8/W9/OSC, `vte` parse; `terminal_core.py` client in the hub; shadow mode feeds every chunk of every pywinpty session to a core parse-only session and logs screen/scrollback diffs | flag `off` | M, 5–8 d |
| 2. Core owns PTYs, `on` for opt-in profiles | `spawn`/`write`/`resize`/`kill`/`screen`/`scrollback`/`raw`/`subscribe`; `_read_loop` replaced by the event pump; exact-`screen` call sites converted; `is_process_alive`, pid, `last_output_at` from events | env `off` + resume | L, 5–10 d |
| 3. Terminal WebSockets in the core | `GET /api/sessions/{id}/terminal-ticket` (HMAC over session_id, expiry, shared secret from `hello`) and a core WS endpoint; console URL changes only; `replay_end` frame produced by the core | env `HUB_TERMINAL_WS=hub` keeps the proxy path | M, 3–5 d |
| 4. Remove the Python path | drop pyte, the K4 subclasses, W8 carry, `trim_history_text` (kept only as fixture generator, then deleted), pywinpty from `requirements.in`/`.lock`, and the Python-side WS batching from the parallel unit | previous commit | S, 2–3 d |
| 5. Load test and default flip | run §11 on an idle machine, tune flush windows and ring sizes, then make `on` the default and delete the profile flag if it no longer earns its place | env `off` | M, 2–4 d |

Total ≈ 4–5 weeks for one implementer, plus review. Steps 1 and 2 dominate.

## 10. Tests

Existing tests that cover this behaviour and what porting them means:

| test | pins | action |
|---|---|---|
| `test_tui_delivery.py` | checked-submit key sequence, one Enter, clear input, no screen-based proofs | replace the shared `FakePty` (`test_native_records.py`) with a fake core client that records the same `inputs` list; keep the assertions literally |
| `test_session_approvals.py` | approval keys reach the PTY, `get_clean_screen_text` patched per test | same fake client; patch the client's `screen` instead of the method |
| `test_hub_persistence.py` | scrollback blob written once per rev, `_scrollback_rev_written` | assert on the core's `rev`; keep the "no rewrite without a new rev" case |
| `test_restart_resume_on_start.py` | resume banner, continuation text, archived-at-stop paths | add: seeded history arrives in the `spawn` payload and the separator is injected before the first frame |
| `test_event_loop_stalls.py` | loop latency under a write backlog | add a case pumping a recorded burst through the fake client; existing cases unchanged |
| `test_session_launch_args.py` | argument stripping/injection | unchanged; the shim table moves to a Rust test with the same fixtures |
| `test_transcript_feed.py` | transcript rendering | unchanged |

New tests, none run by this unit:

- Rust: shim-unwrap table, W9 trim golden fixtures (including a UTF-8 split and the newest-banner rule),
  W8 split-sequence carry, OSC strip, UTF-8 carry across reads, parser-vs-pyte corpus from step 0,
  coalescing (bytes preserved and ordered, frame cap honoured), subscribe/seq no-loss under a reconnect.
- Python: `test_terminal_core_client.py` — protocol framing, deadline expiry falls back to the cached
  snapshot, `overflow` bookkeeping, broken pipe marks sessions crashed rather than hanging a call.
- Load/soak: §11.

## 11. Load test

Run only when the machine is idle; it is not part of the default suite.

- `tools/fake_tui_engine.py` replays a recorded corpus (JSONL of `(delay_ms, chunk)` from step 0) at the
  recorded rate to stdout, then prints a final byte count. Started as a normal session, so the whole
  real path is exercised (ConPTY, core, hub, console), with `--speed 1` matching today's 72 chunks/s per
  session.
- `tools/load_terminal_core.py` starts 15 / 30 / 50 sessions, attaches one WebSocket consumer each,
  samples for 5 minutes, and records: hub CPU (`\Process(python*)\% Processor Time`), context switches
  per second for the hub PID (`\Thread(*)\Context Switches/sec`), thread count, event-loop lag (a
  coroutine measuring `sleep(0.05)` overshoot, p50/p99/max, same technique as
  `test_event_loop_stalls.py`), GIL-held share (py-spy `--gil` if available, otherwise loop lag and CPU
  as the proxies), `core_stats` counters, and per-session byte accounting (corpus bytes vs bytes
  delivered to the consumer).
- Pass criteria at 50 sessions: hub CPU < 0.5 core, p99 loop lag < 50 ms, no lost bytes, Python-side
  wakeups < 1,000/s, hub threads < 20, console FPS ≥ 30 while a session prints continuously.
- Also run the same script against `HUB_TERMINAL_CORE=off` before starting, so the numbers are a
  comparison, not an assertion.

## 12. Risks and open questions

1. **Parser parity.** The approval and composer parsers match exact rows. `alacritty_terminal` may
   render a cell differently from pyte in ways nobody has hit yet. Mitigation: step 1's shadow diff on
   real traffic with a written rule — an unexplained diff keeps that engine on pywinpty.
2. **W9 byte parity.** The trim is byte-level (UTF-8 split, banner protection, prefix). Golden fixtures
   are the only defence; treat any diff as a bug in the port, not in the fixture.
3. **Control path on the event loop.** A wedged core costs up to the 50 ms deadline per call. Mitigation:
   deadline, cached-snapshot fallback, and the core's control thread never waiting behind output.
4. **ConPTY close/resize races.** Closing a pseudoconsole while a read is pending is a known source of
   hangs (unverified specifics). Mitigation: release on a separate thread with a deadline, and kill via
   the session's job object rather than around the console handle.
5. **Nested job objects.** The core runs inside the app's job (kill-on-close) and creates one job per
   session for tree kill. Nested jobs are supported on Windows 8+, but whether the outer job's limits
   allow the inner handle to behave needs a one-hour experiment before step 2 is committed.
6. **Crash blast radius.** A core crash kills every session at once. The hub must archive each as crashed
   with its last snapshot, then respawn; the console must show the loss rather than a frozen terminal.
7. **Packaging.** A new EXE in the payload needs `payload.txt`, `FILES`, the privacy scan
   (`desktop/scripts/check-portable-privacy.mjs`) and the zip layout updated, and the from-source path
   needs a documented `cargo build` plus a clear fallback when the EXE is absent.
8. **Overlap with the parallel unit.** Python-side WS batching is landing now; the core's coalescing
   subsumes it in phase 2 and deletes it in phase 4. Keep both behind their flags so they never batch
   twice in phase 2.
9. **Open questions for the owner.** (i) Core-owned terminal WebSockets in phase 3, or keep the hub as a
   proxy? (ii) Is the per-profile flag wanted in the launcher UI, or is the global env enough? (iii)
   Should the Tauri app ever spawn the core itself (`HUB_TERMINAL_CORE_PIPE`), which would make sessions
   survive a hub restart and therefore needs a decision to replace `shutdown-ends-cli`? (iv) Keep
   `conpty.dll` pinned to the version pywinpty ships, or move to the system ConPTY?
10. **Skipped here.** No code, no protocol schemas compiled, no benchmark. The `windows`/`alacritty_terminal`
    crate versions and APIs cited are from file evidence or marked unverified; pin them when step 1 opens.
