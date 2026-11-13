# Rust terminal core

Design for moving the per-output-chunk work out of Python so the hub can run 50 printing sessions at
once. This document is the plan; nothing here is implemented yet. Line numbers refer to the tree at the
time of writing and are anchors, not contracts.

The owner chose the hosting on 2026-11-12 (decision page `hub-rust-core-1112`, the `terminal-core-*` rows
in DECISIONS.md): a **PyO3 extension inside the hub process** (`hub_terminal_core`), which owns the
ConPTYs, parses on its own native threads and serves the terminal WebSockets on its own loopback
listener; **no feature flag and no legacy path** — the extension replaces pywinpty and pyte outright, and
rollback is reverting the merge. There is no sidecar, no pipe protocol and no Python-side batching to
shadow.

Target: 50 concurrent sessions that all print, with the hub's CPU, thread count and event-loop latency
staying close to today's 12-session numbers. Engine logic, MCP tools, transcripts, approvals, waiting
and persistence stay in Python.

## 1. Goals and non-goals

### Goals

- Own the PTY for every session in Rust: spawn, read, write, resize, kill.
- Parse the terminal stream in Rust and own both surface models Python consumes today:
  the screen (80×visible + 3000 history rows) and the raw ANSI history (512 KB cap, W9 trim).
- Coalesce output into frames per session, and make the frame rate independent of the engine's write
  rate.
- Keep every existing Python consumer's behaviour, including the invariants in §8.
- Leave Python with no work proportional to output chunks: no per-chunk parse, no per-chunk history
  append, no per-chunk `call_soon_threadsafe`, no per-session reader thread.
- Keep the hub's event loop out of the output path entirely: the core reads, parses and writes the
  terminal sockets on native threads that never hold the GIL per chunk (§6.5). Python is woken on a
  fixed timer and only to read counters.

### Non-goals

- Changing what the console shows, how sessions are launched, approved, observed or persisted.
- Making sessions survive a hub restart. Today a restart ends every CLI process tree
  (DECISIONS.md `shutdown-ends-cli`) and sessions are resumed through their native records; the core
  keeps that, and in-process hosting makes it automatic (§6.6).
- Moving transcript parsing, MCP, HTTP or WebSocket *business* logic to Rust. The core terminates the
  terminal sockets; `/mcp`, the REST API, the console push socket and every other route stay in Python.
- Porting to macOS/Linux. The PTY layer, `CreatePseudoConsole` and the job objects are Windows-only; a
  POSIX backend is out of scope.
- Keeping a Python implementation of the PTY or the parser that a runtime switch can fall back to.
  Removed outright; `pyte` survives at most as an offline fixture generator (§9 step 0, §12).

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
   engine write. This is the only batching in the system (owner decision 7: the Python-side batching
   unit never lands).
5. **Terminal WebSockets**: the core serves the terminal socket itself, on its own native threads and
   its own loopback listener, behind a short-lived ticket the core mints and the hub hands to the
   console. Python holds no queue in the output path.
6. **Activity tracking**: `last_output_at`, bytes/chunks per session, screen revision, liveness — as
   counters Python reads at a fixed rate, instead of being accumulated per chunk in Python.

Kept in Python: env assembly and policy (`TERM`, `COLORTERM`, `PYTHONHOME` removal, model injection),
the 20/5 col/row clamp and viewport propagation (`resize_session_from_client`, `:3161`), TuiWriterQueue
ordering policy (`:1951`), approval/dialog detection and every parser of screen text
(`parse_codex_approval_prompt`, `_tui_input_line`, `sanitize_tui_screen_text`), the DSH virtual
session's echo emulation (`start_virtual`/`write_input` fallback, `:2200`/`:2463`), persistence,
ticket issuing and the console-facing HTTP API.

## 4. Consumer map

Every current reader of the pyte screen or the raw history, what it needs, and its new call.

The anchors are line numbers at `b3d0614`, the commit this design was written against. Master has moved
since (the fixes in §8.1 added screen readers and shifted every line), so step 3 re-derives this table on
its branch head: every read of `session.screen` (`display`, `cursor`, `history`, `buffer`), every
`get_clean_screen_text`, `get_scrollback_text` and `raw_history` use, by name, not by line.

| consumer | anchor | needs | new call |
|---|---|---|---|
| terminal WebSocket (console) | `server.py:1308`, `static/js/session-workspace.js:1079` | replay + live raw chunks with Q11 atomicity, input, resize | no Python consumer: the console fetches a ticket from the hub and connects to the core's socket directly (§6.4) |
| hub-side terminal bookkeeping (session events, `last_output_at`, liveness, exit) | `session_manager.py:2336`, `:2562`, `:2713` | counters and the exit code, not text | one `drain()` per 50 ms returns the dirty sessions' counters and the exit code; there is no Python subscription |
| `read_subagent_screen` (MCP) | `server.py:631` | exact screen, ≤80 lines | `screen` |
| screen-based approval / dialog / composer detection | `:7133`, `:7392`, `:10220`, `:10260`, `:10390`, `:10414`, `:10461`, `:10739`, `:11796`, `:11829`, `:12656` | exact screen, `max(38, rows)` lines | `screen` |
| Codex empty-composer check | `:10209` | `cursor.x`, cols, rows | `screen` reply carries cursor and size |
| typed-text settle before Enter (`_await_typed_text_settled`, `_codex_composer_shows_whole`; master `0e17c54`) | not at `b3d0614` | screen 40 lines every 0.1 s, for up to 120 s while the composer keeps changing; Codex's scroll mark `↑` (U+2191) between spaces as text | `screen`; `rev` may gate the re-read, the verdict stays on the text |
| OMP approval box paired with its tool call (`_omp_box_shows_call`, `_register_omp_screen_approval`, `_omp_box_left_to_events`, `tool_call_for_approval_box`, `screen_command_clipped`; master `6276c63`, `c527749`) | not at `b3d0614` | exact box rows: the command is compared with the transcript's call with whitespace removed, so a wrapped row must render exactly as pyte renders it | `screen` |
| Kimi banner session id, Codex "is archived", Claude spinner | `:8742`, `:8273`, `:9667` | screen, 35 lines | `screen` |
| generic-engine TUI tool lines | `:2345` | every changed visible row | `watch_screen` snapshot (informational only) |
| exit snapshot for `final_response` | `:2305` | scrollback 80 lines, once | `scrollback` in the drain handler that sees `alive = false` |
| `persist_session` scrollback text | `:4369` | scrollback 250 lines | `scrollback` |
| `_last_screen_text` / archive `last_screen` | `:4382`, `:4526` | screen 80 lines → last 20 non-empty | `screen` |
| `_scrollback_blob` | `:4437` | joined raw history, already trimmed, plus a revision | `raw` (trimmed core-side, `rev` from core) |
| submit diagnostics | `:10193` | raw ANSI tail, 65536 characters | `raw_tail` |
| readiness banner may have scrolled | `:10625` | scrollback N rows | `scrollback` |
| "did the task echo / is it quiet" wait | `:12643`, `:12656` | `last_output_at`, screen 15 lines | the drain record's `last_output_at` + `watch_screen` |
| `_crash_tail` | `:10861` | last screen | `screen` |
| `is_process_alive` (B9) | `:2562` | process alive, not status | `alive` in the drain record, no syscall |
| `terminate` | `:2579` | scrollback snapshot, tree kill | `raw`, then `kill` (job object) |
| `to_dict["pid"]` | `:2709` | pid | `spawn` reply |
| `resize` | `:2486` | PTY + screen resize | `resize` |
| `write_input`, live message injection | `:2463`, call sites `:6718-10759` | ordered keystrokes | `write` |
| DSH virtual session | `:2188`, `:2213` | screen fed from text, no PTY | `attach_text` + `feed` |
| archived screen / scrollback reads | `server.py:669`, `server.py:1032` | the DB row only | unchanged; its input is `raw`/`screen` at persist time |

## 5. Interface between the core and Python

### 5.1 The module and its native threads

One PyO3 extension module, imported by `server.py` at start and held for the hub's lifetime. There is no
subprocess, no named pipe, no protocol framing and no version handshake: a call is a call, and a reply is
a Python value built by the core.

```
hub_terminal_core.pyd (cdylib; kernel32 for ConPTY/pipes/jobs, bcrypt for the ticket key)
  Core(...)                    one object per hub; owns the session table, the WS listener, the ticket key
  reader thread per session    ReadFile on the ConPTY output pipe: strip -> decode -> parse -> ring -> fan out
  ticker thread                every 5 ms: flush due batches; at most every 50 ms: one Python wake
  ws accept thread             one loopback listener; the handshake and the ticket check happen here
  ws thread per connection     one per attached console socket (blocking send/recv, so a slow pane stalls itself)
```

- Created in `Core::new(notify, log, opts)`: `notify` is the one callable the core calls at most every
  50 ms (§5.3), `log` is called on spawn, kill and panic only (§6.5). Nothing else crosses the boundary
  without Python asking for it.
- The module is a required import: `server.py` fails at start with an actionable message when it is
  missing (§7.3). There is no fallback implementation to select at runtime.
- Every argument and reply is a plain Python type (str, int, bytes, dict, list of tuples). The core never
  returns a reference to internal state: `screen`/`scrollback`/`raw` build the string on the native
  thread and hand over one `str`.
- Errors: an unknown session id raises `KeyError`; a spawn failure raises `RuntimeError` with the
  Win32 message appended, which the hub turns into the same `RESUME_FAILED_PREFIX`/启动失败 path as
  today.

### 5.2 Calls from Python

