# Rust terminal core

Design of the per-output-chunk work that moved out of Python so the hub can run 50 printing sessions at
once. The hub runs on the core: `terminal-core/` is in the tree, every session's PTY, screen and raw ring
live in `hub_terminal_core`, and the console's terminal sockets are the core's own behind a hub ticket
(step 4). Line numbers refer to the tree at the time of writing and are anchors, not contracts.

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
  Removed outright; `pyte` was an offline fixture generator only (§9 step 0), deleted at step 6.

## 2. What the hub did before the core

Measured on the live hub (2026-11-13, ~12 sessions printing) while it still ran pywinpty/pyte, and
confirmed in code. This is the baseline §11 compares against; the path below no longer exists.

| fact | value | source |
|---|---|---|
| server CPU | 0.4–0.7 cores | profile |
| thread wakeups | ~4,500/s, ~90 hub threads | profile |
| Python-held GIL share | ~5% of samples | profile |
| per printing session | 3–6% of a core, 200–770 wakeups/s | profile |
| idle session | ~0 | profile |
| output chunks | ~870/s across sessions | profile |
| linear projection at 50 sessions | 2–3 cores, ~19,000 wakeups/s, ~250 threads | profile |

Per chunk, then:

```
winpty/ptyprocess.py _read_in_thread   read(blocking) -> loopback TCP send -> sleep(1 ms)
  -> AgentSession._read_loop            one thread per session
  -> AgentSession._on_pty_output
       lock -> pyte feed -> generic-engine tool-line scan -> raw_history append + W9 trim check
            -> snapshot of ws_queues
       unlock
     -> one loop.call_soon_threadsafe per chunk PER WebSocket
  -> ws_pty_endpoint sender             server.py  one WebSocket frame per chunk
```

`pywinpty` 3.1.2 was a C extension built on **winpty-rs 1.0.8** (the crate path is embedded in
`_winpty.cp314-win_amd64.pyd`) that called the bundled `conpty.dll` exports
`ConptyCreatePseudoConsole`, `ConptyResizePseudoConsole`, `ConptyClosePseudoConsole`,
`ConptyReleasePseudoConsole`, `ConptyPackPseudoConsole`, `ConptyReparentPseudoConsole`,
`ConptyShowHidePseudoConsole`, `ConptyClearPseudoConsole`. Its Python layer
(`winpty/ptyprocess.py`) was the loopback socket plus 1 ms sleep loop above — that is where two of the
five hub threads and most of the small-chunk overhead came from.

The pure-Python parser was pyte 0.8.2 through two subclasses, `HubTerminalScreen` (private-DSR guard
and safe `display`) and `HubTerminalStream` (one bad escape sequence must not abandon the rest of a
chunk). It rendered the frozen corpus offline and was deleted at step 6 with its renderer; the hub
imports neither pyte nor pywinpty.

## 3. What moved to Rust

1. **ConPTY lifecycle**: spawn (argv, cwd, env, cols×rows), write, resize, kill the process tree,
   report the exit code and the pid. `unwrap_windows_cmd_shim` stays in Python (decision
   `cmd-shim-stays-python`, step 3): it is argv preparation that runs once per spawn, off the output
   path, so the hub unwraps argv and then calls `spawn`; its table stays in `test_session_launch_args.py`.
2. **VT parsing**: screen (cols×rows, cursor, alt screen, wide/CJK cells), 3000-line history, and text
   rendering that matches `screen.display` plus `rstrip`/blank-line trimming.
3. **Raw history**: the byte-capped ring with W9 trim (`terminal_stream/src/history.rs`
   `trim_history_text`), the W8 `ESC[3J` strip for resumed sessions (`history.rs`'s carry,
   `strip_esc3j_on_output`) and the `OSC_COLOR_QUERY_RE` strip (`terminal_stream/src/esc.rs`
   `strip_color_queries`), with their split-sequence carry buffers.
4. **Per-frame batching**: one coalesced frame per session per flush window, instead of one frame per
   engine write. This is the only batching in the system (owner decision 7: the Python-side batching
   unit never lands).
5. **Terminal WebSockets**: the core serves the terminal socket itself, on its own native threads and
   its own loopback listener, behind a short-lived ticket the core mints and the hub hands to the
   console. Python holds no queue in the output path.
6. **Activity tracking**: `last_output_at`, bytes/chunks per session, screen revision, liveness — as
   counters Python reads at a fixed rate, instead of being accumulated per chunk in Python.

Kept in Python: env assembly and policy (`TERM`, `COLORTERM`, `PYTHONHOME` removal, model injection),
the 20/5 col/row clamp and viewport propagation (`resize_session_from_client`,
`session_manager.py:4301`), TuiWriterQueue ordering policy (`:2098`), approval/dialog detection and
every parser of screen text (`parse_codex_approval_prompt`, `:1774`; `_tui_input_line`, `:11386`;
`sanitize_tui_screen_text`), the hub's own text path into the core (`emit_virtual_ansi`'s `feed`, `:2397`),
persistence, ticket issuing and the console-facing HTTP API.

## 4. Consumer map

Every current reader of the pyte screen or the raw history, what it needs, and its new call.

The anchors are line numbers at `b3d0614`, the commit this design was written against; the table keeps
them for its core-call column only. Step 3a re-derived the consumers by function name and routed every
one through `AgentSession`'s seam methods, one per core call: §4.1 is that map and the one to work from.

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
| text session (core tests, corpus recorder) | — | screen fed from text, no PTY | `attach_text` + `feed` |
| archived screen / scrollback reads | `server.py:669`, `server.py:1032` | the DB row only | unchanged; its input is `raw`/`screen` at persist time |

### 4.1 After step 3a: the seam and its callers, by name

Every place in `session_manager.py`, `server.py` and `tools/*.py` that read or wrote terminal state, by the
function that holds it, with what it needs and the seam call it now makes. Line numbers are gone on purpose:
they were stale at `b3d0614` and will be stale again. The table above maps consumers to core calls; this
one, split by what each caller actually touched, maps them to the seam methods 3b-hub swapped for core
calls.

The seam (design §5.2) lives on `AgentSession`; each body is now one core call, as
`seam-coverage.md` maps it (the file is gone from the tree; its tables survive here and in §5.2):