Every call that can block releases the GIL first (`Python::detach` in PyO3 0.30, which replaced
`allow_threads`) and waits on the core's own state with a deadline. A call that misses its deadline
returns the last cached value and leaves the session `degraded`; `degraded` is itself a dirty flag, so the
next drain reports it without waiting for new output, and the next call that gets its lock again clears
it. A call that cannot do its work inside the deadline reports that instead of a partial success: `write`
raises `TimeoutError` with nothing queued. `spawn`'s 2 s is a gate, not a timer — `CreateProcessW` cannot
be interrupted once entered — so the selftest measures 20 `cmd.exe` spawns and fails the release when p99
exceeds 2 s, a spawn over the bound is logged with its measured time, and the reply carries `elapsed_ms`.

| call | arguments | reply | deadline | replaces |
|---|---|---|---|---|
| `spawn` | session_id, argv, cwd, env, cols, rows, flags, seed_history, ws_prefix | pid, cols, rows, elapsed_ms | 2 s, measured by the selftest (not interruptible) | `PtyProcess.spawn` (`:2262`), `push_ws_frame` of the resume banner (`:2271`) |
| `attach_text` | session_id, cols, rows, flags, seed_history, seed | ok | 50 ms | `HubTerminalScreen` for DSH virtual sessions (`seed_history` is A1's archived scrollback, `seed` the separator frame) |
| `feed` | session_id, text | rev | 50 ms | `_on_pty_output` for virtual sessions |
| `write` | session_id, text | bytes queued: the whole text or nothing; `0` for empty text; `TimeoutError` when the queue is still full at the deadline | 50 ms | `pty.write` (`:2465`) |
| `resize` | session_id, cols, rows | cols, rows; `ValueError` when the pseudoconsole is already closing/closed, `TimeoutError` when the console is busy past the deadline | 50 ms | `pty.setwinsize` + `screen.resize` (`:2490`) |
| `kill` | session_id, grace_ms | exit_code (None when the process outlived the budget) | 2 s for the whole call: the exit wait, then the same teardown the reaper runs (console close, wait for the reader's last bytes), which publishes on its deadline at the latest | `taskkill /T /F` (`:2593`, `:5680`) |
| `screen` | session_id, max_lines | text, cursor, cols, rows, rev | 50 ms | `get_clean_screen_text` (`:2499`) |
| `scrollback` | session_id, max_lines | text, rev | 50 ms | `get_full_scrollback_text` (`:2516`) |
| `raw` | session_id | text, bytes, rev | 50 ms | `"".join(raw_history)` (`:4454`) |
| `raw_tail` | session_id, max_chars | text | 50 ms | `raw_ansi_tail` (`:10194`); a character count, because the hub's tail is `"".join(raw_history)[-65536:]`, and it is cut on a character boundary |
| `watch_screen` / `unwatch` | session_id, max_lines, interval_ms | ok | 50 ms | generic-engine tool-line polling (`:2345`), now bounded by `interval_ms` |
| `ws_ticket` | session_id, ttl_ms | url, expires_at | 1 ms | nothing (the ticket is new, §6.4) |
| `close_sockets` | session_id | count | 50 ms | `close_ws_frontends` (`:2415`, W7: normal close, code 1000) |
| `snapshot_all` | max_lines, max_bytes | per-session dict | whole 8 s budget | graceful shutdown (`graceful-budget`) |
| `kill_all` | grace_ms | ok; `TimeoutError` when the session table could not be snapshotted inside the budget | 2 s for the whole set after `snapshot_all`, every session in parallel | shutdown (`shutdown-ends-cli`) |
| `stats` | — | counters (GIL released, table read bounded) | 5 ms | diagnostics; the 50 ms drain is the normal reader |

The write queue's cap is 1 MiB (`CoreOptions::write_queue_bytes`). The cap bounds what *waits* in the
queue, not what one call may hand it: a text larger than the cap is accepted whole when the queue is
empty, so a paste bigger than 1 MiB behaves as it did under pywinpty, and only a second write that
arrives while the first is still queued gets `TimeoutError`.

`flags` for `spawn`/`attach_text`: `strip_esc3j_on_output` (true when `resume_count > 0`),
`strip_osc_color_queries` (always true today), `history_rows` (3000), `raw_cap_bytes` (512 KiB),
`raw_slack_bytes` (64 KiB). `seed_history` sets the raw ring before the first byte of the new process
(today `sess.raw_history = [seed]`, `:4864`) and clears the `ESC[3J` carry. `ws_prefix` is written to
every socket already subscribed to that id before the reader thread starts, which is where A1's "between
spawn and first output" ordering stops depending on Python's timing. The sinks are the **id's**, not the
new `Session`'s: `Session::start` takes the predecessor's fan-out over, so the prefix goes to the sockets
that were attached before the resume, in stream order after the predecessor's last flushed batch and its
`superseded` mark (`take_over`, §5.4). `attach_text` does the same with its `seed`.

`subscribe`/`unsubscribe` are gone: no Python object holds a terminal queue any more. The console
attaches straight to the core's socket (§6.4), and `close_sockets` is the only fan-out call the hub
still makes (on archive, `:5631`/`:5733`).

### 5.3 How Python learns about output, screens and exits

Python is not on the output path, so there is nothing to deliver per chunk. The core keeps a dirty flag
per session and a per-session counter record, and the ticker thread makes **one** Python wakeup per
interval, however much output arrived:

- Ticker every 5 ms: flush batches that are due (§5.4) to the core's own sockets.
- At most every 50 ms (the drain interval) and only if any session is dirty, the ticker calls the one
  callback the hub registered, from a thread that attaches to the interpreter just for that call
  (`Python::try_attach` in PyO3 0.30; during interpreter finalization it returns `None` and the tick is
  dropped) and calls
  `loop.call_soon_threadsafe(hub._on_core_tick)`. One wakeup for all sessions.
- `hub._on_core_tick` runs on the loop and makes a single `core.drain()` call, which returns only the
  sessions whose flags were set and clears them: `(session_id, last_output_at, bytes_total,
  chunks_total, screen_rev, alive, exit_code, dropped_bytes, screen_text)` — `screen_text` only for a
  watched session whose `screen_rev` changed.
- Exits are detected on the process handle, not on the output pipe. The write end of the console's
  output pipe belongs to conhost, which lives as long as we hold the HPCON, so a session that exits by
  itself never ends the reader's read. Each session therefore has one reaper thread (512 KiB stack: its
  teardown ends in the `log` callback) blocked in `WaitForSingleObject(process, INFINITE)`; on the signal
  it records `GetExitCodeProcess` (a failed query logs and leaves `exit_code = None`) and runs the **one**
  exit teardown, the same one `kill` runs: take the pseudoconsole with the close deadline,
  `ClosePseudoConsole` on a helper thread, wait for the reader's end of output inside the remaining
  budget, then publish — or publish on the deadline, because a wedged close must cost the last bytes,
  never the record (§12.7). The console slot is three-state (`open`/`closing`/`closed`), never "the
  handle is gone, so it is closed", so a second closer that finds another one inside
  `ClosePseudoConsole` waits for that completion instead of publishing over a live read. The death
  record (`alive = false`, one dirty record, `urgent`, so the ticker notifies on its next 5 ms tick)
  therefore never precedes the session's last byte: the hub sees a process die within 5 ms and asks for
  `scrollback(80)` in the same handler, exactly where `_finish_read` does it today (`:2305`).
- A code learned after the death record went out — a crashed session the hub drained and then killed,
  or a late `GetExitCodeProcess` — marks the session dirty and pending, so the next drain carries it;
  the drain record stays the hub's only exit-code source. `kill` reads the code before
  `TerminateJobObject`, because a process that has exited but is still terminating can have its code
  replaced by the job's termination code.

Note that Windows' Proactor loop has no `add_reader` (`NotImplementedError`, verified on CPython 3.14.7),
so a self-pipe cannot be used to wake the loop; `call_soon_threadsafe` is the only mechanism, which is
why it must be one call and not one per session.

Cost at 50 printing sessions:

| quantity | today | after |
|---|---|---|
| output chunks/s through Python | ~3,600 (50 × 72) | 0 |
| Python wakeups/s from the terminal path | ~19,000 (projected) | ≤20, all sessions together |
| GIL holds for terminal work | one per chunk per socket | ≤20/s, each ≈40–60 µs for a 50-record drain → ≤1.2 ms/s |
| screen rebuilds for polling readers | 1 per reader per poll (`screen.display` per call) | 0 unless watched; a watched session costs one render per changed rev, on a native thread |

`last_output_at` keeps its meaning (unix seconds of the last byte) and is what
`to_dict["idle_seconds"]` (`:2713`) and the "did it echo / is it quiet" wait (`:12643`) read, so both
become reads of the drain record instead of thread-shared fields.

### 5.4 Coalescing and backpressure

Per session the core appends output to a batch and flushes it as **one** frame per attached socket when
any of: 32 KiB accumulated, 20 ms since the batch's first byte, or 5 ms of silence after at least one
byte. That caps a session at 50 frames/s per socket while a busy session (≈72 chunks/s today) still
delivers every byte in order, and it is the only batching in the system (owner decision 7: the
Python-side batching unit does not land).

**Delivery state belongs to the session id.** The delivery lock, the sink list and the handle counter
are one structure carried across a resume (`SinkFanout`, `session.rs`'s `fanout` field): a socket that
attached before a resume keeps its registration, its handle and its replay stream, and `detach_sink(id,
handle)` works whichever incarnation registered the handle (§6.4 A1). Every frame and every registration
happens under that lock, so frame order and Q11's atomic attach are one rule rather than two: a frame is
taken from the batch and handed to the sinks inside it, and `attach_sink` takes its replay snapshot,
registers the sink and hands back the batch it took in one gap between two deliveries. Lock order is
one-directional: the id's delivery lock, then a session's text lock; the sink list only under delivery.

`FrameSink::deliver` runs on the session's reader thread under that lock, so it may not block and may not
write a socket itself: it pushes onto the socket's own bounded queue (`FrameQueue`), and the socket's own
thread writes the replay and `replay_end` before the first queued frame (§6.4). One sink's panic costs
that sink and nothing else: `deliver` is wrapped in `catch_unwind`, the panicking sink is detached and
counted in `stats.panics` and logged once with its handle, the other sinks still get the frame, and the
session keeps running. No lock in the delivery path skips its work on poison, so `attach_sink` can never
return a handle it did not register.

One frame bypasses the batch: the resume separator (`spawn`'s `ws_prefix`, `attach_text`'s seed), which
must land between the predecessor's last bytes and the new process's first. `Session::push_frame`'s
precondition is therefore a state, not a caller's promise: the incarnation's reader has not started, no
batch is pending, and the caller holds the id's delivery lock — all three asserted in debug builds, and
only the handover (`take_over`) satisfies them (§6.4 A1).

`output.text` is already W8-stripped, OSC-stripped and decoded before it enters a batch: the core owns a
per-session UTF-8 decoder with up to 3 carried bytes, so a multi-byte character split across two ConPTY
reads stays intact — the same guarantee `pty.read()` gave Python.

A frame is dropped only when that socket's own queue is full (2 MB of unsent frames): the oldest whole
frames go, the drop is counted in that connection's `dropped_bytes`, and the count appears in the next
drain. This is the failure mode today's `put_nowait` on `asyncio.Queue(maxsize=500)` has, except that it
is counted and a reconnect replays history. A socket that has not accepted a byte for 30 s is closed
(code 1000) instead of pinning a thread and a 2 MB ring forever.

### 5.5 Threading

| today | after |
|---|---|
| pywinpty reader thread + loopback socket per session | 1 blocking ConPTY read thread per session in the core (native, no GIL, no socket) |
| `_read_loop` thread per session | gone |
| `call_soon_threadsafe` per chunk per socket | ≤1 per 50 ms for the whole hub (§5.3) |
| console `/ws/pty` sender task per socket (uvicorn + asyncio) | 1 core thread per console socket, no asyncio, no uvicorn |
| `taskkill` process spawn per terminate | `TerminateJobObject` on the session's job |
| ~5 hub threads per session (winpty reader, socket, asyncio task, accept, cleanup) | hub: 0 per session; core: 1 reader + 1 socket thread per attached console pane, plus 2 fixed (ticker, accept) |

The core's readers are native threads that spend their life blocked in `ReadFile`, so they cost no
scheduling. An optional later backend (overlapped pipes on a single IOCP) would collapse them, and is
worth adding only if the load test shows thread count, not CPU, is the limit.

### 5.6 What Python no longer does, per chunk

- **Parse**: ≈3,600 chunks/s at 50 sessions, a few µs of work each, all on native threads. Python's cost
  per chunk is zero: nothing in `_on_pty_output` (`:2325`) survives at the chunk rate; the generic-engine
  tool-line scan (`:2345`) moves to a `watch_screen` snapshot bounded by its interval.
- **History**: the append, the byte count, the slack check and the joined-buffer trim happen inside the
  core; Python reads a trimmed snapshot every 60 s (`RESUME_SCROLLBACK_INTERVAL_SEC`) with the core's
  `rev`, so the existing "changed?" gate in `_scrollback_blob` keeps working unchanged.
- **Fan-out**: zero Python objects, zero schedules. The core writes each console socket itself.
- **Polling**: `screen` calls happen on the loop at a few Hz per session and cost one render plus one
  `str`, the same class as today's in-process `screen.display` rebuild. Informational readers use
  `watch_screen`, whose staleness bound (≤ interval) is stated at the call site.

## 6. The chosen design: `hub_terminal_core`, a PyO3 extension

The owner chose in-process hosting on 2026-11-12 (decision page `hub-rust-core-1112`): the terminal core
is an extension module inside the hub process, it owns the ConPTYs, it parses on its own threads, and it
serves the terminal WebSockets from its own loopback listener. It replaces pywinpty and pyte outright —
no flag, no shadow mode, no fallback path. Rollback is reverting the merge.

### 6.1 Shape

```
hub process (CPython 3.14.7)
├─ asyncio/uvicorn loop ── /mcp, REST, /ws/console, every parser of screen text
│    └─ tick()  ← one call_soon_threadsafe per 50 ms, which calls core.drain() once
└─ hub_terminal_core.pyd   ← imported at start, holds one Core object
     ├─ ticker thread          flush due batches (5 ms), notify Python (≤20/s), report exits
     ├─ reader thread × N      ReadFile → strip → UTF-8 decode → parse → raw ring → fan out
     ├─ ws accept thread       127.0.0.1:<ephemeral>, handshake, ticket check
     └─ ws thread × M          one per attached console socket: replay, live frames, input
```

Python owns policy; the core owns bytes. A terminal pane loads without the hub touching a single PTY
chunk: the browser fetches a ticket from the hub, then speaks to the core's listener directly.

### 6.2 Correcting the earlier claim

The first version of this document said an in-process extension "can never remove the hub's own event
loop from the output path". That was wrong, and the rest of this section depends on the correction. A
PyO3 extension is ordinary native code loaded into the process: it may start any number of OS threads,
run them without the GIL, block in `ReadFile`, parse, and accept and write sockets. The GIL is the lock
Python takes to execute Python bytecode; nothing obliges the extension to take it per chunk. What an
extension cannot do is run *Python* off the loop — and after this change there is no Python on the output
path to run. It also cannot survive the process dying, which is what §6.3 is about.

### 6.3 Panics, aborts, and what the owner accepted

- `panic = "unwind"` for the extension's profile. The desktop app sets `panic = "abort"`
  (`desktop/src-tauri/Cargo.toml`, `[profile.release]`), so the core crate must not be built as a member
  of that workspace: it is its own workspace with its own release profile (§7.3).
- A Rust panic that unwinds out of a `#[pymethods]` call becomes a Python `PanicException` (unverified,
  from memory of the PyO3 docs; the design only depends on the panic not corrupting the interpreter).
- The failing item costs its item, never the loop. A panic in a reader thread ends **that session only**:
  the core drops the pseudoconsole, keeps the screen and scrollback, sets `alive = false`,
  `exit_code = None` and `crashed = true`, and the hub archives it through `classify_end_reason`
  (`:2312`) the way it archives a failed exit today. The console shows a crashed pane, not a frozen one.
  A panic in the ticker thread is caught and logged and the thread continues; a panic on the accept thread
  refuses that connection and keeps the listener. A panic from a `FrameSink::deliver` costs **that sink**:
  each delivery is wrapped in `catch_unwind`, the sink is detached, counted in `stats.panics` and logged
  once with the session id and its handle, the other sinks still get that frame and every later one, and
  the session's reader, screen and scrollback are untouched (§5.4).
- An error *returned* by one item is handled the same way as a panic: it costs that item, never the
  loop. A socket write that fails closes that socket; a frame a sink cannot take is dropped and counted;
  an accept that fails is logged and retried (§6.4). Only the reader's own end of pipe ends a reader, and
  only `shutdown` ends the ticker and the accept thread. A `?` that leaves a native thread's loop on any
  other error is a bug. Master `3874299` is the Python case of this: one tool output too large to store
  raised out of the transcript watcher and ended it for the session's remaining life. The step 3 drain
  handler isolates sessions the same way: an exception while handling one session's record (a scrollback
  write that persistence refuses, a broken archive) is logged against that session and the drain goes
  on with the next.
- Residual risk the owner accepted with in-process hosting: `abort()`, a stack overflow, an access
  violation, or a second panic while unwinding takes the **whole hub** down — every session, every MCP
  stream, every wait handle. Any dependency compiled with `panic = "abort"` restores exactly that
  failure mode, so the profile is asserted in CI (§10). There is no supervisor to respawn the core; the
  desktop app's job object still takes the agent trees down with the hub, which is the recorded
  `shutdown-ends-cli` behaviour.

### 6.4 The terminal WebSocket server

**Listener.** The hub binds `127.0.0.1` only (`server.py:1483`); there is no `0.0.0.0` bind, no
remote-console path and no proxy in the tree. Its port comes from `HUB_PORT` (default 4444,
`session_manager.py:830`), which the desktop app picks from 4444–4544 (`hub_config.rs:158`) and passes as
`HUB_PORT` (`owned_hub.rs:146`). The core mirrors the *address policy*, not the port: it binds
`127.0.0.1:0` and reports what it got (`Core.ws_port`), so two hubs (4444 and 4445) cannot collide,
there is no retry loop and no "port + 1" guess. A loopback bind raises no Windows Firewall prompt.
Reaching the console from another machine is a separate decision — a non-loopback listener would need
its own TLS and ticket story, and until then the listener stays on `127.0.0.1`.

**A failed accept never closes the listener** (master `4b06139`). On Windows an accept fails when a client
drops its connection mid-accept (WinError 64, `ERROR_NETNAME_DELETED`); asyncio's Proactor loop then
closed the hub's listening socket, and the hub kept serving open connections while refusing every new one
until `windows_accept_guard` was added. A Rust accept loop written as `for s in listener.incoming() { s?; … }`
or one that `break`s on `Err` has the same bug. The core's accept thread treats every `Err` from `accept`
as that one connection's failure: it logs the first and every 100th, waits 0, 50 ms, 200 ms, then 1 s
between consecutive failures (the guard's `RETRY_DELAYS`), and accepts again; only `shutdown` ends it.
The hub's health check includes the listener: `Core.ws_listening()` is false when the accept thread has
ended, and the hub reports that the way the desktop app now reports a hub whose port stopped listening.
Step 4's test injects accept failures through a `test-hooks` seam and proves the next connection is
served.

**Ticket.** `core.ws_ticket(session_id, ttl_ms=30_000)` returns
`ws://127.0.0.1:<port>/ws/pty/<session_id>?ticket=<token>`; the token is
`nonce || base64url(hmac_sha256(key, session_id ‖ 0x00 ‖ expiry_unix ‖ 0x00 ‖ nonce))`. `key` is 32
bytes from `BCryptGenRandom`, one per hub process, held only inside the core — which is why the core mints
the ticket instead of the hub, and why the sidecar design's `hello` handshake is no longer needed at all.
The hub exposes `GET /api/sessions/{session_id}/terminal-ticket` → `{"url", "expires_in"}`. The handshake
verifies the MAC, the expiry and that the ticket's session id equals the path's, and remembers the nonce
until expiry so a ticket is single use. A session that has already ended 404s at the hub before a ticket
is minted, keeping the console's existing not-found path.

**Frames and semantics.** The core reproduces `/ws/pty`'s contract, because both the console JS and
`test_hub_persistence.py:276` parse it:

- Replay: on connect the core sends the *ring as it stands* — `join(raw_history)`, already collapsed by
  the in-loop W9 trigger when it fired, and **not** the 512 KiB `_scrollback_blob` trim — followed by one
  more `OSC_COLOR_QUERY_RE` pass over the joined text (`attach_ws_queue`, `:2426-2437`; the per-chunk
  strip at `:2333` cannot see a query split across two chunks, so this second pass is what removes it),
  then `{"type": "replay_end"}`. `isReplayEndFrame` (`static/js/session-workspace.js:33`) already
  accepts a server that sends the marker, and the 1.5 s fallback timer covers one that does not, so no
  JS change is needed for the marker itself.
- Q11 atomicity lives in the core: one session mutex covers "append to the raw ring + publish to
  subscribers" and "snapshot the replay + register this socket", so a chunk is either inside the replay
  or in the socket's queue, never both and never neither. There is no Python lock left to race.
  Concretely, the id's delivery lock serialises every fan-out, and `attach_sink` holds it across the
  replay snapshot, the registration and the hand-over of the batch it took; the replay it returns
  therefore continues exactly where the frames it will receive begin. The sink must write the replay and
  `replay_end` **before** draining its own queue (a direct write from `deliver` would put a frame on the
  wire ahead of the replay), which is what §5.4's sink contract requires.
- A1 (a resumed session keeps its sockets): sockets attach to the **session id**, not to a Python
  object, so a resume changes nothing for them — `adopt_ws_frontends` (`:2446`) and
  `register_ws_closer` (`:2410`) disappear. The delivery lock, the sink list and the handle counter are
  one `Arc`-shared structure that each new incarnation of an id takes over from its predecessor, so
  attached sinks and their handles survive the swap and `detach_sink(id, handle)` still finds them. The
  resume banner travels as `spawn(ws_prefix=…)` and is written to every attached socket before the reader
  thread starts; the DSH virtual resume passes it as `attach_text(seed=…)` (`:5317`). The handover runs
  under the delivery lock: the predecessor's pending batch is delivered to the sinks, the predecessor is
  marked `superseded` (anything its reader still files is dropped and counted, never written after the
  banner), the successor becomes the id's replay source, and then the banner goes out. A sink attached
  during that swap is kept exactly once: it lands either fully before the handover (its replay includes
  the old bytes and the banner) or fully after it.
- W8 on resume: `spawn(strip_esc3j_on_output=true)` drops `ESC[3J` with the same 3-byte carry.
- Input: `{"type":"input","data":…}`, `{"type":"resize",…}` and bare text are handled as today. OSC
  terminal replies are stripped in the core with `OSC_TERMINAL_REPLY_RE`'s pattern (`:1853`) before any
  input reaches the PTY. A resize is **not** applied by the core: it raises a `resize` dirty flag with the
  requested cols/rows, and the hub applies the 20/5 clamp and the single-window propagation
  (`resize_session_from_client`, `:3161`) on its next tick, because propagation is policy. Worst case
  that is one drain interval (50 ms), well inside what a pane refit already costs.
- Slow client: per-socket 2 MB ring, oldest whole frames dropped and counted (§5.4); a socket that
  accepts nothing for 30 s is closed with code 1000. Today's `asyncio.Queue(maxsize=500)` drops silently;
  the new count is visible in `stats`.
- permessage-deflate: uvicorn's default offers it (`ws_per_message_deflate: bool = True` in the installed
  uvicorn 0.42.0 config source; the lock pins 0.54.0, unverified there), so today's frames are
  compressed. The core starts **without** deflate: the link is loopback, so bandwidth is free, while
  compression costs CPU on both ends for a stream the browser re-parses anyway. `tungstenite`'s deflate
  support (unverified) keeps the option open if §11 shows a difference worth having.

**Console JS changes.** `hubApi.ptySocketUrl(id)` (`static/js/hub-api.js:79`) becomes asynchronous: it
fetches `/api/sessions/{id}/terminal-ticket` with a bounded timeout, then opens the returned URL.
Everything else stays — replay buffering, the marker, reconnect backoff (`:1129`), the reply-drop window.
A failed ticket fetch takes the existing reconnect path, so a restarting hub looks exactly as it does
today.

### 6.5 GIL rules and the event loop

- Every `pymethod` that can block (spawn, kill, screen, scrollback, raw, drain, `snapshot_all`) releases
  the GIL for its duration (`Python::allow_threads`).
- The core holds exactly two Python callables: `notify`, called by the ticker only (≤20/s, plus one per
  session end), and `log`, called on spawn, kill and panic only. Neither ever runs on a per-chunk path,
  and neither is called while the core holds a session mutex. Python may therefore hold the GIL for a
  long time without delaying PTY reads, and the core may hold its own locks without blocking the loop.
- Lock ordering is one-directional: Python may hold a Python lock and then call into the core; the core
  never takes a Python lock except through those two callables, which it calls with no core lock held.
  That removes the classic deadlock (Python lock → core lock while the core waits for the GIL to call
  back).
- Deadlines (§5.2) mean a wedged reader thread costs one bounded call, not a hung loop; the call returns
  the cached snapshot and marks the session `degraded`.

### 6.6 Lifecycle and shutdown

- Sessions end with the hub by construction: the module and its threads die with the interpreter, so
  `shutdown-ends-cli` needs no supervision code — but it does need ordering, or agents are left with a
  dead MCP endpoint. Shutdown keeps the recorded order (`graceful-budget`): stop producers,
  `snapshot_all(max_lines=250, max_bytes=512 KiB)` per live session, `kill_all(grace_ms=2000)` — every
  session terminated in parallel inside one 2 s budget, `TerminateJobObject` first and
  `ClosePseudoConsole` on a helper that may outlive the call — then drain persistence inside the shared
  8 s budget. `snapshot_all` returns what `persist_session` archives today (`:4369`), so the rows do not
  change.
- Interpreter exit does not kill sessions by itself: the per-session job objects do. Each session holds a
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job, so the CLI trees die when the interpreter's handles close even
  if `kill_all` never ran (a crash, a `taskkill`), which is the guarantee the desktop app's outer job gave
  pywinpty. Two paths skip rather than wait during finalization: the ticker's `notify`
  (`Python::try_attach` returns `None`, the tick is dropped) and the join in `Core`'s drop (the ticker is
  left to exit by itself).
- Each session gets its own job object at spawn (`CreateJobObjectW` +
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` + `AssignProcessToJobObject`). Inside the desktop app the hub
  already runs in the app's kill-on-close job (`owned_hub.rs:142`), so these jobs are nested; nested jobs
  are supported on Windows 8+ but the outer job's limits must allow it — that is the one-hour experiment
  in §12, and falling back to `TerminateJobObject` on the inner handle when assignment fails keeps
  today's tree kill.
- Core log lines (spawn, kill, panic, listener) go through the `log` callable into `hub.log`, so a crash
  leaves its traceback next to the last screen.

### 6.7 Alternatives considered

| option | why it was not chosen (owner's call, 2026-11-12) |
|---|---|
| standalone sidecar process, two named pipes | its one advantage is crash isolation: a core crash would kill the core's sessions, not the hub. It buys that with a framing protocol, a version handshake, a supervisor, a second process in the payload and a second lifecycle to test. The owner judged a panic in the parser — which `catch_unwind` turns into one crashed session — cheaper than that whole protocol. A sidecar that outlived the hub would also contradict `shutdown-ends-cli` and strand agents with a dead MCP endpoint. |
| core inside the Tauri desktop app | exists only when the app started the hub, and the hub is also run by hand (`Start-AgentHub.ps1`); two hubs (4444/4445) share one app process; and it moves terminal lifetime out of the hub with the same `shutdown-ends-cli` contradiction. |
| keep pywinpty + pyte behind a flag | rejected: no legacy path. Two parsers and two PTY paths would have to be maintained, tested and reasoned about through every future console change, for a rollback that reverting the merge already gives. |

Accepted cost of the choice: a Rust panic domain inside the hub, a compiled extension required in every
run path (from source and portable), and a Python ABI tag on the artifact (§7.4).

## 7. Crates

### 7.1 ConPTY: the system API through `windows` 0.62

| choice | why / why not |
|---|---|
| `windows` 0.62 `Win32_System_Console` (verified in the local registry copy, `windows-0.62.2`): `CreatePseudoConsole(size, hinput, houtput, flags) -> Result<HPCON>`, `ResizePseudoConsole(hpc, size)`, `ClosePseudoConsole(hpc)` | **chosen.** These are `kernel32.dll` imports, i.e. the ConPTY that ships with the OS — no bundled `conpty.dll`, no `OpenConsole.exe`, nothing to carry in the payload. `windows` 0.62.2 is already in `desktop/src-tauri/Cargo.lock`; the console feature is not enabled there yet (`Win32_System_Console` appears in the crate's feature list only, `windows-0.62.2/Cargo.toml:577`). Job objects, pipes, `ReadFile`/`WriteFile`, `CreateProcessW` with `EXTENDED_STARTUPINFO_PRESENT` come from the same crate. |
| `winpty-rs` 1.0.8 | not used. pywinpty 3.1.2 is built on it, and its `.pyd` calls the **bundled** `conpty.dll` (`ConptyCreatePseudoConsole`, `ConptyResizePseudoConsole`, `ConptyClosePseudoConsole`, `ConptyReleasePseudoConsole`, …, verified by scanning the shipped DLL), not `kernel32`. Whether it can be told to use the system API is unverified; the owner's decision is the system ConPTY, so the crate is out regardless. |
| bundled `conpty.dll` / `OpenConsole.exe` | dropped, per the same decision. Consequence for fixtures: today's captures came from the bundled implementation, whose build can differ from the OS's `conhost.exe`/`OpenConsole` in erase/cursor-repaint details, so step 0's fixtures were recorded against a slightly different producer. That is why step 1 re-records them against the system ConPTY and why the golden files are regenerated, not trusted (§9, §10). |
| `portable-pty` | not used: (unverified, from memory) one reader thread per PTY with blocking reads and no overlapped I/O, a winpty fallback the hub does not need, and it hides the job-object control `kill` depends on. |

Spawn sequence in the core, per session: `CreatePipe` twice (input, output) → `CreatePseudoConsole` →
`STARTUPINFOEXW` with `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` → `CreateProcessW` suspended →
`AssignProcessToJobObject(inner job)` → `ResumeThread` → close our copies of the console's ends →
reader thread on the output pipe. `write` is `WriteFile` on the input pipe (one writer thread per session
keeps ordering); `resize` is `ResizePseudoConsole`; `kill` is `TerminateJobObject`, then
`ClosePseudoConsole` on a helper thread with a deadline, because closing a pseudoconsole while a read is
pending is a known source of hangs (unverified specifics).

### 7.2 VT parsing

| choice | why / why not |
|---|---|
| `alacritty_terminal` (its `vte` parser plus `Term`/`Grid`) | **not used for the screen; its parser is.** Step 2 measured it and `Term` cannot reproduce two behaviours the hub's screens depend on: `Term::resize` reflows the grid while pyte's `Screen.resize` anchors content top-left, pads and drops the top rows on a shrink — the live cases resize 120x36 -> 140x44 mid-stream and `screen_at_resize.txt` is the screen right after it — and `Term` keeps a separate inactive grid for the alternate screen while pyte has one buffer (`?1049` only sets a mode bit), which four of the five live recordings switch to. The core therefore takes the crate's parser, `vte` 0.15.0 (pinned in `terminal_stream/Cargo.toml`, the version `alacritty_terminal` 0.26.0 builds on), and models pyte's screen itself. |
| `vt100` | smaller, screen + scrollback + cursor; (unverified, from memory) fewer tested sequences and a screen model that has lost content under rapid resize/alt-screen switching. Acceptable fallback if `Term`'s API fights the history-row semantics; then the parser is `vte` + our own screen model. |
| `termwiz` | correct and rich, used by WezTerm; its `Surface`/`Scrollback` model is display-oriented, so reproducing `history.top` row semantics is more code, and the dependency tree is the heaviest of the three. |

Rendering rule: the core renders rows exactly as `get_clean_screen_text`/`get_full_scrollback_text`
expect them — each row `rstrip`ped, trailing blank rows dropped from the screen, history walked
newest-first and stopped at `max_lines`. Padding cells render as spaces; a wide cell renders once.

### 7.3 Build and packaging

The artifact is one `cdylib` renamed to a Python extension. No wheel, no index, no `requirements.lock`
entry: `python server.py` puts the script's directory first on `sys.path`, so a `hub_terminal_core.pyd`
beside `server.py` is importable in both run paths, and pywinpty leaves `requirements.in` at the same
time.

`terminal-core/` is its own Cargo workspace (`terminal-core/Cargo.toml`), *not* a member of
`desktop/src-tauri`, because that manifest sets `panic = "abort"`, `opt-level = "s"` and full LTO for the
app (§6.3). The core's release profile: `panic = "unwind"`, `opt-level = 2`, `lto = "thin"`,
`strip = "symbols"`, `codegen-units = 1`.

**Toolchain choice.** `maturin` is not installed on this machine (verified: not on `PATH`), so:

| option | what it costs | verdict |
|---|---|---|
| (i) `maturin build --release` | a new tool the owner has to approve and every developer has to install; produces a wheel (`hub_terminal_core-…-cp314-cp314-win_amd64.whl`) that has to be placed in the portable `wheels/` folder or added to the lock, and the venv cache (keyed by a hash of the lock and the interpreter in `bootstrap.ps1`) then has to be invalidated whenever the extension changes | rejected for now: the artifact is not a pip package, and it drags the install path into the release |
| (ii) `cargo build --release` + copy | `terminal-core/target/release/hub_terminal_core.dll` → `<repo>/hub_terminal_core.pyd`. No new tool, no lock entry, no venv rebuild; the same command works for the portable build, which simply embeds the file that already exists | **chosen** |

**abi3 vs per-minor.** `sysconfig` reports the extension suffixes `.cp314-win_amd64.pyd` and `.pyd`
(verified on the hub's 3.14.7), and pywinpty itself ships `_winpty.cp314-win_amd64.pyd`, i.e. one build
per minor. The portable bootstrap accepts CPython 3.11–3.14 (`runtime.json` `python_minors`) and creates
a venv per interpreter, so per-minor artifacts would mean four builds and a selection step. Preferred: one
**abi3** build (`Py_LIMITED_API` at 3.11) named `hub_terminal_core.pyd`, valid for every supported minor.
Whether the current PyO3 release supports the stable ABI up to 3.14 is unverified — that is step 1's first
gate; if it does not, build per minor and let the portable payload carry four files.

**Portable build.** The payload is an explicit manifest: `desktop/portable/payload.txt` lists repo-relative
paths, `build.rs` `embed_portable_resources()` embeds each file into the EXE and hashes them into
`PAYLOAD_ID`, and the app unpacks them to `resources/<PAYLOAD_ID>/` where `server.py` lives
(`portable_runtime.rs:18`). So the `.pyd` joins that manifest (`desktop/portable/hub_terminal_core.pyd`,
produced by the release script before `pnpm build:portable`), lands beside `server.py` in the unpacked
resources, and needs no venv change at all. Two consequences to handle in the release script:
- `desktop/scripts/check-portable-privacy.mjs` scans every payload member for host paths and credentials
  and rejects member names ending `.exe`/`.pdb` (a `.pyd` passes the name rule). A stripped Rust PE can
  still carry a CodeView path, so the `.pyd` needs the same PE check the EXE already gets (`checkPe`) and
  must build with `strip = true`; if the gate trips, that is a build-config fix, not a gate exemption.
- `bootstrap.ps1` `Check-Imports` runs `python -I -c 'import …'`; `-I` implies `-P`, so the script
  directory is *not* on `sys.path`. The check must add the resources dir explicitly
  (`sys.path.insert(0, …); import hub_terminal_core`) or it would report a false failure.

**From source.** There is no fallback: `server.py` imports the module at start and refuses to run without
it. A `terminal_core.py` thin wrapper does the import and turns `ImportError` into one actionable Chinese
message with the exact build command:

```
终端核心未构建：缺少 hub_terminal_core（预期 <repo>\hub_terminal_core.pyd）。
构建：cargo build --release --manifest-path terminal-core\Cargo.toml
      Copy-Item terminal-core\target\release\hub_terminal_core.dll hub_terminal_core.pyd
```

The copy lands in the repo root, and the release script's copy lands in `desktop/portable/`; `.gitignore`
must ignore both (a two-line addition, outside this document's file set). `Start-AgentHub.ps1` keeps using
`$HubDir\.venv\Scripts\python.exe $HubDir\server.py`, so nothing about the launcher changes except that a
missing `.pyd` now stops it with the message above instead of starting a hub without a terminal core.

### 7.4 Crates that need the owner's approval

Versions marked "in lock" are already resolved in `desktop/src-tauri/Cargo.lock` (verified); the others
are new downloads.

| crate | purpose | in `desktop/src-tauri/Cargo.lock` |
|---|---|---|
| `pyo3` (version pinned at step 1) | the extension module, `#[pymethods]`, `allow_threads`, abi3 | no |
| `tungstenite` (version pinned at step 1) | RFC 6455 handshake, frames, ping/pong on the console socket | no |
| `hmac` | HMAC-SHA256 for the terminal ticket (pairs with the `sha2` already in the lock) | no |
| `sha2` 0.10.9 | hash primitive under `hmac` | yes |
| `base64` 0.21.7 | ticket encoding | yes |
| `windows` 0.62.2 (`Win32_System_Console`, `Win32_System_JobObjects`, `Win32_System_Pipes`, `Win32_Storage_FileSystem`, `Win32_Security_Cryptography`) | ConPTY, job objects, pipes, `BCryptGenRandom` | yes (crate present; those features are not enabled in the app's manifest yet) |
| `vte` 0.15.0 (`alacritty_terminal` was approved with it but is not used, §7.2) | VT parsing; the screen model is the core's own copy of pyte's | no |
| `crossbeam-channel` 0.5.17 | bounded writer queue and ticker notifications | yes |
| `regex` 1.13.1 | the two OSC patterns, only run when the buffer contains `\x1b]` | yes |
| `flate2` 1.1.11 | only if §11 makes permessage-deflate worth enabling | yes |

Deliberately not used: `serde`/`serde_json` (there is no wire protocol any more), `parking_lot` (std
mutexes are enough), `tokio` (no async runtime in the core), `unicode-width` (the parser's grid owns cell
widths), `winpty-rs` and `portable-pty` (§7.1).

## 8. Semantics that must survive migration

| invariant | where it lives now | how the core keeps it |
|---|---|---|
| Q11: a new WebSocket sees every chunk exactly once (replay/register atomic) | `attach_ws_queue` `:2425` under `_lock` | the id's delivery lock covers "append to the ring + publish" and "snapshot the replay + register the socket" (§5.4); there is no Python lock left to race (§6.4) |
| A1: a resumed session keeps its sockets, and the resume separator lands between spawn and first output | `adopt_ws_frontends` `:2447`, `push_ws_frame` `:2458`, `:2270` | sockets attach to the **session id**: the delivery lock, sink list and handles are one structure a new incarnation takes over, so a resume changes nothing for them; the separator arrives as `spawn(ws_prefix=…)` written under that lock, before the reader thread starts, after the predecessor's batch |
| A1: a superseded incarnation never writes after its successor's banner | `adopt_ws_frontends` re-points the queue; the old `_read_loop` keeps running until EOF | the handover marks the predecessor `superseded`; a frame it would still deliver is dropped and counted in its `dropped_bytes`, logged once |
| A1: `raw_history` is seeded with the archived scrollback on resume | `:4864` | `spawn.seed_history` |
| W8: `ESC[3J` (erase saved lines) is dropped on resumed sessions only, with a split-sequence carry | `_strip_saved_lines_erase` `:2390` | same algorithm in the core, `strip_esc3j_on_output` flag, per-session carry ≤3 bytes |
| OSC 10/11/12/4 colour queries never reach the console or the history | `OSC_COLOR_QUERY_RE` `:1852` `:2334` `:2437` | same regex in the core, applied before append and before replay |
| OSC replies from the browser never reach the PTY | `strip_osc_terminal_replies` `:1856`, used in `ws_pty_endpoint` | moves into the core: it owns the socket that receives the input, so it strips with the same pattern before `write` (§6.4) |
| W9: the raw ring is trimmed to ≤ 512 KiB when it passes 512 KiB + 64 KiB of slack, and `_scrollback_blob` trims the same way on read: cut at a line break, `[较早的输出已截断]` prefix; the newest `── 已恢复（第 N 次）──` banner line is kept whole, with its leading `\r\n`, whenever the budget's own cut (before the move to a line break) falls before the end of that line; a cut inside the line therefore leaves the history over the cap by less than the line (`HISTORY_BANNER_LINE_MAX_BYTES` = 128 bounds what counts as the banner line), and a banner that ends before the cut is dropped. Fixed in Python before the port (`oracle-bugs`, owner, 2026-11-13); until then a cut inside the banner line dropped it | `trim_history_text` `:633`, `_on_pty_output` `:2358-2370`, `_scrollback_blob` `:4437` | byte-identical Rust implementation; the in-loop collapse, the prefix and all three banner outcomes are golden fixtures (§10) |
| history sizes: screen `history=3000`, `snapshot_all`/`scrollback` caps | `:2157`, `:4369`, `:2516` | `spawn.flags.history_rows` |
| K4: one unparseable sequence costs only itself | `HubTerminalStream` `:1832`, `HubTerminalScreen` `:1792` | `vte` cannot fail; `catch_unwind` around the feed so a handler bug costs one chunk, not the session |
| A CSI whose parameter bytes include `:`, `<` or `=` is consumed whole and draws nothing. pyte 0.8.2 ends a CSI at the first byte that is not a digit, `;`, `?`, a space or `>`, so kimi's and omp's `ESC[<u` and agy's `ESC[=5;1u` used to draw `u` and `5;1u` as text; fixed in Python before the port (`csi-residue-fix`), with a carry for a sequence split across chunks. `ESC[?2026$p` is consumed whole too, `$` and the byte after it | `HubTerminalStream.feed`, `_split_marker_csi` | `vte` reads these bytes as one sequence already, so the core needs no rewrite of the stream (step 2's `csi_marker.rs` is deleted in step 2c); raw history and frames keep the bytes |
| Inside a CSI, an ESC abandons the sequence and starts the next one, a C0 control runs where it arrives and the sequence goes on, and DEL is ignored. pyte took each of the three for the final byte and drew the rest as text (`A ESC[31 ESC[1m B` showed `A[1mB`); fixed in Python before the port (`csi-esc-restart`) | `_split_marker_csi` | `vte` reads them this way already; `edge-screen-csi-cancel` and `edge-screen-csi-controls` pin it |
| A zero-width character that is not combining (ZWSP, ZWJ, VS16) takes no cell, starts no pending wrap, and the text after it is drawn; pyte dropped the rest of the run. Fixed in Python before the port (`zero-width-skip`) | `HubTerminalScreen.draw` | `screen.rs` skips it before the wrap check (step 2c); `edge-screen-zero-width*` pin it |
| The four screen behaviours step 2 got wrong (DECSTBM off by one and its one-parameter form, DECOM not homing, restore clearing modes set after the save, a combining mark repeating its base) | pyte 0.8.2 `screens.py` `set_margins`, `set_mode`/`reset_mode`, `restore_cursor`, `draw` | pyte is right here; the core copies it row for row since step 2c, and `edge-screen-*` cases pin each one |
| pyte NFC-composes a cell (`unicodedata.normalize("NFC", base + mark)`); the core stores the base plus its marks unnormalised, because exact NFC needs `unicode-normalization`, a dependency the owner has not approved | `HubTerminalScreen.draw` vs pyte's `draw` | `terminal_stream/tests/fixture_corpus/screen_exceptions.jsonl` carries exactly those 8 rows and a stale row fails the gate; drop them when the crate is approved or pyte goes at step 6 |
| B9: liveness is the process, never the status string | `is_process_alive` `:2562` | `alive` in the drain record, set false by the reader thread; no syscall and no round trip, and `to_dict["idle_seconds"]` reads the same record |
| D7/D1–D13: approval keys are byte-faithful | `write_input` `:2463`, call sites `:6718-7276` | `write` preserves arrival order per session through one writer thread; no interpretation, no translation |
| graceful shutdown ends every CLI and captures its scrollback inside the 8 s budget | `graceful-budget`, `:5671`, `:5082` | `snapshot_all` then `kill_all`, both with deadlines; on timeout the hub logs and archives what it has |
| scrollback is written at most every 60 s, only when changed | `_scrollback_blob` `:4443` | `raw.rev` replaces `raw_history_rev`; `_scrollback_written_at`/`_scrollback_rev_written` logic unchanged |
| `last_screen` = last 20 non-empty screen lines | `_last_screen_text` `:4379` | `screen` reply, same trimming in Python (policy stays there) |
| viewport clamp (cols ≥ 20, rows ≥ 5) and single-window propagation | `:2487`, `:3161` | a console `resize` arrives as a dirty flag on the drain record; Python applies the clamp and propagation, then calls `resize` per session. The core refuses cols < 20 / rows < 5 defensively and never propagates |
| DSH virtual sessions have a screen without a PTY | `start_virtual` `:2188`, `emit_virtual_ansi` `:2213` | `attach_text` + `feed`; the echo emulation stays in Python |

No recorded decision is reversed. Two are touched: `shutdown-ends-cli` is preserved because the core's
threads die with the interpreter plus `kill_all` on the recorded shutdown path, and `retention`'s 512 KB
scrollback cap is preserved byte-for-byte by W9 in the core. Six new rows record this design itself:
`terminal-core-start`, `-host`, `-ws`, `-replaces`, `-batching` and `-conpty`.

### 8.1 Fixes on master since this design

Master took these fixes after `b3d0614` (and on 2026-11-12, while the design was drafted). Each row says
what the core or the cut-over must do so the bug does not come back. The integration branch merges master
before each step starts and again before step 6, so the Python suites of these fixes run in every gate.

| master commit | bug fixed | what the core and the cut-over keep | where it is checked |
|---|---|---|---|
| `4e693ce` | pyte drew the residue of a CSI with a `:`, `<` or `=` marker as text | §8 row; `vte` reads the sequence whole, `csi_marker.rs` (step 2's copy of the bug) is deleted in step 2c | 12 re-rendered screen goldens; `edge-screen-*` marker cases |
| `ee943e3` | a trim cut inside the newest resume banner dropped it | §8 W9 row; `history.rs` carries `BANNER_LINE_MAX_BYTES` | `edge-w9-*` goldens |
| `defa26b` (on master via `5abe2db`) | pyte dropped the rest of a text run after a zero-width character (ZWSP, ZWJ, VS16 in `⚠️`) | a zero-width character that is not combining takes no cell, starts no pending wrap, and the text after it is drawn | `edge-screen-*` zero-width cases; `test_terminal_screen_zero_width` |
| `a3dce22` (branch `fix/pyte-csi-esc-restart`, not on master yet) | pyte drew the rest of a CSI cut by an ESC, a C0 control or a DEL as text | §8 row; `vte` already reads them as a terminal does, no core change | `edge-screen-csi-cancel`, `edge-screen-csi-controls`; `test_terminal_stream_csi` |
| `4b06139` | one failed accept closed the hub's listener for good | §6.4: the core's accept loop survives every accept error; `ws_listening()` in the health check | step 4 test with injected accept failures |
| `3874299` | one oversized record ended a long-lived watcher loop | §6.3: an error costs its item, never the loop, in every native thread and in the step 3 drain handler | step 2c/3 review checks every `?` and `break` in the native loops; a step 3 test makes one session's scrollback write fail and sees the next session drained |
| `0e17c54` | a scrolled or slowly typed Codex paste never settled, so no Enter was sent | new consumer (§4): the settle loop reads the composer every 0.1 s and must see the scroll mark `↑` and each new frame as pyte does | step 3 live capture `live-codex-scrolled-paste` (§10) |
| `c527749`, `6276c63` | OMP approvals showed a clipped command; a box was not paired with its own call | new consumers (§4): box rows, wrapped or not, must render exactly as pyte renders them, since the command is matched against the transcript with whitespace removed | step 3 live capture `live-omp-approval-box` (§10) |
| `3418da9` | a Codex abort did not wake the parent; deny feedback was not submitted | the key guards (`OMP_PROMPT_KEY_GUARD_SEC` 0.6 s after the box is seen, `AGY_DENY_FEEDBACK_DELAY_SEC`, `CODEX_DENY_FEEDBACK_HOLD_SEC`) are measured in Python from when it sees the box; `write` only ever lands later than it is called, so the guards hold as long as `write` keeps arrival order (D7) | `test_session_approvals`, `test_native_records` in the step 3 gate |

Not terminal work, so nothing for the core: `e2a7030` and `8ca5931` (transcript-driven turn ends),
`a2abf90` and `b718834` (DSH TLS context, logging, a DB index), `54db9b3` and `0168939` (wait handles),
`68bcd0c` and the `transcript_feed` commits.

## 9. Migration plan

No flag, no shadow mode, no dual ownership. The work happens on the long-lived `feat/rust-terminal-core`
branch; every step's gate is tests (cargo, the ported Python tests, fixture parity), never a runtime
switch, and there is exactly **one cut-over merge**. Before the merge the hub still runs pywinpty; after
it, the branch's tree runs only the extension. Rollback is `git revert` of the merge. No data migration:
`raw`/`scrollback`/`last_screen` keep their formats. `pyte` survives to step 6 as an offline, dev-time
oracle only: it renders the frozen corpus so the Rust parser can be compared against it, it is never
imported by the hub after step 3, and it is deleted with its `requirements.in` entry in step 6.

| step | work | gate | rollback | size |
|---|---|---|---|---|
| 0. Freeze behaviour, offline | a temporary dump in `_on_pty_output` records a few minutes of raw chunks per engine (the five PTY engines: claude, codex, omp, kimi, agy); `pyte` renders them and the dumps are frozen into `tools/fixtures/terminal/` (chunk stream, screen, scrollback 250, `raw` after trim, `raw_tail`); the dump is deleted. `pyte` is used only here, offline, and deleted at step 6 | fixtures committed; the dump is gone from the tree | files only, no runtime change | S, 1–2 d |
| 1. Extension skeleton | `terminal-core/` crate (`hub_terminal_core`, `cdylib`, `panic="unwind"`, abi3 gate §7.3): module, `Core`, threads, `spawn`/`write`/`resize`/`kill` via system ConPTY, one reader thread, no parser yet; `terminal_core.py` wrapper with the missing-module message | `cargo test` on CI; a manual `--selftest` binary spawns `cmd.exe`, echoes bytes, resizes, kills the tree; abi3 import works on 3.11–3.14 | branch only; nothing references it | M, 4–6 d |
| 2. Parse, history, coalescing | `vte` 0.15 + pyte's screen model in `terminal-core/terminal_stream`, W8 carry, OSC strips, W9 ring with golden fixtures, UTF-8 carry, `screen`/`scrollback`/`raw`/`raw_tail`, batches + the ticker, `drain`; step 2c fixes the screen-model divergences the review found (DECSTBM, DECOM, save/restore, zero-width, combining), orders delivery per sink, and deletes `csi_marker.rs`; step 2d makes the delivery state (lock, sinks, handles) belong to the session id, which step 4's sockets attach to | `cargo test` **and** `cargo test --features conpty_session/test-hooks` in `terminal-core/` (the delivery-order, resume-handover and panic-isolation cases need the hooks; neither the hooks nor their call sites exist in a release build): `terminal_stream/tests/fixture_corpus` parses all 33 cases under `tools/fixtures/terminal/`, byte for byte, and fails when any golden a case calls for is missing; the 8 `screen_exceptions.jsonl` rows are exactly the NFC difference, and a stale row fails the gate; coalescing preserves bytes and order; `delivery_order.rs` pins per-sink order and atomic attach, `sink_handover.rs` the resume handover, and both pass 20 runs in a row with the hooks on | branch only | M, 5-7 d |
| 3. PTY ownership in Python | `_read_loop`, `_on_pty_output`, `HubTerminalScreen`, `HubTerminalStream`, `trim_history_text`, `_strip_saved_lines_erase`, `unwrap_windows_cmd_shim` and pywinpty all deleted; every `screen` call site reads the core; `is_process_alive`/pid/`last_output_at` come from the drain; `attach_text`/`feed` for DSH virtual sessions; `tools/*.py` fakes replace `FakePty` | the full Python suite on the branch, with the ported tests from §10; `test_tui_delivery` and `test_session_approvals` unchanged in their assertions | branch only | L, 6–9 d |
| 4. Terminal sockets in the core | WS listener, ticket call + `GET /api/sessions/{id}/terminal-ticket`, replay/`replay_end`, input+OSC strip, `close_sockets`, console `ptySocketUrl` becomes a ticket fetch; the socket sink is a bounded `FrameQueue` drained by the socket's own thread, which writes the replay and `replay_end` before the first frame; per-socket `dropped_bytes` reported per handle in the drain record rather than summed per session, and `attach_sink`'s replay render measured under the delivery lock (≈1.5 ms at the 512 KiB cap) before deciding whether to move it out; `ws_pty_endpoint`, `attach_ws_queue`, `detach_ws_queue`, `adopt_ws_frontends`, `register_ws_closer`, `close_ws_frontends`, `push_ws_frame` deleted | `test_hub_persistence.py`'s WS case against the core socket; a manual console check per engine (replay, reconnect, input, resize, resume banner) | branch only | M, 4–6 d |
| 5. Packaging and the required import | `payload.txt` + release script produce and embed the `.pyd`; privacy gate covers it; `bootstrap.ps1` `Check-Imports` knows the resources dir; `.gitignore` entry; pywinpty removed, `requirements.lock` regenerated | portable build starts on a machine with no cargo; a fresh from-source checkout fails with the documented message and starts after the two build commands | branch only; the merge is the cut-over | S, 2–3 d |
| 6. Cut-over merge and load test | merge; run §11 against `master` (recorded before the merge) on an idle machine; tune flush windows, ring sizes and the drain interval; delete `tools/fixtures`'s pyte oracle and the `pyte` dependency entry | §11's pass criteria; a full suite run on `master` after the merge | revert the merge | M, 2–4 d |

Total ≈ 5–6 weeks for one implementer plus review; steps 2 and 3 dominate, and step 3 is the only one that
touches most of `session_manager.py`.

## 10. Tests

Existing tests that cover this behaviour and what porting them means:

| test | pins | action |
|---|---|---|
| `test_tui_delivery.py` | checked-submit key sequence, one Enter, clear input, no screen-based proofs | replace the shared `FakePty` (`test_native_records.py:15`) with a fake core client that records the same `inputs` list; keep the assertions literally |
| `test_session_approvals.py` | approval keys reach the PTY, `get_clean_screen_text` patched per test | same fake client; patch the client's `screen` instead of the method |
| `test_hub_persistence.py` | scrollback blob written once per rev, `_scrollback_rev_written`; and one live `/ws/pty` connect (`:276`) | assert on the core's `rev`; point the WS case at the core's listener with a minted ticket and keep the `replay_end` expectation |
| `test_restart_resume_on_start.py` | resume banner, continuation text, archived-at-stop paths | add: the seeded history and the banner arrive in one `spawn(seed_history=…, ws_prefix=…)` call and the banner precedes the first output frame |
| `test_event_loop_stalls.py` | loop latency under a write backlog | add a case that fires 20 drains of 50 records through the fake client; existing cases unchanged |
| `test_session_launch_args.py` | argument stripping/injection | unchanged; the shim table moves to a Rust test with the same fixtures |
| `test_transcript_feed.py` | transcript rendering | unchanged |

New tests, none run by this unit:

- Rust, parser and history: the shim-unwrap table (same fixtures as `test_session_launch_args.py`) and
  the step-0 corpus (`tools/fixtures/terminal/`, rendered screen-for-screen against the frozen Python
  output): W9 trim goldens (UTF-8 split, `budget <= 0`, and the three banner outcomes — banner after the
  cut, cut inside the banner line (banner kept, history over the cap by less than the line), banner
  older than the budget); the in-loop W9 trigger
  (`edge-w9-inloop-trigger`: regenerate `chunks.jsonl` from its `generator` block, then the ring must
  collapse to one entry with the entry/byte counts `trigger.json` records, the trimmed text in
  `raw.txt`, and the 64 KiB tail unchanged); the W8 carry for all three splits; the per-chunk OSC query
  strip and the per-stream replay strip (`replay.txt`); the OSC reply strip including the `52` and
  `1[01]\d` branches; UTF-8 carry across reads.
  The 13 `edge-screen-*` cases pin DECSTBM (full height, partial, one parameter and `n;`), DECOM (set, reset,
  inside a region), save/restore with DECAWM and DECOM toggled after the save, zero-width characters
  (VS16/ZWSP/ZWJ, at the right edge, split across reads), combining marks, a CSI cut by CAN/SUB/ESC, C0
  controls and DEL inside a CSI, and the CSI forms pyte's `Stream` ends early (`?2026$p`, `<` `=` `:`, an
  SGR with subparameters). The harness also prints the parse throughput of the largest live case (20+
  passes on one thread), and a golden that a case's kind and `meta.json` call for must exist: a missing one
  fails, it is not skipped.
- Rust, mechanics: coalescing (bytes preserved and order kept, frame cap honoured), Q11 (a socket that
  connects during a write storm sees every chunk exactly once, checked by a monotone counter in the
  stream), `catch_unwind` per reader thread (a panic marks one session `crashed` and leaves the others
  alive), the ticket
  (expiry, wrong session id, reused nonce, bad MAC), slow-client drop accounting, `kill` ends a child
  tree (a spawned `cmd /c start` grandchild must not survive), a failed accept followed by a served
  connection (§6.4, master `4b06139`).
- Live captures for the screen readers master added after this design (§8.1), recorded in step 3 with
  the step 0 recorder and rendered by the oracle: `live-codex-scrolled-paste` (Codex at 80×24, a
  ~900-character follow-up that scrolls the composer, so the `↑` mark shows) and `live-omp-approval-box`
  (an OMP approval box whose command is longer than the box is wide). Besides the golden match, the step 3
  test runs the real consumers on the core's screen: `_codex_composer_shows_whole` accepts the settled
  composer, and the OMP box parse yields the command and its `command_truncated` flag as it does on pyte's
  screen. Today their tests pass the screen in as a string, so no test feeds their bytes through either
  screen model.
- Rust, build config: CI asserts the extension's profile unwinds (a `cargo metadata`
  `profile.release.panic` check for the core crate), because a stray `panic = "abort"` silently turns
  every caught panic back into a hub kill.