| seam call | reply | replaces |
|---|---|---|
| `read_screen(max_lines=80) -> ScreenRead(text, cursor_x, cursor_y, cols, rows, rev)` | text + cursor + geometry + revision | `get_clean_screen_text`, `sess.screen.display`, `.cursor`, `.columns`, `.lines` |
| `read_scrollback(max_lines=300) -> ScrollbackRead(text, rev)` | visible screen + history rows | `get_full_scrollback_text` |
| `read_raw() -> RawRead(text, bytes, rev)` | the joined ring, already inside the cap | `"".join(sess.raw_history)`, `raw_history` text access |
| `read_raw_tail(max_chars=SUBMIT_DIAGNOSTIC_TAIL_CHARS) -> str` | newest characters, cut on a character boundary | `"".join(raw_history)[-65536:]` |
| `raw_revision -> int` (property) | the rev without the join | `raw_history_rev` at the blob's change gate |
| `seed_history(text)` | one ring entry, rev 1, ESC[3J carry cleared | `sess.raw_history = [seed]` + the two counters |
| `write_input(text)` | bytes queued / virtual echo | `pty.write`, unchanged name |
| `resize(cols, rows) -> (cols, rows)` | applied geometry after the 20/5 clamp | `pty.setwinsize` + `screen.resize` |
| `kill(grace_ms=KILL_GRACE_MS) -> Optional[int]` | exit code if known | `taskkill /T /F` + `pty.terminate(force=True)` |
| `is_process_alive() -> bool` | alive, by PTY, never by status | unchanged name (kept for tests) |
| `has_process -> bool` (property) | holds a PTY object | `sess.pty is not None` truthiness |
| `pid -> Optional[int]` (property) | the spawn reply's pid | `getattr(sess.pty, "pid", None)` |
| `last_output_time() -> float` | unix seconds of the last byte | `sess.last_output_at` reads |

#### `server.py` (3 terminal sites)

| consumer | needs | now calls |
|---|---|---|
| `read_subagent_screen` (MCP tool) | exact screen, `max_lines` (default 80), plus the composer line and K1 banner state from the same text | `read_screen(max_lines).text` (`server.py:644`) |
| `_session_can_receive` | live process | `is_process_alive()` (`server.py:122`) |
| `api_terminal_ticket` (console ticket) | a one-use ticket URL, minted by the core | `core.ws_ticket` off the loop (`server.py:1343`); the socket itself is the core's (§6.4) |

`ws_pty_endpoint` is gone (step 4): the console fetches the ticket and connects to the core's listener, so
no hub route touches terminal input any more. Resize frames from the browser reach the core's socket, which
records them as requests; `hub.resize_session_from_client` (`session_manager.py:4301`) applies them with the
hub's clamp and propagation.

#### `session_manager.py`, inside `AgentSession` (the seam's own bodies, not consumers)

`read_screen`, `read_scrollback`, `read_raw`, `read_raw_tail`, `raw_revision`, `seed_history`, `write_input`
(`write_input_async` for the drain consumer), `resize`/`resize_async`, `kill`, `is_process_alive`,
`has_process`, `pid`, `last_output_time` hold the only field access that is left. `terminate` reads the
session it ends (`read_scrollback(80)`, `is_process_alive()`, `kill`); `to_dict` reads
`liveness`/`has_process`/`pid`/`last_output_time`; the exit path (`_finish_read`) no longer reads the
scrollback itself: the 80-row final-response snapshot is the lifecycle callback's, taken in a worker by
`_snapshot_final_response_async` (§6.5).

Allowed direct readers/writers after 3b-hub:

- `start_pty` (`:2454`) — the spawn path: `env` assembly, `unwrap_windows_cmd_shim` and one
  `core.spawn(…, seed_history, ws_prefix)`. This is a producer, not a reader: it holds no terminal state.
- `emit_virtual_ansi` (`:2397`) — hub-written lines (resume banner, exit note) go through the core's `feed`.
- `_pid_alive` (module level, `:936`) — `os.kill(pid, 0)` on a *pid number* for the hub's single-instance
  lock, not on `AgentSession.pty`.

#### `session_manager.py`, `HubSessionManager` (the reads, liveness checks and writes below)

Screen readers (25 call sites in the manager, all `read_screen(...).text` unless noted):

| consumer | needs | argument kept |
|---|---|---|
| `_last_screen_text` | screen → last 20 non-empty lines for the record | 80 |
| `summarize_transcript` | screen as the raw-TUI fallback | 30 |
| `_capture_submit_diagnostic` | screen geometry (`cols`, `rows`) only | default read |
| `_tui_composer_located`, `_clear_tui_draft` (2), `_submit_codex_text`, `_submit_text_to_tui`, `_submit_queued_live_text`, `_await_typed_text_settled`, `_confirm_tui_submit` (2), `_watch_tui_startup_and_type` (2), `deliver_parent_message` | composer line, submit marks, settle loop | 40 |
| `_codex_composer_empty` | `cursor_x` only (placeholder painted after column 3) | default read |
| `_watch_claude_session`, `_watch_kimi_session` | banner / spinner lines | 35 |
| `_watch_codex_rollout` | "is archived" detection | 40 |
| `respond_to_interaction` | Codex MCP modal parse | `max(38, sess.rows)` |
| `_refresh_codex_mcp_response` | modal parse after answering | `max(38, session.rows)` |
| `_register_omp_event_approval`, `poll_tui_screen_interactions_once` | OMP box rows | `max_rows` = `max(38, rows)` |
| `wait_for_output_settle` | screen handed to `progress_cb` | 15 |

Scrollback readers (3 call sites in the manager, plus the seam's own `terminate`): `persist_session`
(250 lines, the record), `_read_session_snapshot` (250, for the stop's per-id fallback), and
`_tui_banner_visible` (K1 banner may have scrolled, `TUI_READY_BANNER_HISTORY_ROWS`). The two 80-line exit
snapshots are gone from this list: `_finish_read` hands its snapshot to the lifecycle callback, which reads
it in a worker through `_snapshot_final_response_async`/`_capture_submit_diagnostic`'s sibling helpers.

Raw readers (3 sites): `_scrollback_blob` — `read_raw()` for the text and `raw_revision` for the two change
gates; `_capture_submit_diagnostic` — `read_raw_tail(SUBMIT_DIAGNOSTIC_TAIL_CHARS)`.

Spawn/exit plumbing: `_make_resumed_session` → `seed_history(seed)`; `_end_predecessor` (a resume) →
`kill(KILL_GRACE_MS)` on the predecessor; `_shutdown` → `kill_all` in the core, no per-session kill left.
`start_pty` is the only `spawn`.

Liveness, 21 `is_process_alive()` at manager level plus the seam's own `terminate`/`is_process_alive` body:
`compute_resumable` (2), `_claim_conversation`, `_resume_locked`, `_resume_check_context`, `_close_session`,
`_shutdown`, `_scrollback_flush_loop`, `_submit_queued_live_text` (2), `_deliver_deny_feedback`,
`_clear_tui_draft` (2), `_transcript_watch_alive`, `_agy_new_conversation_dir`, `deliver_parent_message`,
`get_subagent_status_and_trace`, `resize_session_from_client` and the resume/predecessor waits. Each reads the
drain record's `alive`, never the status string; a window-only session (desktop GUI) keeps the status check
because the core has no entry for it.

Process presence, 3 `has_process` at manager level plus 8 inside `AgentSession` (the seam, `terminate`,
`classify_end_reason`, `to_dict`): `_end_predecessor`, the close path, and the Codex live-write check.

Write side: `write_input` at 46 call sites (26 of them in `respond_to_interaction`), `write_input_async` once
(the drain consumer's text-session input), `resize_async` in `resize_session_from_client` (the target; the
background panes go through the same async path), `last_output_time()` in `to_dict` (2) and
`wait_for_output_settle` (3), `kill` in the seam and `_end_predecessor`.

#### `tools/render_terminal_fixtures.py` (deleted at step 6)

The renderer was the oracle's seam consumer: `screen_text` → `read_screen(80).text`, `joined_raw` →
`read_raw().text`, `render_session` → `read_scrollback(250).text`/`read_raw_tail`/`replay_frame`,
`feed_session` → `resize`, `new_session` → `seed_history(seed)`. Step 6 deleted it with the oracle;
the goldens it rendered are the spec and `terminal_stream/tests/fixture_corpus` compares them against the
core. `tools/record_terminal_corpus.py` touches only its own recorder PTY (its live screen is now a core
text session), never `AgentSession`.

#### What did not fit the seam, and why

- **`_scrollback_blob`'s change gate** uses `raw_revision` (the counter, no join) rather than `read_raw().rev`.
  Joining 512 KB per persist only to compare two integers would be a real regression; the counter is the same
  revision the `raw` reply carries, and the drain record supplies it.
- **`session.cols` / `session.rows`** stay plain fields: they are the hub's chosen geometry, not a screen
  read, and the 20/5 clamp is applied by the hub (§6.4). The **screen's** `columns`/`lines` had no seam accessor
  before; they now come from `read_screen().cols/rows`.
- **`_capture_submit_diagnostic`'s evidence JSON** keeps its two screen-geometry fields by asking
  `read_screen()` for them; the screen's `cols`/`rows` track `session.cols`/`rows` exactly.
- **`write_input` and `is_process_alive` keep their names** (the brief's exception): `write_input` is patched by
  `test_session_approvals` (3 targets) and `test_tui_delivery` (2), and `is_process_alive` by
  `test_transcript_view` (2); 46 and 21 internal sites plus `server.py` call them. The bodies are the core's `write`
  and the drain record's `alive`.
- **`kill` is one core call**: `asyncio.to_thread(sess.kill, …)` where a caller on the loop needs it, and the
  core's 2 s bound covers the exit wait plus the console close. A text session has no process but the core
  still owns its id, so the call ends it there and `retire` can accept the id afterwards.
- **`kill` returns `None` for a process that outlived its budget**: the core reads the code before
  `TerminateJobObject`, so `cmd /c "exit 3"` reports 3, and a code learned afterwards arrives on the next drain
  record (which rewrites the row once, §5.3).

## 5. Interface between the core and Python

### 5.1 The module and its native threads

One PyO3 extension module, imported by `server.py` at start and held for the hub's lifetime. There is no
subprocess, no named pipe, no protocol framing and no version handshake: a call is a call, and a reply is
a Python value built by the core.

```
hub_terminal_core.pyd (cdylib; kernel32 for ConPTY/pipes/jobs, bcrypt for the ticket key)
  Core(...)                    one object per hub; owns the session table, the WS listener, the ticket key
  reader thread per session    core-read-<id>: ReadFile on the ConPTY output pipe: strip -> decode -> parse -> ring -> fan out
  writer thread per session    core-write-<id>: one WriteFile at a time, so input keeps arrival order
  reaper thread per session    core-reap-<id>: WaitForSingleObject on the process, then the one exit teardown
  ticker thread                core-ticker: every 5 ms flush due batches; at most every 50 ms one Python wake
  ws accept thread             ws-accept: one loopback listener, which hands each connection to a handshake thread
  ws handshake thread          ws-handshake-<n>: 5 s deadline, at most 16 at once (503 over the cap)
  ws thread per connection     ws-<n>: one per attached console socket (blocking send/recv in a 250 ms slice, so a slow pane stalls itself)
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
  before the core; `TimeoutError` is a deadline the caller retries (§5.2).

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
| `spawn` | session_id, argv, cwd, env, cols, rows, flags, seed_history, ws_prefix | pid, cols, rows, elapsed_ms | 2 s, measured by the selftest (not interruptible) | `PtyProcess.spawn`, `push_ws_frame` of the resume banner |
| `attach_text` | session_id, cols, rows, flags, seed_history, seed | pid (0), cols, rows | 50 ms | `HubTerminalScreen` for text sessions (`seed_history` is A1's archived scrollback, `seed` the separator frame; no hub caller since DSH was dropped, kept by the A1 contract) |
| `feed` | session_id, text | rev | 50 ms | `emit_virtual_ansi`: hub-written lines (resume failure, exit note) into a session's screen and ring |
| `write` | session_id, text | bytes queued: the whole text or nothing; `0` for empty text; `TimeoutError` when the queue is still full at the deadline | 50 ms | `pty.write` |
| `resize` | session_id, cols, rows | cols, rows; `ValueError` when the pseudoconsole is already closing/closed, `TimeoutError` when the console is busy past the deadline | 50 ms | `pty.setwinsize` + `screen.resize` |
| `kill` | session_id, grace_ms | exit_code (None when the process outlived the budget) | 2 s for the whole call: the exit wait, then the same teardown the reaper runs (console close, wait for the reader's last bytes), which publishes on its deadline at the latest | `taskkill /T /F` |
| `screen` | session_id, max_lines | text, cursor_x, cursor_y, cols, rows, rev | 50 ms | `get_clean_screen_text` |
| `scrollback` | session_id, max_lines | text, rev | 50 ms | `get_full_scrollback_text` |
| `raw` | session_id | text, bytes, rev | 50 ms | `"".join(raw_history)` |
| `raw_tail` | session_id, max_chars | text | 50 ms | `raw_ansi_tail`; a character count, because the hub's tail is `"".join(raw_history)[-65536:]`, and it is cut on a character boundary |
| `retire` | session_id | bool: `false` for an unknown id; `ValueError` for a live one, for one whose exit record has not been drained, and for a spawn/attach/retire in flight; `TimeoutError` past the table deadline | 50 ms for the table read; releasing the last reference can wait for the console-close deadline, and the fan-out close waits for a delivery in flight (bounded by `deliver`'s no-block contract) | nothing removed a session before: every ended session kept its 512 KiB ring and its screen for the hub's lifetime |
| `watch_screen` / `unwatch` | session_id, max_lines, interval_ms | ok | 50 ms | generic-engine tool-line polling, now bounded by `interval_ms` |
| `ws_ticket` | session_id, ttl_ms | url, expires_at | 50 ms: it reads the session table like any other call, and the route awaits it in a worker | nothing (the ticket is new, §6.4) |
| `ws_port` | — (property) | the `127.0.0.1` port the console's sockets are served on | none | nothing |
| `ws_listening` | — | bool, false once the accept thread has ended | none | nothing (the health check, `ws-listening-health`) |
| `close_sockets` | session_id | count: how many sockets were asked to close with 1000 | 50 ms | `close_ws_frontends` (W7: normal close, code 1000) |
| `snapshot_all` | max_lines, max_bytes, budget_ms | per-session dict (`session_id`, `alive`, `exit_code`, `pid`, `crashed`, `degraded`, `screen_text`, `scrollback_text`, `raw_text`, `raw_rev`, `raw_bytes`) | whole 8 s budget | graceful shutdown (`graceful-budget`) |
| `kill_all` | grace_ms | ok; `TimeoutError` when the session table could not be read inside the budget | 2 s for the whole set, every session in parallel | shutdown (`shutdown-ends-cli`) |
| `stats` | — | counters (GIL released, table read bounded) | 5 ms | diagnostics; the 50 ms drain is the normal reader |
| `socket_count` | — | how many console sockets the core holds | 50 ms | diagnostics and tests |
| `attach_sink` | session_id, FrameSink | replay, sink handle | 50 ms | the core WebSocket registration and test sinks; replay and registration are one Q11 step |

`replay(session_id)` stays as a test surface: it returns the text one new socket would get (`replay-shape`),
which is what `test_terminal_core_client` asserts before the listener is exercised.

The write queue's cap is 1 MiB (`CoreOptions::write_queue_bytes`). The cap bounds what *waits* in the
queue, not what one call may hand it: a text larger than the cap is accepted whole when the queue is
empty, so a paste bigger than 1 MiB behaves as it did under pywinpty, and only a second write that
arrives while the first is still queued gets `TimeoutError`.

`flags` for `spawn`/`attach_text`: `strip_esc3j_on_output` (true when `resume_count > 0`),
`strip_osc_color_queries` (always true), `history_rows` (3000), `raw_cap_bytes` (512 KiB),
`raw_slack_bytes` (64 KiB). `seed_history` sets the raw ring before the first byte of the new process
(it was `sess.raw_history = [seed]` before the swap) and clears the `ESC[3J` carry. `ws_prefix` is written
to every socket already subscribed to that id before the reader thread starts, which is where A1's
"between spawn and first output" ordering stops depending on Python's timing. The sinks are the **id's**,
not the new `Session`'s: `Session::start` takes the predecessor's fan-out over, so the prefix goes to the
sockets that were attached before the resume, in stream order after the predecessor's last flushed batch
and its `superseded` mark (`take_over`, §5.4). `attach_text` does the same with its `seed`.

`subscribe`/`unsubscribe` are gone: no Python object holds a terminal queue any more. The console
attaches straight to the core's socket (§6.4), and `close_sockets` is the only fan-out call the hub
still makes (the close path, `_close_terminal_sockets`, and the stop's batch).

**The socket sink path (step 4).** The core WebSocket listener registers a bounded `FrameQueue` through
`attach_sink`. The replay snapshot and sink registration are one Q11 step, so every byte belongs to
exactly one of replay or queued frames. The socket thread writes replay and `replay_end`, then drains
frames in order. Sink handles belong to the session id's fan-out and survive a resume; a later `spawn` or
`attach_text` after `retire` starts with a fresh fan-out. Per-socket `dropped_bytes` remains cumulative
and is reported by the socket listener's drain record.

**`retire`.** Removes an ended session from the table, so its ring and its screen stop living for the
hub's lifetime: `false` for an unknown id, `ValueError` for a live one (the hub kills first) and for one
whose **exit record** no drain has reported yet — that record (exit code, `crashed`) is the one thing
that cannot be re-created once the session is gone, so the hub drains it (within one 50 ms tick) and
retires on the next attempt. A code learned after the death record (SF3) gates the same way. Every queue
attached to the id is closed, the fan-out is retired, and the table's reference is released **before**
that close so an `attach_sink` racing the call cannot have its owner `Arc` dropped while it holds the
delivery lock (`Session::drop` asserts it in test builds).

A resume and a retirement of one id never interleave: `spawn`, `attach_text` and `retire` each take the
id's **lifecycle claim** for the whole call, and a call that finds it taken fails at once with `ValueError`
("… in flight; retry") — never a wait, because the hub's loop must not park on another hub thread's
`CreateProcessW`. The hub retries a claim refusal up to three times, 50 ms and then 100 ms apart
(`_start_with_claim_retry`, `session_manager.py:6004`; `LIFECYCLE_CLAIM_RETRY_DELAYS_SEC`, `:473`), so a
resume that races a snapshot's retire settles instead of failing for good. A `retire` therefore never
closes a fan-out a successor owns or is taking over, and a `spawn`/`attach_text` of a retired id builds a
fresh fan-out: nothing is carried over. A sink that races retirement is not registered; the listener
returns `ended` and closes with 1000 rather than waiting for frames that will never come.

### 5.3 How Python learns about output, screens and exits

Python is not on the output path, so there is nothing to deliver per chunk. The core keeps a dirty flag
per session and a per-session counter record, and the ticker thread makes **one** Python wakeup per
interval, however much output arrived:

- Ticker every 5 ms: flush batches that are due (§5.4) to the core's own sockets.
- At most every 50 ms (the drain interval) and only if any session is dirty, the ticker calls the one
  callback the hub registered, from a thread that attaches to the interpreter just for that call
  (`Python::try_attach` in PyO3 0.30; during interpreter finalization it returns `None` and the tick is
  dropped) and calls `loop.call_soon_threadsafe(hub._on_core_tick)`. One wakeup for all sessions.
- `hub._on_core_tick` (`session_manager.py:3677`) runs on the loop and makes a single `core.drain()`
  call, which returns only the sessions whose flags were set and clears them. A record carries
  `session_id`, `alive`, `exit_code`, `crashed`, `degraded`, `pid`, `bytes_total`, `chunks_total`,
  `last_output_at`, `dropped_bytes`, `screen_rev`, `raw_rev`, `screen_text`, and two hub-facing lists:
  `resize_requests` (`{cols, rows, propagate}` in arrival order) and `socket_drops` (`(handle, bytes)` per
  console socket). Socket input on a text session (no PTY) is discarded: its only consumer was DSH.
  - `raw_rev` is the ring's revision: `raw`'s reply carries the same number, and `_scrollback_blob`'s
    change gate reads it without joining the ring. The mirror is initialised from the stream in
    `new_state` (a seeded attach's ring is already at 1) and advanced with `fetch_max` under the text
    lock, before the frame is published, so a drain taken during a delivery reports the same number
    `raw()` does.
  - `screen_text` comes only for a watched session whose `screen_rev` changed (§5.6).
  - `dropped_bytes` is the **raw ring's** own loss. A console socket's loss is in `socket_drops`, keyed
    by `(session id, handle)` because the core numbers handles per session id, and it is never added to
    the ring's count (§5.4).
  - A console socket's dropped bytes are marked reported only by the drain record that carries them: a
    drop that lands after a drain collected its records belongs to a session with no record in that
    drain and stays unreported until one goes out.
- Each record costs its own session and nothing else (§6.3): an exception while applying one is logged
  against that session and the next record still applies.
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
  therefore never precedes the session's last byte: the hub sees a process die within 5 ms and takes its
  final snapshot in the lifecycle callback, in a worker.
- The death record is never the first thing a drain reports about an ended session: `publish_death`
  delivers the session's last batch inside its own `alive` gate, and a drain that finds the gate closed
  while that flush is still running reports nothing and keeps the session dirty. The record therefore
  arrives at most one 50 ms tick later than it used to, and always after the last byte.
- A code learned after the death record went out — a crashed session the hub drained and then killed,
  or a late `GetExitCodeProcess` — marks the session dirty and pending, so the next drain carries it;
  the drain record stays the hub's only exit-code source. `kill` reads the code before
  `TerminateJobObject`, because a process that has exited but is still terminating can have its code
  replaced by the job's termination code.

**A resume's kill is a supersede, not a death.** Before a successor's `spawn`/`attach_text` replaces an
id's entry, the hub ends the entry the predecessor owns. The death record that kill publishes is a
hand-over: a predecessor that had already ended (an ended session, a user termination) keeps that end
state and emits no lifecycle event for the kill, so a parent is never told that a session it is resuming
crashed. A text entry has no process and the core's text kill answers `exit_code=None, crashed=false`, so
"a death without a code is a crash" applies only to a session that had a process; a text entry keeps the
end state it reached. The death record may be withheld for one tick (above), and the successor's table
insert drops whatever record the core still holds for the id, so the hub drains again, one drain interval
apart and at most 0.25 s, until the death has been applied; a code the core learns while the predecessor
is still the id's object is written to its final row there, before the successor is installed. A
permanent close keeps the closed object as the id's tombstone until `retire` succeeds, so a code the core
publishes after the close still reaches the row it belongs to; a resume of the id drops the tombstone and a
late record is never routed to a successor.

**The console's requests.** The drain record's `resize_requests` are applied by one consumer per session
id, never by the drain itself: the tick only enqueues them in arrival order, and the consumer awaits each
blocking core call (`resize` and the F12 `watch_screen` re-watch) in a worker thread while the 20/5 clamp,
the single-window propagation and the session dict stay on the loop. A
request whose id left the hub's list before it is applied does nothing: the pane is gone, so its intent
must not resize the panes that are still there, and a worker that has not entered the core yet refuses
for the same reason (`CoreCallRefused`). One bad request costs that request only. Each application runs
as its own task, because cancelling the consumer does not cancel a `to_thread` call. A close waits for
that task, bounded by its own deadline, before its `close_sockets`/`retire`, and the stop waits the same
way before `kill_all`/`shutdown`; a worker that has not passed its check by then refuses, and one already
past it when the wait runs out can still enter the core, which answers a killed or retired entry with an
error the request path logs. The consumer ends after 10 s with nothing to do (the next record creates a
new one, and an id the hub dropped leaves no empty queue behind), at the sentinel a permanent close, a
failed resume or the stop puts in its queue, or by cancelling after that close's grace, so no task outlives
the session. A full queue keeps the rest of a batch for the next turn; a backlog past the socket window is
dropped, counted per handle apart from the ring's count, and logged once.

A resume's queued requests belong to the id, not to the object: while its spawn or attach is in flight the
id is listed under the object being installed, so what the console queues then is applied to that object (the
pane is the same one). When the spawn raises, the failure path puts the id's entry back — the predecessor,
or nothing — before it awaits anything and before it ends the queue, so a request still queued there reaches
the incarnation that owns the id again, or, with no predecessor left, nothing at all, and never the object
that never started.

An exit code the core learns after the death record rewrites that row once, with no second lifecycle event.
The rewrite's identity check is read again after its worker screen read and immediately before the write,
because a resume of the closed id inside that read makes the successor the row's owner; the row write
itself is not taken under the id's resume lock, which a deferred retire holds while it waits for that
write.

Note that Windows' Proactor loop has no `add_reader` (`NotImplementedError`, verified on CPython 3.14.7),
so a self-pipe cannot be used to wake the loop; `call_soon_threadsafe` is the only mechanism, which is
why it must be one call and not one per session.

Cost at 50 printing sessions:

| quantity | before the core | after |
|---|---|---|
| output chunks/s through Python | ~3,600 (50 × 72) | 0 |
| Python wakeups/s from the terminal path | ~19,000 (projected) | ≤20, all sessions together |
| GIL holds for terminal work | one per chunk per socket | ≤20/s, each ≈40–60 µs for a 50-record drain → ≤1.2 ms/s |
| screen rebuilds for polling readers | 1 per reader per poll (`screen.display` per call) | 0 unless watched; a watched session costs one render per changed rev, on a native thread |

`last_output_at` keeps its meaning (unix seconds of the last byte) and is what
`to_dict["idle_seconds"]` and the "did it echo / is it quiet" wait (`wait_for_output_settle`) read, so both
are reads of the drain record instead of thread-shared fields.

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
write a socket itself: it pushes onto the socket's own bounded queue (`FrameQueue`,
`conpty_session/src/frame_queue.rs`). The socket's own thread is the consumer: it writes replay and
`replay_end`, then drains frames in order. The queue holds whole frames in arrival order up to its cap
(`CoreOptions::sink_queue_bytes`, 2 MB), drops the **oldest whole frames**
on overflow — a frame larger than the whole cap is dropped by itself, so a reader never gets half a frame
— and counts every dropped byte in its own `dropped_bytes`, so one slow client's loss is its own and not
the session's. `deliver` holds the queue's mutex only for the append, never across a consumer's work.
The per-handle count moves into the drain record instead of being summed per session (§12.6), so a session
with three sockets reports three counts, and each is marked reported only by the drain that carried it. A
retired id's socket is closed with code 1000 and its queued frames are discarded. A client that accepts nothing for
30 s is closed with code 1000. A retired id's socket drains what is still queued, is told `closed`, and
the queue's memory is released when the socket ends. One sink's panic costs that sink and nothing else:
`deliver` is wrapped in `catch_unwind`, the panicking sink is detached and counted in `stats.panics` and
logged once with its handle, the other sinks still get the frame, and the session keeps running. No lock
in the delivery path skips its work on poison, so `attach_sink` can never return a handle it did not
register.

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
drain. Before the core, `put_nowait` on `asyncio.Queue(maxsize=500)` did the same thing silently; the
count is now visible. A socket that has not made write progress for 30 s is closed (code 1000) instead of
pinning a thread and a 2 MB ring forever.

The hub keeps each socket's cumulative count by `(session id, handle)` — the core numbers handles per
session id, so two sessions' handles collide — and logs each increase against that socket
(`_note_socket_drops`, `session_manager.py:3988`). It is never added to the session's ring loss
(`dropped_bytes` in the drain record is the ring's), and an id's entries go when its `retire` succeeds or
the hub stops.

### 5.5 Threading

| before the core | after |
|---|---|
| pywinpty reader thread + loopback socket per session | 3 native threads per session in the core, no GIL, no socket: `core-read-<id>` blocked in `ReadFile`, `core-write-<id>` blocked on its queue, `core-reap-<id>` blocked in `WaitForSingleObject` (§5.3) |
| `_read_loop` thread per session | gone |
| `call_soon_threadsafe` per chunk per socket | ≤1 per 50 ms for the whole hub (§5.3) |
| console `/ws/pty` sender task per socket (uvicorn + asyncio) | 1 core thread per console socket, no asyncio, no uvicorn |
| `taskkill` process spawn per terminate | `TerminateJobObject` on the session's job |
| ~5 hub threads per session (winpty reader, socket, asyncio task, accept, cleanup) | hub: 0 per session; core: 3 per session, 1 per attached console socket, and fixed ones: `core-ticker`, `ws-accept`, `ws-handshake-<n>` (≤16 at once) and `core-close` per close in flight |

The core's readers are native threads that spend their life blocked in `ReadFile`, so they cost no
scheduling; the writer and the reaper are the same, blocked on a condvar and a wait. An optional later
backend (overlapped pipes on a single IOCP) would collapse the readers, and is worth adding only if the
load test shows thread count, not CPU, is the limit — at 50 sessions the core alone is ~150 threads plus
one per attached pane, so §11's "hub threads" criterion is measured on the hub's Python threads
(`threading.active_count()`) and the process's thread count is recorded beside it, as a number rather than
a gate.

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
  (`session_manager.py:2953`) the way it archives a failed exit. The console shows a crashed pane, not a
  frozen one. The hub maps that record exactly: `crashed = true` or `exit_code = None` ends the session as
  `crashed` for every engine, generic and pwsh included, and the lifecycle event carries `exit_code=None`;
  a code the core learns after the death record (SF3) rewrites the archived row once, without a second
  event. A panic in the ticker thread is caught and logged and the thread continues; a panic on the accept
  thread refuses that connection and keeps the listener. A panic from a `FrameSink::deliver` costs **that
  sink**: each delivery is wrapped in `catch_unwind`, the sink is detached, counted in `stats.panics` and
  logged once with the session id and its handle, the other sinks still get that frame and every later one,
  and the session's reader, screen and scrollback are untouched (§5.4).
- An error *returned* by one item is handled the same way as a panic: it costs that item, never the
  loop. A socket write that fails closes that socket; a frame a sink cannot take is dropped and counted;
  an accept that fails is logged and retried (§6.4). Only the reader's own end of pipe ends a reader, and
  only `shutdown` ends the ticker and the accept thread. A `?` that leaves a native thread's loop on any
  other error is a bug. Master `3874299` is the Python case of this: one tool output too large to store
  raised out of the transcript watcher and ended it for the session's remaining life. The drain handler
  isolates sessions the same way: each record is applied inside its own guard, in the tick and in the
  consumer's worker, so an exception while handling one session's record (a scrollback write that
  persistence refuses, a broken archive) is logged against that session and the drain goes on with the
  next; the stop's forced final rows are guarded per session too (`_write_remaining_final_rows`,
  `session_manager.py:7243`).
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

**The handshake never runs on the accept thread.** The accept loop hands each connection to a
short-lived `ws-handshake-<n>` thread and keeps only `accept` (`listener.rs:602`). At most 16 handshakes
are in progress at once (`MAX_PENDING_HANDSHAKES`); a peer over the cap is answered `503 Service
Unavailable` in the accept loop itself and dropped, never queued. Each handshake has one total deadline
of 5 s (`HANDSHAKE_DEADLINE`, enforced by a timer that shuts the stream down — the socket's read timeout
bounds one read, so a peer that sends a byte more often than that would otherwise hold its slot forever),
and a handshake cut off by it is refused `408`. A peer that connects and says nothing, or dribbles its
request, therefore costs one of 16 slots for at most 5 s and never delays another pane's attach. Refused
handshakes are logged at the accept loop's cadence, the first and every hundredth of each HTTP status,
and the line carries the status the wire answered (401 for a missing, unknown, expired or reused ticket, 403
for a valid ticket of another session, 404 for the path, 408 for the deadline, 410 for an ended session,
503 for a busy one or a core shutting down) and never the token.

**Ticket.** `core.ws_ticket(session_id, ttl_ms=30_000)` returns
`ws://127.0.0.1:<port>/ws/pty/<session_id>?ticket=<token>`; the token is
`nonce || base64url(hmac_sha256(key, session_id ‖ 0x00 ‖ expiry_unix ‖ 0x00 ‖ nonce))`. `key` is 32
bytes from `BCryptGenRandom`, one per hub process, held only inside the core — which is why the core mints
the ticket instead of the hub, and why the sidecar design's `hello` handshake is no longer needed at all.
The hub exposes `GET /api/sessions/{session_id}/terminal-ticket` → `{"url", "expires_in"}`
(`server.py:1316`). The handshake verifies the MAC, the expiry and that the ticket's session id equals the
path's, and remembers the nonce until expiry so a ticket is single use.
The hub 404s before minting for an id it does not list and for a listed id whose core entry is gone
(retired, or closed for good); with no core built yet it 404s too, and a ticket never builds one (the lazy
start `/health` keeps). A listed session that has already ended but whose core entry still exists **does**
get a ticket: the pane attaches an ended session's terminal and shows the final replay, which `/ws/pty`
always allowed, so the ended state alone is not the 404 test. A starting session (no first alive record
yet) and a text session get one as well. `expires_in` is the whole seconds left of that mint, and each
call mints a new ticket because a ticket is single use and a suspended tab's has expired. The mint reads
the core's session table, so a missed 50 ms deadline answers a retryable 503 in a worker rather than
blocking the route. The token is a bearer credential: it appears only in the response body — never in a
URL the hub serves, so uvicorn's access log cannot carry it — and never in a hub log line.
The nonce table is a bounded store: at most 4096 remembered tickets, expired ones pruned by a heap pop on
every mint and every consume (never a walk of the table), and a mint past the cap evicts the ticket
closest to its own expiry. `ttl_ms` is capped at five minutes, so a caller cannot pin a nonce
indefinitely; an evicted ticket is refused as unknown (401) and the console's reconnect fetches a fresh
one.
`core.ws_ticket` reads the session table, so the route awaits it in a worker thread. The answers are: 404
when the hub does not list the id, when the hub has no core, or when the core's entry is gone
(`KeyError`); 503 with `{"detail": "终端服务忙，请稍后重试"}` and `Retry-After: 1` when the table missed
its read deadline (`TimeoutError`) — the console retries every non-404 answer on its existing backoff, so
a busy table is a retry, not a fault; 500 only for a genuine core fault; otherwise
`{"url", "expires_in"}`. `/health` also carries `terminal_ws: {listening, port}` from `ws_listening()` and
`ws_port`, null while the hub has no core, so a dead accept thread is visible before a pane fails.

**Frames and semantics.** The core reproduces `/ws/pty`'s contract, because both the console JS and
`test_hub_persistence.py:276` parse it:

- Replay: on connect the core sends the *ring as it stands* — the joined raw ring, already collapsed by
  the in-loop W9 trigger when it fired, and **not** the 512 KiB `_scrollback_blob` trim — followed by one
  more `strip_color_queries` pass over the joined text (`esc.rs:53-54`, the hub's `OSC_COLOR_QUERY_RE`;
  the per-chunk strip cannot see a query split across two chunks, so this second pass is what removes
  it), then `{"type":"replay_end"}`. `isReplayEndFrame`
  (`static/js/session-workspace.js:29-33`) accepts a server that sends the marker, and the 1.5 s fallback
  timer covers one that does not, so no JS change was needed for the marker itself.
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
  thread starts; a text-session resume passes it as `attach_text(seed=…)` (no hub caller since DSH was dropped;
  the A1 handover still tests it). The handover runs
  under the delivery lock: the predecessor's pending batch is delivered to the sinks, the predecessor is
  marked `superseded` (anything its reader still files is dropped and counted, never written after the
  banner), the successor becomes the id's replay source, and then the banner goes out. A sink attached
  during that swap is kept exactly once: it lands either fully before the handover (its replay includes
  the old bytes and the banner) or fully after it.
- W8 on resume: `spawn(strip_esc3j_on_output=true)` drops `ESC[3J` with the same 3-byte carry.
- Input: `{"type":"input","data":…}`, `{"type":"resize",…}` and bare text are handled as before. Frames are
  parsed by `serde_json` (decision `json-reader`, owner 2026-11-15); a frame is an object only when it is
  exactly one JSON value and that value is an object, which is `json.loads`'s rule: trailing junk, a
  non-JSON number (`+1`, `01`, `1.`), a bad escape or an unterminated string makes the
  whole frame bare text, and bare text is written as it arrived. `NaN` and `Infinity`, which `json.loads`
  accepts, are bare text too, as they were with the old reader. Duplicate keys keep the last value. A
  frame nested past serde_json's 128-deep limit is bare text, as one past the old reader's 64 was; a
  number past `f64::MAX` (`1e400`) is bare text where the old reader read `inf` and then refused the
  resize, the one frame the console never sends whose meaning changed. `"data"` that is a string is
  written as is (after the reply strip); absent or `null` writes nothing; any other type is ignored and
  logged once per socket — Python wrote `str(data)`, the console never sends it, and reproducing
  Python's `repr` is not worth a code path. A lone surrogate escape, which `json.loads` keeps and a Rust
  `String` cannot hold, is still U+FFFD: such a frame is read a second time with that escape replaced and
  paired escapes copied untouched; any other frame `serde_json` refuses stays bare text. `resize` takes
  `cols`/`rows` as JSON integers (truncating a fraction as `int()` does) or as the ASCII form `int()`
  accepts, with whitespace and a sign; `propagate` is `bool()` of the value and `true` when absent; a size
  `int()` would refuse, or one outside `1..=65535`, is no resize at all. OSC terminal replies are
  stripped in the core with `OSC_TERMINAL_REPLY_RE`'s pattern
  (`OSC_TERMINAL_REPLY_RE`, `session_manager.py:2000`) before any input reaches the PTY. A resize is
  **not** applied by the core: it raises a `resize` dirty flag with the requested cols/rows, and the hub
  applies the 20/5 clamp and the single-window propagation (`resize_session_from_client`,
  `session_manager.py:4301`) on its next tick, because propagation is policy. Worst case that is one
  drain interval (50 ms), well inside what a pane refit already costs.
- Slow client: per-socket 2 MB ring, oldest whole frames dropped and counted (§5.4); a socket that makes
  no write progress for 30 s while it still owes the peer bytes is closed with code 1000. "Owes" counts a
  write in flight — the replay above all, which goes out before the first frame — and not only a
  non-empty frame queue; an idle pane owes nothing and keeps its socket. Writes block for at most a 250 ms
  slice (`WRITE_SLICE`), tungstenite keeps an unfinished frame in its own buffer and the socket thread
  resumes it, so a close request and the stall clock are never more than one slice behind.
  `close_sockets`, `retire` and `shutdown` shut the stream down when they find a blocked write instead of
  waiting for it: they return at once and the thread ends within a slice. Before the core,
  `asyncio.Queue(maxsize=500)` dropped silently; the new count is per handle in the drain record.
- **Close.** A permanent close (`close_session`), a hub stop and a `retire` each close the id's sockets
  with code 1000 (`close_sockets`; `_close_terminal_sockets`, `session_manager.py:3636`); the close path
  and the stop run that off the loop, and the stop's batch shares what is left of its 8 s budget. A
  natural process death and a resume close nothing (A1): the pane keeps its socket across the resume, and
  the retire is what ends it.
- permessage-deflate: uvicorn's default offers it (`ws_per_message_deflate: bool = True` in the installed
  uvicorn 0.42.0 config source; the lock pins 0.54.0, unverified there), so today's frames are
  compressed. The core starts **without** deflate: the link is loopback, so bandwidth is free, while
  compression costs CPU on both ends for a stream the browser re-parses anyway. `tungstenite`'s deflate
  support (unverified) keeps the option open if §11 shows a difference worth having.

**Console JS changes.** `hubApi.ptySocketUrl(id)` is gone; `hubApi.terminalTicket(id)`
(`static/js/hub-api.js:82`) fetches `/api/sessions/{id}/terminal-ticket` with a 5 s bound, and the pane's
`connect()` (`static/js/session-workspace.js:1100`) opens `new WebSocket(ticket.url)`. Everything else
stays — replay buffering, the marker, reconnect backoff, the reply-drop window. A 404 takes the console's
closed-pane path (`terminalClosed`, no retry); a timeout, a 503 or a missing `url` takes the existing
reconnect path, so a restarting hub looks exactly as it did before. One attempt identity per fetch keeps
a late response from opening a socket for a pane that was already removed. `test_terminal_seam` pins the
shape: no `ptySocketUrl`, no `/ws/pty/` in `hub-api.js`, `terminal-ticket` present, `new WebSocket(url)` in
the workspace.

### 6.5 GIL rules and the event loop

- Every `pymethod` that can block (spawn, kill, screen, scrollback, raw, drain, `snapshot_all`) releases
  the GIL for its duration (`py.detach` in PyO3 0.30).
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
- Every blocking call of the stop runs off the loop. The one 8 s budget starts at entry and every later step gets
  what is left of it. The reconcile drains at the top are not part of that bounded work: a drain only takes the
  records the core already queued, and the core's table read inside it is bounded at 50 ms, so a reconcile drain
  is a loop call by intent (§5.3). The steps that still run after the budget is spent get a 0.05 s floor each,
  and the producer-cancel wait keeps its own 0.3 s, so with a wedged core the stop can reach about 9 s — never
  8 s plus a fresh per-step timeout. `snapshot_all`, `kill_all`, the `close_sockets` batch and the final rows
  each get a share of what is left, and a share that is already spent skips its step; the consumer drain and the
  request workers a cancelled consumer left running share one bounded wait. Every archive path (the stop's and
  the close's) and the forced final rows are handed a worker snapshot; a session whose reads missed gets an
  empty one, which stores nothing and so keeps what an earlier persist wrote, because a live read there is
  exactly what would stall the stop again.

**Retire retries and their cap.** A `retire` the core refuses (an exit record or code no drain has
carried yet, a spawn/attach/retire in flight for that id, a busy table) keeps the id pending, bound to the
incarnation it was dropped for, and one background timer tries again: every 250 ms
(`RETIRE_RETRY_INTERVAL_SEC`, `session_manager.py:447`), and only the timer's own attempts count towards
`RETIRE_RETRY_MAX_ATTEMPTS` (40, ≈10 s), so a busy drain's record-driven retries cannot spend the budget.
Passing the cap does not abandon the drop: it logs one line with the real refusal count and retries every
5 s (`RETIRE_RETRY_SLOW_SEC`) until `retire` succeeds or the core no longer knows the id. Only a resume of
that exact incarnation cancels the pending drop, because its `spawn`/`attach_text` replaces the entry and
takes the sink fan-out over (§5.4); a retry re-reads the id's incarnation after every awaited core result
and queues nothing for a generation a resume has replaced. A retired id never happens before the row that
needs it: a code the core learned after the death record is written to the archived row first, so `retire`
cannot take the answer away from it.

A non-retryable `retire` failure (`RuntimeError` from the core's panic wrapper or a seam `TypeError`) drops
the tombstone and socket diagnostics and logs the id once; it does not start a delayed retry.

### 6.6 Lifecycle and shutdown

- Sessions end with the hub by construction: the module and its threads die with the interpreter, so
  `shutdown-ends-cli` needs no supervision code — but it does need ordering, or agents are left with a
  dead MCP endpoint. Shutdown keeps the recorded order (`graceful-budget`): one reconcile drain, then a
  second one drain interval later (50 ms, `SHUTDOWN_RECONCILE_DRAINS`, `SHUTDOWN_RECONCILE_GAP_SEC`) —
  both inside the budget — so a death that happened just before the stop keeps its ordinary finalization
  (native transcript drain, final row, one lifecycle event) and only the deaths `kill_all` causes take the
  stop's own archive path; then stop producers, `snapshot_all(max_lines=250, max_bytes=512 KiB)` per live
  session, `kill_all(grace_ms=2000)` — every session terminated in parallel inside one 2 s budget,
  `TerminateJobObject` first and `ClosePseudoConsole` on a helper that may outlive the call — then archive
  inside the shared 8 s budget (`SHUTDOWN_MAX_SEC`) and stop the core's ticker last. `snapshot_all` returns
  what `persist_session` archives today, so the rows do not change. Anything the budget leaves unarchived
  still gets its final row before the store closes, each session inside its own guard: one session's
  failing row costs that row only.
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
beside `server.py` is importable in both run paths. The core replaces `pywinpty`; neither `pywinpty` nor
`pyte` is installed by the portable runtime, and step 6 deleted the dev-only oracle with its renderer and
tests.

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

**Portable build.** The release script builds the core with the same path remapping as the EXE; `strip =
"symbols"` alone leaves Cargo registry paths in the `.pyd`. The payload is an explicit manifest:
`desktop/portable/payload.txt` lists repo-relative
paths, `build.rs` `embed_portable_resources()` embeds each file into the EXE and hashes them into
`PAYLOAD_ID`, and the app unpacks them to `resources/<PAYLOAD_ID>/` where `server.py` lives
(`portable_runtime.rs:18`). So the `.pyd` joins that manifest (`desktop/portable/hub_terminal_core.pyd`,
produced by the release script before `pnpm build:portable`), lands beside `server.py` in the unpacked
resources, and needs no venv change at all. Two consequences to handle in the release script:
- `desktop/scripts/check-portable-privacy.mjs` scans every payload member for host paths and credentials
  and rejects member names ending `.exe`/`.pdb` (a `.pyd` passes the name rule). It runs `checkPe` on every
  `.pyd` payload member. A stripped Rust PE can still carry a CodeView path, so the `.pyd` uses the same PE
  check the EXE gets; if the gate trips, that is a build-config fix, not a gate exemption.
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

The versions below are the ones `terminal-core/Cargo.lock` resolves today; "in the app lock" says whether
the same version already sits in `desktop/src-tauri/Cargo.lock`, so a developer's registry copy is warm.

| crate | version | purpose | in the app lock |
|---|---|---|---|
| `pyo3` | 0.30.1 (`abi3-py311`, `extension-module`) | the extension module, `#[pymethods]`, `py.detach` (was `allow_threads`), abi3 | no |
| `tungstenite` | 0.28.0 (default features) | RFC 6455 handshake, frames, ping/pong, close handshake on the console socket; blocking, no async runtime | no |
| `serde_json` | 1.0.154 (default features: no `derive`, no `arbitrary_precision`; builds `serde_core`, `itoa`, `memchr`, `zmij`) | parses the socket's input frames (§6.4 "Input", decision `json-reader`, owner 2026-11-15); the map keeps the last duplicate key and a nested frame is bounded by its recursion limit (128) | yes |
| `hmac` | 0.12.1 | HMAC-SHA256 for the terminal ticket | no |
| `sha2` | 0.10.9 | hash primitive under `hmac` | yes |
| `base64` | 0.21.7 | ticket encoding (`URL_SAFE_NO_PAD`) | yes |
| `windows` | 0.62.2 (`Win32_Foundation`, `Win32_Security`, `Win32_Security_Cryptography`, `Win32_Storage_FileSystem`, `Win32_System_Console`, `Win32_System_Diagnostics_ToolHelp`, `Win32_System_Environment`, `Win32_System_IO`, `Win32_System_JobObjects`, `Win32_System_Pipes`, `Win32_System_Threading`) | ConPTY, job objects, pipes, ToolHelp, `BCryptGenRandom` | yes (crate present; these features are not enabled in the app's manifest) |
| `vte` | 0.15.0 (`alacritty_terminal` was approved with it but is not used, §7.2) | VT parsing; the screen model is the core's own copy of pyte's | no |
| `regex` | 1.13.1 | the two OSC patterns, only run when the buffer contains `\x1b]` | yes |
| `unicode-normalization` | 0.1.26 | pyte NFC-composes every combined cell; this crate supplies the same tables (decision `nfc-compose`) | no |

The socket's input frames are parsed by `serde_json`; `ws/input.rs` keeps only the thin `json.loads` layer
around a parsed value (decision `json-semantics`). Other deliberately unused crates are `crossbeam-channel`
(the writer queue is a std condvar and `VecDeque`), `parking_lot` (std mutexes are enough), `tokio` (no
async runtime in the core), `unicode-width` (the parser's grid owns cell widths), `flate2`
(permessage-deflate stays off until §11 shows a reason), `winpty-rs` and `portable-pty` (§7.1).

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
| pyte NFC-composes a cell when a combining mark joins it (`unicodedata.normalize("NFC", cell.data + mark)`); the core does the same in `screen.rs` `combine` with `unicode-normalization`, so a pair with no precomposed form keeps both code points, a cell that already holds a composed character composes again only where the table allows it, and a wide character's stub cell holds the mark exactly as pyte's `Char` does | `HubTerminalScreen.draw` vs pyte's `draw` | `terminal_stream` depends on `unicode-normalization` 0.1.x (owner, 2026-11-14, `nfc-compose`); `screen_exceptions.jsonl` is empty and stays as the mechanism (a stale row still fails the gate); `edge-screen-combining` and the `screen.rs` unit tests pin composition, canonical order, the no-precomposed-form case, a composed cell taking another mark, and a mark on a wide character's stub. The crate's tables are Unicode 18.0 against Python 3.15's `unicodedata` 17.0.0, a difference only a character added in Unicode 18 could show |
| B9: liveness is the process, never the status string | `is_process_alive` `:2562` | `alive` in the drain record, set false by the reader thread; no syscall and no round trip, and `to_dict["idle_seconds"]` reads the same record |
| D7/D1–D13: approval keys are byte-faithful | `write_input` `:2463`, call sites `:6718-7276` | `write` preserves arrival order per session through one writer thread; no interpretation, no translation |
| graceful shutdown ends every CLI and captures its scrollback inside the 8 s budget | `graceful-budget`, `:5671`, `:5082` | `snapshot_all` then `kill_all`, both with deadlines; on timeout the hub logs and archives what it has |
| scrollback is written at most every 60 s, only when changed | `_scrollback_blob` `:4443` | `raw.rev` replaces `raw_history_rev`; `_scrollback_written_at`/`_scrollback_rev_written` logic unchanged |
| `last_screen` = last 20 non-empty screen lines | `_last_screen_text` `:4379` | `screen` reply, same trimming in Python (policy stays there) |
| viewport clamp (cols ≥ 20, rows ≥ 5) and single-window propagation | `:2487`, `:3161` | a console `resize` arrives as a dirty flag on the drain record; Python applies the clamp and propagation, then calls `resize` per session. The core refuses cols < 20 / rows < 5 defensively and never propagates |
| text sessions have a screen without a PTY (core tests, corpus recorder) | `emit_virtual_ansi` `:2213` | `attach_text` + `feed` |

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
| `defa26b` (on master via `5abe2db`) | pyte dropped the rest of a text run after a zero-width character (ZWSP, ZWJ, VS16 in `⚠️`) | a zero-width character that is not combining takes no cell, starts no pending wrap, and the text after it is drawn | `edge-screen-*` zero-width cases; `test_terminal_core_screen_cases` |
| `a3dce22` (branch `fix/pyte-csi-esc-restart`, not on master yet) | pyte drew the rest of a CSI cut by an ESC, a C0 control or a DEL as text | §8 row; `vte` already reads them as a terminal does, no core change | `edge-screen-csi-cancel`, `edge-screen-csi-controls`; `test_terminal_core_screen_cases` |
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
`raw`/`scrollback`/`last_screen` keep their formats. `pyte` was an offline, dev-time oracle only: it
rendered the frozen corpus so the Rust parser could be compared against it, the hub never imported it after
step 3, and step 6 deleted it with its renderer and its tests; the committed goldens are the spec and the
corpus gate compares them against the core. No requirements file lists pyte (the portable manifest lost the
entry in step 5).

| step | work | gate | rollback | size |
|---|---|---|---|---|
| 0. Freeze behaviour, offline | a temporary dump in `_on_pty_output` records a few minutes of raw chunks per engine (the five PTY engines: claude, codex, omp, kimi, agy); `pyte` renders them and the dumps are frozen into `tools/fixtures/terminal/` (chunk stream, screen, scrollback 250, `raw` after trim, `raw_tail`); the dump is deleted. `pyte` is used only here, offline, and deleted at step 6 | fixtures committed; the dump is gone from the tree | files only, no runtime change | S, 1–2 d |
| 1. Extension skeleton | `terminal-core/` crate (`hub_terminal_core`, `cdylib`, `panic="unwind"`, abi3 gate §7.3): module, `Core`, threads, `spawn`/`write`/`resize`/`kill` via system ConPTY, one reader thread, no parser yet; `terminal_core.py` wrapper with the missing-module message | `cargo test` on CI; a manual `--selftest` binary spawns `cmd.exe`, echoes bytes, resizes, kills the tree; abi3 import works on 3.11–3.14 | branch only; nothing references it | M, 4–6 d |
| 2. Parse, history, coalescing | `vte` 0.15 + pyte's screen model in `terminal-core/terminal_stream`, W8 carry, OSC strips, W9 ring with golden fixtures, UTF-8 carry, `screen`/`scrollback`/`raw`/`raw_tail`, batches + the ticker, `drain`; step 2c fixes the screen-model divergences the review found (DECSTBM, DECOM, save/restore, zero-width, combining), orders delivery per sink, and deletes `csi_marker.rs`; step 2d makes the delivery state (lock, sinks, handles) belong to the session id, which step 4's sockets attach to | `cargo test` **and** `cargo test --features conpty_session/test-hooks` in `terminal-core/` (the delivery-order, resume-handover and panic-isolation cases need the hooks; neither the hooks nor their call sites exist in a release build): `terminal_stream/tests/fixture_corpus` parses all 33 cases under `tools/fixtures/terminal/`, byte for byte, and fails when any golden a case calls for is missing; `screen_exceptions.jsonl` is empty (the core NFC-composes with `unicode-normalization`), and a row that stops differing still fails the gate; coalescing preserves bytes and order; `delivery_order.rs` pins per-sink order and atomic attach, `sink_handover.rs` the resume handover, and both pass 20 runs in a row with the hooks on | branch only | M, 5-7 d |
| 3. PTY ownership in Python | 3a (done): every terminal read and write in the hub goes through `AgentSession`'s seam methods. 3b-core (done) delivered `FrameQueue`, `attach_sink`, `retire`, `raw_rev`, and the delivery-lock ownership fix; 3b-hub swaps the seam bodies for the core and deletes the Python PTY/parser path. | the full Python suite on the branch, with the ported live-consumer tests | branch only | L, 6–9 d |
| 4. Terminal sockets in the core | Done: the WS listener, tickets, replay/`replay_end`, input and OSC stripping, `close_sockets`, and console ticket fetch are in the core. The socket thread consumes `FrameQueue` directly through `attach_sink`; the temporary `attach_queue`/`take_frames`/`detach_queue` calls and Python `/ws/pty` path are removed. Per-socket `dropped_bytes` is reported per handle in the drain record. | the core WS tests, focused Python terminal suite, and the full suite | branch only | M, 4–6 d |
| 5. Packaging and the required import | Done: `payload.txt` and the release script produce and embed the `.pyd`; the privacy gate applies `checkPe` to each `.pyd`; `bootstrap.ps1` `Check-Imports` adds the resources directory; `.gitignore` is updated; `requirements.in`/`requirements.lock` drop `pyte` and `pywinpty`. | the portable build starts on a machine with no cargo; a fresh from-source checkout fails with the documented message and starts after the two build commands | branch only; the merge is the cut-over | S, 2–3 d |
| 6. Cut-over merge and load test | merge; run §11 against `master` (recorded before the merge) on an idle machine; tune flush windows, ring sizes and the drain interval; with the goldens frozen, delete the pyte oracle (`tools/pyte_oracle.py`), its renderer (`tools/render_terminal_fixtures.py`) and the three tests that drove the oracle's classes, porting every case to the core first (`terminal_stream/tests/trim_history_cases.rs`, `test_terminal_core_screen_cases.py`), and freeze the live consumers' stop-point screens as `screen-at-consumer-read.txt`; the recorder's own live screen becomes a core text session; no pyte dependency entry remains. | §11's pass criteria; a full suite run on `master` after the merge | revert the merge | M, 2–4 d |
| 6a. Load-test tooling | Done: `tools/fake_tui_engine.py`, `tools/load_terminal_core.py`, and `test_fake_tui_engine.py` provide the recorded-rate replay, marker accounting, and probe harness described in §11; the full 300 s comparison ran on 2026-11-14 (§11's recorded table) and needed no tuning. | tool tests and the 60 s smoke runs; the full comparison is step 6's gate | branch only | S, 1–2 d |

Total ≈ 5–6 weeks for one implementer plus review; steps 2 and 3 dominate, and step 3 is the only one that
touches most of `session_manager.py`.

## 10. Tests

Existing tests that cover this behaviour and what porting them means:

| test | pins | action |
|---|---|---|
| `test_tui_delivery.py` | checked-submit key sequence, one Enter, clear input, no screen-based proofs | replace the shared `FakePty` (`test_native_records.py:15`) with a fake core client that records the same `inputs` list; keep the assertions literally |
| `test_session_approvals.py` | approval keys reach the PTY, `get_clean_screen_text` patched per test | same fake client; patch the client's `screen` instead of the method |
| `test_hub_persistence.py` | scrollback blob written once per rev, `_scrollback_rev_written`; and one live core-socket connect (`:276`) | assert on the core's `rev`; point the socket case at the core listener with a minted ticket and keep the `replay_end` expectation |
| `test_restart_resume_on_start.py` | resume banner, continuation text, archived-at-stop paths | add: the seeded history and the banner arrive in one `spawn(seed_history=…, ws_prefix=…)` call and the banner precedes the first output frame |
| `test_event_loop_stalls.py` | loop latency under a write backlog | add a case that fires 20 drains of 50 records through the fake client; existing cases unchanged |
| `test_session_launch_args.py` | argument stripping/injection | unchanged; the shim table stays here with the shim (`cmd-shim-stays-python`) |
| `test_transcript_feed.py` | transcript rendering | unchanged |

New tests, none run by this unit:

- Rust, parser and history: the step-0 corpus (`tools/fixtures/terminal/`, rendered screen-for-screen against the frozen Python
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
  the step 0 recorder's stop points: `live-codex-scrolled-paste` (Codex at
  80×24, a 945-character follow-up typed unsent so the composer scrolled and the `↑` mark shows on its
  top visible row) and `live-omp-approval-box` (an omp approval box at 120×36 whose `Command:` row runs
  to the frame's edge and is cut with `…`; the recorder denied it with Escape and never approved it).
  Both record `consumer.reads_at_chunk`, the chunk a replay stops at to reproduce exactly the screen the
  consumer read, and `test_terminal_live_consumers.py` feeds that prefix through the core (`attach_text`
  + `feed`), asserts the screen equals `screen-at-consumer-read.txt` (the oracle's last answer, frozen
  before step 6 deleted it), and runs the real consumers on the core:
  `_codex_composer_shows_whole` accepts the settled composer, and the omp box parse yields the same tool,
  command and `command_truncated` flag (True) from both. Their older tests pass the screen in as a
  string; these are the only ones that feed recorded bytes through the core screen model.
- Rust, build config: CI asserts the extension's profile unwinds (a `cargo metadata`
  `profile.release.panic` check for the core crate), because a stray `panic = "abort"` silently turns
  every caught panic back into a hub kill.
- `test_terminal_ticket_endpoint.py`: the ticket route and `/health` on the fake core — 404 for an unknown id, a listed id with no core entry and a hub with no core; 200 for a starting, an ended-but-listed and a text session; the `{"url","expires_in"}` shape and one new URL per call; the token absent from every log record and from uvicorn's access log; `terminal_ws` for a live listener, a stopped one and no core (which is never built).
- `test_terminal_core_contracts.py` (`DrainRequestTests`): the hub's application of a drain record — resize requests in arrival order with `propagate` false/true, the 20x5 clamp, taken once; a bad request costs itself only; `socket_drops` per `(id, handle)` and apart from the ring; a close asks the core once and forgets the id.
- Python and Rust, oracle cases ported at step 6: `test_terminal_core_screen_cases.py` drives the core
  with the exact inputs of the deleted oracle tests (`test_terminal_screen_zero_width.py`,
  `test_terminal_stream_csi.py`) — zero-width draws and splits, combining, marker CSIs whole and split,
  DECAWM, CAN/SUB, `$p`, and ESC/C0/DEL inside a CSI — and `terminal_stream/tests/trim_history_cases.rs`
  ports the W9 trim cases the unit tests did not cover. The core keeps no cell attributes, so the oracle's
  `bold`/`fg`/`underscore` assertions are not ported; `vte` does not read `0x9b` as a CSI start, so an
  eight-bit CSI renders its tail as text (no recording contains one).
- Python: `test_terminal_core_client.py` — the wrapper's missing-module message, deadline expiry returns
  the cached snapshot and marks `degraded`, `drain` clears flags so a second call returns nothing,
  `crashed` sessions archive as crashed rather than hanging a call, and the ticket endpoint 404s for an
  unknown id or a core entry that is gone while an ended-but-listed entry still returns its final replay.
- Console: the repo has no JS test harness, so the ticket fetch (`terminalTicket` and `connect` awaiting it)
  is covered by a manual pass per engine in step 4's gate: replay, reconnect after a hub restart, input,
  resize and the resume banner, plus a rejected-then-successful fetch, a pane disposed while its ticket is
  in flight, overlapping attempts, two tabs of one session, a tab suspended past the TTL, a 404 that closes
  the pane, and a restart with a pane open.
- Load/soak: §11.

## 11. Load test

Run only when the machine is idle; it is not part of the default suite.

- `tools/fake_tui_engine.py` replays a recorded corpus (JSONL of `(delay_ms, chunk)` from step 0) at the
  recorded rate to stdout, then prints a final byte count. Started as a normal session, so the whole real
  path is exercised (system ConPTY, core, hub, console), with `--speed 1` replaying the recorded rate
  (1.9–12.7 chunks/s per `live-*` case; `--speed` raises it, for example `--speed 6` is about 42 chunks/s
  average).
- `tools/load_terminal_core.py` starts 15 / 30 / 50 sessions, attaches one WebSocket consumer each, samples
  for 5 minutes and records: hub CPU (`GetProcessTimes` of the hub process, in cores), thread count, event-loop
  lag (a coroutine measuring `sleep(0.05)` overshoot, p50/p99/max, the technique `test_event_loop_stalls.py`
  already uses), GIL-held share (py-spy `--gil` if available, otherwise loop lag and CPU as proxies),
  `core.stats()` counters, drain count per second (expected ≤20), and per-session byte accounting. The
  latter needs a per-session byte counter from `core.stats()`; today the hub drops the drain record's
  per-session counters, so the load tool compares the core's global `bytes_total` with the consumers' sum
  and relies on the hub's socket-drop counter and numbered marker lines for loss. The core's
  `dropped_bytes` is not a loss check: it counts what the raw history ring trimmed (output older than its
  cap), which a long run at 50 sessions reaches by design.
- Read event-loop lag against a baseline measured on the same machine before any session spawns: the probe's
  floor on Windows is one timer tick, about 11 ms at 50 ms granularity, and under load the loop wakes more
  often, which lowers the floor.
- Method notes: lag is measured by a coroutine injected with `sys.remote_exec` (fallback: a `GET /health`
  round trip, labelled); context switches per second are not measured because Windows has no cheap per-process
  counter; the tool sets `HUB_PROFILES_PATH` under its data folder because the hub's default profiles file is
  the live hub's (`%APPDATA%\mcp-agent-hub\agent_profiles.json`), so any second hub must do the same.
- Pass criteria at 50 sessions: hub CPU < 0.5 core, p99 loop lag < 50 ms, no lost bytes, Python-side
  wakeups < 1,000/s, **Python-side hub threads < 20** (the core runs three native threads per session plus
  one per console socket, so the total OS thread count at 50 sessions is about 200 by construction), and
  console FPS ≥ 30 while a session prints continuously.
- The comparison is against `master` before the cut-over merge: the same script pointed at a hub on the
  pywinpty code (the recorded 12-session profile in §2 is the baseline). There is no `off` mode to compare
  against after the merge, which is the point of removing the flag.

Recorded on 2026-11-14 (integration branch with the core vs `master` `ebe4740`; 300 s per run, `--speed 1`,
on the owner's working machine rather than an idle one, so every number is an upper bound for the hub
itself; idle lag floor 11.2–11.7 ms in every run):

| tree | sessions | hub CPU cores p50 / p99 | OS / Python threads | loop lag p50 / p99 / max (ms) | wakeups/s p50 | markers lost | socket drops |
|---|---|---|---|---|---|---|---|
| core | 15 | 0.07 / 0.14 | 70 / 4 | 11.3 / 13.8 / 25 | 16.3 | 0 of 1,589 | 0 |
| core | 30 | 0.11 / 0.18 | 130 / 4 | 11.3 / 13.8 / 43 | 16.3 | 0 of 3,223 | 0 |
| core | 50 | 0.19 / 0.34 | 212 / 6 | 11.3 / 14.0 / 57 | 16.3 | 0 of 5,414 | 0 |
| master | 15 | 0.28 / 0.68 | 82 / 33 | 5.3 / 17.0 / 91 | 158 | 0 of 1,596 | n/a |
| master | 30 | 0.60 / 1.20 | 157 / 63 | 4.1 / 58.2 / 2,481 | 327 | 0 of 3,210 | n/a |
| master | 50 | 0.90 / 1.19 | 255 / 103 | 3.9 / 156.1 / 8,495 | 512 | 0 of 6,060 | n/a |

At 50 sessions the core passes every measured criterion: 0.19 cores, p99 lag 14.0 ms, no marker lost and no
socket drop, 16 wakeups/s, 6 Python-side threads. Its one lag sample above 50 ms (57 ms) is a single 50 ms
probe in 4,911. Master fails CPU and lag from 30 sessions on and stalls its loop for 2.5 s and 8.5 s. The
recorded rate is lighter than §2's profile (about 350 chunks/s at 50 sessions), so a `--speed` run remains
the way to probe headroom; no flush window, ring size or drain interval needed tuning at these numbers.
Console FPS is the owner's manual check.

## 12. Risks and open questions

1. **Parser parity.** The approval and composer parsers match exact rows. `alacritty_terminal` may render
   a cell differently from pyte in ways nobody has hit yet. Mitigation: step 0's frozen corpus, rendered
   both ways in step 2's tests; an unexplained diff is a port bug, and one that survives review blocks the
   merge rather than getting a per-engine exemption (there is no second path to fall back to). `vte` does
   not read U+009B (an eight-bit CSI start) as a CSI, so `"\x9b<uhello"` renders `<uhello` where pyte
   rendered `hello`; ConPTY emits only seven-bit sequences and no recording contains one, so this remains
   a known limitation.
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
6. **Per-socket drop accounting and the replay render under the delivery lock.** Step 4 implements both:
   `dropped_bytes` is carried per socket handle in the drain record, so one client can learn its own loss,
   and `attach_sink` holds the delivery lock across `replay_text()` (one ring join plus one strip pass,
   ≈1.5 ms at the 512 KiB cap). The load test (§11) measures the cost; there is no remaining per-session
   sum or deferred implementation decision.
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
15. **The Codex scroll mark can sit flush against the text (found and fixed).** Codex puts its `↑`
    mark in the composer's last column, so a top visible row filled up to that column ends
    `… without changing↑`. `TUI_CODEX_SCROLL_MARK_RE` wanted whitespace on both sides, so
    `_codex_composer_shows_whole` rejected a settled, fully typed draft and the settle loop ran to its
    cap before pressing Enter (found by the first `live-codex-scrolled-paste` recording; the committed
    one was recorded with wrap slack). Fixed in Python per `oracle-bugs` on `fix/codex-scroll-mark-flush`
    (`a5d8272`): the mark needs whitespace only after it. Master takes it with the owner's OK; the core
    changes nothing, it only has to draw the mark in the same cell as pyte, which the case checks.

16. **A narrow failed-resume identity limitation.** Drain records carry no incarnation. If a PTY resume
    raises after its process has already started, that process's death record is routed by id to the restored
    predecessor (or its tombstone), so the old row can take the failed spawn's exit code. The spawn must raise
    after the process started for this to occur; the limitation is known and not fixed here.

Open questions for the owner: none blocking. Two are deferred by design rather than unanswered: whether
the `.pyd` ultimately ships as abi3 or per-minor follows from step 1's abi3 probe, and permessage-deflate
stays off unless §11 shows a reason to turn it on.