- Python: `test_terminal_core_client.py` — the wrapper's missing-module message, deadline expiry returns
  the cached snapshot and marks `degraded`, `drain` clears flags so a second call returns nothing,
  `crashed` sessions archive as crashed rather than hanging a call, and the ticket endpoint 404s for an
  unknown or ended session.
- Console: the repo has no JS test harness, so the ticket fetch (`ptySocketUrl` becoming async) is
  covered by a manual pass per engine in step 4's gate: replay, reconnect after a hub restart, input,
  resize and the resume banner.
- Load/soak: §11.

## 11. Load test

Run only when the machine is idle; it is not part of the default suite.

- `tools/fake_tui_engine.py` replays a recorded corpus (JSONL of `(delay_ms, chunk)` from step 0) at the
  recorded rate to stdout, then prints a final byte count. Started as a normal session, so the whole real
  path is exercised (system ConPTY, core, hub, console), with `--speed 1` matching today's 72 chunks/s per
  session.
- `tools/load_terminal_core.py` starts 15 / 30 / 50 sessions, attaches one WebSocket consumer each, samples
  for 5 minutes and records: hub CPU (`\Process(python*)\% Processor Time`), context switches per second
  for the hub PID (`\Thread(*)\Context Switches/sec`), thread count, event-loop lag (a coroutine measuring
  `sleep(0.05)` overshoot, p50/p99/max, the technique `test_event_loop_stalls.py` already uses), GIL-held
  share (py-spy `--gil` if available, otherwise loop lag and CPU as proxies), `core.stats()` counters,
  drain count per second (expected ≤20), and per-session byte accounting (corpus bytes vs bytes delivered
  to the consumer).
- Pass criteria at 50 sessions: hub CPU < 0.5 core, p99 loop lag < 50 ms, no lost bytes, Python-side
  wakeups < 1,000/s, hub threads < 20, console FPS ≥ 30 while a session prints continuously.
- The comparison is against `master` before the cut-over merge: the same script pointed at a hub on the
  pywinpty code (the recorded 12-session profile in §2 is the baseline). There is no `off` mode to compare
  against after the merge, which is the point of removing the flag.

## 12. Risks and open questions

1. **Parser parity.** The approval and composer parsers match exact rows. `alacritty_terminal` may render
   a cell differently from pyte in ways nobody has hit yet. Mitigation: step 0's frozen corpus, rendered
   both ways in step 2's tests; an unexplained diff is a port bug, and one that survives review blocks the
   merge rather than getting a per-engine exemption (there is no second path to fall back to).
2. **W9 byte parity.** The trim is byte-level (UTF-8 split, banner protection, prefix). Golden fixtures are
   the only defence; treat any diff as a bug in the port, not in the fixture.
3. **GIL deadlock between a Python lock and a core lock.** The design has one direction only (Python may
   hold a lock and call the core; the core never takes a Python lock and never calls back except to
   schedule), but one mistake — a core call made from inside a `with self._lock:` block that a drain
   callback also wants — deadlocks the loop. Mitigation: the drain callback takes no session lock before
   its core calls, every core call is documented as "GIL released, may block up to the deadline", and
   §10 carries a test that drains while a session lock is held elsewhere in the test.
4. **Extension import failure or a bad build.** With no fallback, an ABIs mismatch (`hub_terminal_core`
   built for 3.13 into a 3.14 venv), a missing file or a broken payload makes the hub refuse to start.
   Mitigation: abi3 or per-minor artefacts decided in step 1; the version is verified by the module
   (`__version__`, a `Core.abi` string checked against `sys.version_info` at import); the bootstrap import
   check (§7.3) fails the environment preparation with the build instruction instead of a hub that starts
   and then 500s on every launch.
5. **Panic to abort.** Any dependency compiled with `panic = "abort"` (or an `abort()`/stack overflow/
   access violation) kills the whole hub. Mitigation: `cargo metadata` profile assertion in CI, and no
   `unsafe` outside the `windows` FFI wrappers, each with a `// SAFETY:` note in the existing style.
6. **Per-socket drop accounting and the replay render under the delivery lock.** Two step-4 items are
   carried forward from step 2's delivery review rather than built now: `dropped_bytes` is summed per
   session (`stats`/drain record), so with two sockets one client cannot learn its own loss, and
   `attach_sink` holds the delivery lock across `replay_text()` (one ring join plus one strip pass, ≈1.5 ms
   at the 512 KiB cap), which serialises the reader for that long. Step 4 owns both: the count moves onto
   `SinkEntry` and is surfaced per handle, and the load test (§11) measures the render before anyone decides
   whether to render under `text` alone and re-take it for the batch.
7. **Control path on the event loop.** A wedged reader costs up to the deadline per call (50 ms for
   write/resize/screen/scrollback/raw/stats, 2 s for spawn/kill/kill_all). Mitigation: deadline,
   cached-snapshot fallback, `degraded` marking (itself a dirty flag, cleared by the next successful
   call), and readers that hold the session mutex only for one buffer append. `spawn`'s 2 s cannot be
   enforced — `CreateProcessW` is not interruptible — so it is a release gate measured by the selftest
   (20 spawns, p50/p99 printed) and logged when exceeded, not a timer.
8. **ConPTY close/resize races.** Closing a pseudoconsole while a read is pending is a known source of
   hangs (unverified specifics). Mitigation: the close happens exactly once, through a three-state slot,
   always on a helper thread with a deadline, never on the reader thread; `resize` holds that slot across
   `ResizePseudoConsole` (releasing it would let a close invalidate the HPCON), and every other acquirer
   uses a deadline, so a stalled resize costs the last bytes of one session and one blocked reader
   thread — the death record, `kill` and `kill_all` still complete inside their budgets. A close helper
   that outlives its deadline keeps running; the slot stays `closing` until it returns, and callers
   publish on their own deadline (measured: a few seconds at most under load on a busy machine).
9. **Nested job objects.** The hub already runs inside the app's kill-on-close job (`owned_hub.rs:142`) and
   the core creates one job per session. Nested jobs are supported on Windows 8+, but whether the outer
   job's limits let the inner handle behave needs the one-hour experiment before step 1 is called done.
   If `AssignProcessToJobObject` fails, the fallback is the app's existing tree kill: walk the children
   with `CreateToolhelp32Snapshot` (`Win32_System_Diagnostics_ToolHelp` is already linked) and terminate
   them, which is what `taskkill /T /F` does today (`:2597`, `:5680`).
10. **Packaging.** The `.pyd` in the payload must survive the privacy gate (host paths, CodeView record) and
   the `bootstrap.ps1` import check (`-I` implies `-P`). Both are step 5's gate, and failure there is a
   build-script fix, not a gate exemption.
11. **Fixture provenance.** Today's captures came from the bundled `conpty.dll` (pywinpty 3.1.2), while the
    core uses the system ConPTY, so the byte stream can differ in repaint/erase details on day one. Step 0
    therefore froze the corpus, and step 2a re-recorded claude, codex, kimi and omp on the system ConPTY
    (exact read bytes, `chunk_encoding: utf8_bytes`); `live-agy` keeps its bundled recording
    (`agy-bundled-case`). Observed differences: conhost answers DA1, window-title and OSC 10/11 queries
    itself, so the live streams no longer carry them (the `edge-osc-*` cases still pin the strips), and
    reads are smaller.
12. **No Python-side batching.** The parallel unit's Task 1 does not land (owner decision 7). The core's
    coalescing is the only batching, which means before the merge there is no batching at all and after it
    there is exactly one layer — never two, so there is no double-batching case to reason about.
13. **Skipped here.** No code, no compiled module, no benchmark. Crate versions and APIs cited are file
    evidence where marked (the `windows` console/job exports, the lock contents, uvicorn's deflate
    default) and marked unverified otherwise (PyO3's panic conversion and abi3 range, `tungstenite`'s
    deflate support); pin them when step 1 opens.
14. **Exit-code fidelity.** The reaper reads `GetExitCodeProcess` after the handle is signalled, and `kill`
    reads it *before* `TerminateJobObject` — a process that has exited but is still terminating can have
    its code replaced by the job's termination code, so the pre-read is what keeps `cmd /c "exit 3"`
    reporting 3 when the hub kills it in that window. A failed query logs and leaves `exit_code = None`
    (archived as a crash) instead of a fake 0; a code that arrives after the death record is re-carried
    by the next drain.

Open questions for the owner: none blocking. Two are deferred by design rather than unanswered: whether
the `.pyd` ultimately ships as abi3 or per-minor follows from step 1's abi3 probe, and permessage-deflate
stays off unless §11 shows a reason to turn it on.
