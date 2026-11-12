# Decisions

Design decisions confirmed by the owner. Change a line here before changing the behaviour it records.

## Event-loop stalls (2026-11-12, owner: batch decision page, all defaults)

| id | decision |
|---|---|
| fastmcp-log | fastmcp logs through its plain handler, not rich: a rich traceback held the loop 1.6-1.75 s per failed tool call. fastmcp lines in hub.log are `ERROR: ...` plus a standard traceback. |
| dsh-tls | Every hub call to the DSH host passes `verify=dsh_tls_context()`, one shared TLS context built off the loop at start. The five synchronous `httpx.post` calls to DSH stay synchronous: awaiting them could start two continues for one session without a per-session lock. |
| record-drain | The DSH session-record drain yields after each payload and still stores the session per payload, which keeps a stop mid-batch from recording a payload twice. |
| sniffio | The portable runtime ships `sniffio`: httpcore imports it at every lock setup and connection close, and a missing package meant a disk search each time (0.2-0.85 s stalls). |
| release | Merge and build at once; restart the live hub only on the owner's word, through `resume_on_start.json`. |

## Parent waiting protocol (2026-11-10, owner: systematic optimization)

| id | decision |
|---|---|
| opaque-wait | Parent-facing control calls return only accepted/session/turn identity and an opaque wait handle. `GET /api/wait/<handle>` owns the durable state, lease, request inbox and result delivery. |
| wait-transport | The wait URL is the only parent wait transport. A timeout is retried against the same URL; no cursor, session list or helper script is exposed. |
| auto-approval | Automatic approval rules are session-scoped, bounded by structured request matching, expiry and use count, and lower priority than hard-deny and forced-manual-approval rules. |
| durable-inbox | Wait handles persist session sets, turn ids, request/result deduplication and leases. One active waiter is allowed; a replacement returns `superseded` to the old connection. |
| wake-policy | Only approval/question, delivery failure, completion, exit/crash and Hub shutdown wake a parent. Tool/progress/delivery telemetry remains audit-only. |

## Agent prompt language (2026-11-10, owner)

| id | decision |
|---|---|
| english-guidance | Hub-authored MCP instructions, tool descriptions, routing guidance and interaction reply instructions use concise English. Shared waiting rules have one source. User interface labels and messages stay Chinese. User-authored tasks, profile descriptions, routing notes and native records are preserved verbatim. |

## Background persistence (2026-11-10, Hub lock contention fix)

| id | decision |
|---|---|
| buffered-durability | Ordinary telemetry and mailbox success means accepted by the bounded background queue. Forced process termination can lose accepted, uncommitted data. Normal shutdown drains accepted writes; `persisted_in_sqlite` is true only after commit confirmation. |
| sqlite-writer | Keep the existing schema and one Hub per database. Require WAL and `synchronous=FULL`; a dedicated thread owns the only writer, with ordered batches of at most 128 logical units / 25 ms. Lock waits are 100 ms with background retry. |
| bounded-admission | Limit pending writes to 4096 units / 32 MiB. External admission and consistency barriers wait at most 2 seconds before returning temporary unavailability. Internal watchers wait for capacity without advancing their input cursor. |
| graceful-budget | Stop producers once, stop child processes concurrently (at most 2 seconds), capture final archives and scrollback, then drain within a shared 8 second budget. Log pending counts on failure/timeout. |
| committed-lists | Live session lists read memory and committed history without flushing. Archive details, restore, deletion and full history await a commit barrier. `/health` reports queue age/count, committed event sequence and storage errors without accessing SQLite. |

## Web console (2026-11-05, console UI redesign)

| id | decision | default kept |
|---|---|---|
| file-structure | Console front end is split into `static/css/hub-console.css` + ES modules under `static/js/`, served by the `/static` mount in `server.py` (no bundler). | yes |
| brand-name | Top bar shows "Sub-Agent Hub" with the Chinese subtitle "MCP 聚合网关 · 非无头终端总控台". | yes |
| profile-id-edit | A saved profile's ID is read-only; renaming is "复制为新 Profile" (duplicate) then delete the old one. | yes |
| terminate-confirm | Closing a session asks for confirmation only while its process is alive (changed 2026-11-06: was "while its status is starting / running / waiting_parent"); a session whose process has ended is removed in one click. | yes |

## Profiles and launching (2026-11-06, profile redesign; owner: "change it however you see fit")

| id | decision |
|---|---|
| profile-identity | A Profile is a launch recipe (engine, command, args, task mode, default model, default thinking level, and optional `when_to_use` hint). Model and thinking level are chosen at launch; variants per model/thinking are not Profiles. Old variant ids were dropped without aliases. |
| agent-guidance | Parent-agent routing uses the single global routing guide first, with per-model notes (`engine_policies.<engine>.model_notes`) as catalog metadata. Profile `when_to_use` is supplemental guidance for new or not-yet-benchmarked route cases, not a competing primary policy. |
| thinking-injection | The hub injects model and thinking per engine and strips them from saved args: omp `--model=/--thinking=`, kimi `-m` + env `KIMI_MODEL_THINKING_EFFORT` (child only), agy model-name suffix, codex `-m` + `-c model_reasoning_effort`, dsh via the DSH spec. Saved args may not contain these flags. |
| disabled-default | A disabled default model is replaced by the first enabled model, and the launcher, preview and toast say so; an engine with no enabled model refuses to launch. |
| codex-synthesis | Codex profiles are no longer synthesised from ~/.codex config or recent configurations. |
| quick-keys | Terminal quick keys go over the PTY WebSocket, never the parent-message mailbox. |
| waiting-parent | The console shows a sub-agent's question to the main agent in full but does not answer it. |
| launch-wording | UI says 启动 (not 拉起). |
| launcher-after-launch | The launcher collapses after a successful launch without changing its saved expanded/collapsed preference. |

## Archive and resume (2026-11-07, decision page archive-resume-2026-11-07)

| id | decision | default kept |
|---|---|---|
| resume-same-id | A resumed session keeps its hub session id; the record counts resumes and the terminal shows `── 已恢复（第 N 次）──`. | yes |
| auto-revive | `send_to_subagent` to an ended, resumable session resumes it and then delivers the message; a non-resumable one returns the reason in Chinese. | yes |
| retention | Archive records are kept forever; only a manual delete (console 删除记录 / `DELETE /api/archive/{id}`) removes one. Scrollback stays capped at 512 KB per session. | no (was: 30 days or newest 500) |
| resume-clears-reply | Resume and a follow-up message to a completed session keep the previous final reply until the new turn writes one; meanwhile `get_subagent_final_response` adds `from_previous_turn: true`. | no (was: clear it) |
| shutdown-ends-cli | A graceful hub shutdown ends each sub-agent's CLI process tree and cancels running dsh jobs, archiving them as `interrupted` (resumable). | yes |
| dsh-one-event | A dsh turn emits one `turn_completed` event (no separate `completed`), like the other engines. | yes |
| unconfirmed-injection | When the hub cannot confirm a typed task was submitted, it presses Enter once more (never while a known trust/update dialog is on screen); if still unknown, it records the injection as unconfirmed. | no (was: press nothing more) |

## Approval by default (2026-11-07, decided by the orchestrator; owner revised on decision page approval-by-default-2026-11-07)

| id | decision |
|---|---|
| D1 enforced | Enforced by default; per-profile `bypass_approvals` off switch (owner, decision page approval-by-default-2026-11-07). Every engine starts and resumes with high-risk approval on unless its profile has `bypass_approvals: true` (bool, missing = false; non-bool rejected on save; console checkbox 「跳过高风险审批（全自动，不推荐）」). With the switch on the hub injects the engine's full-bypass flag instead: omp `--auto-approve`, kimi `--auto`, codex `--dangerously-bypass-approvals-and-sandbox`, agy `--dangerously-skip-permissions` (no agentMode settings); dsh and pwsh ignore it (noted). A resume follows the profile's current value. Spawn, resume and status report `high_risk_approval` (true / false = bypassed / null for dsh, pwsh); the console marks bypassed sessions 「全自动」. Either way the hub strips each engine's bypass/mode flags from the profile args at launch (`apply_approval_flags`, next to the model/thinking injection) and adds a note to the launch result; `agent_profiles.json` is not rewritten. Stripped: omp `--auto-approve`, `--approval-mode`, `--config` at the hub overlay; kimi `--auto`, `--yolo`, `-y`; codex `--dangerously-bypass-approvals-and-sandbox`, `--full-auto`, `--approve-for-me`, `-a`/`--ask-for-approval`, `-s`/`--sandbox`, `-c approval_policy=…`/`-c sandbox_mode=…`; agy `--dangerously-skip-permissions`, `--mode`. |
| D2 omp | `--approval-mode=write --config=<repo>\engine_policies\omp-approval.yml`. The overlay allows ordinary shell commands and asks for the high-risk list (recursive deletes, force push / hard reset / clean -f / branch -D, publish, format / diskpart / shutdown / Restart-/Stop-Computer). Plain network fetches are not on it. |
| D3 kimi | `-y` (Ask When Needed). `KIMI_CODE_DANGEROUS_COMMAND_GUARD` is never set; wider kimi coverage needs the owner's global config. |
| D4 codex | `-a on-request -s workspace-write`, also on `codex resume`. Network through the proxy port stays reachable (accepted). |
| D5 agy | `--settings '{"agentMode":"auto"}'` per launch (merged with any `--settings` in the profile); `~/.gemini/antigravity-cli/settings.json` is never written. A live launch showed it holds from the first turn, unlike `--mode auto`, so the task still goes in with `-i` and there is no Shift+Tab step. |
| D6 dsh | Jobs need no flag (DSH already runs workspace-write + ask). The hub reads DSH's base URL from `hub_url` in dsh-crew's `config.json` (`HUB_DSH_CREW_CONFIG` overrides the path), falling back to `http://127.0.0.1:3080` (`dsh_endpoint.py`). |
| D7 answering | No decision means an error, never an approval; free text means deny with that feedback. omp: approve once Ctrl+Q, session row by arrows + Enter (else approve once, noted), deny one Escape, keys wait 0.6 s after the prompt. agy: options read from the screen and mapped by label; the persist option (`3`) is never sent; deny feedback goes in as the next message. codex: `y` / `n`; session approval sends the "don't ask again" key read from the screen: `p` on command prompts (owner, decision page approval-by-default-2026-11-07: codex writes a permanent allow rule for the command prefix into `~/.codex/rules/default.rules`; the result notes say so), `a` on edit prompts; a prompt without that option approves once, noted. A command that wraps on screen is joined up to the blank line / first option (codex and kimi readers; agy and omp already joined). kimi: one prompt is one pending interaction (the wire record adopts the screen one). |
| D8 no approval_mode | The `approval_mode` parameter, field and output are gone; old records that carry it still load. |
| D9 resume | Resume applies the same strip + inject to the stored args, so sessions created before this change resume with approval on (or full-auto, if the profile's `bypass_approvals` is on now). |
| D11 session override (2026-11-10, owner) | Codex, OMP, Kimi, Claude and AGY accept `bypass_approvals: bool \| null` on MCP spawn, HTTP create and launch preview. Null/missing inherits the profile; false enforces approval; true bypasses it. The profile is never rewritten. `bypass_approvals_override` stores the choice separately from effective `bypass_approvals`; `approval_source` is profile/session/not_applicable. Explicit choices survive resume; legacy/inherited records retain D9's current-profile behavior. The launcher resets to inherit on profile change and after successful launch; bypass is never saved as a UI preference. Inapplicable engines ignore with a note. This extends D1/D9; it does not restore D8's removed approval_mode. |
| D12 Codex MCP approvals (2026-11-10, owner) | Recognize the native `Allow the … MCP server to run tool …?` selector as an approval_request, with server/tool/arguments and native options. Allow/Cancel select the corresponding row and submit Enter; no session option means approve_session downgrades to once. Command/edit shortcuts retain D7. Responders refresh interactions and reject missing/stale/mismatched IDs without keys. `delivered` means input sent; `response_state` is sent/confirmed/unconfirmed. MCP acknowledgement waits at most 2 seconds for modal disappearance or matching native output. Timeout retains waiting_parent and blocks duplicate delivery; polling reconciles later acknowledgement. |
| D13 native auto-approval by default (2026-11-12, owner) | Owner: an engine with its own auto-approval mode runs it by default unless the main agent deliberately asks for another mode, and the guidance says so. `approval_mode` (`auto` / `ask` / `bypass`, null = default) on MCP spawn, `spawn_subagents` specs, HTTP create and launch preview; it supersedes D8's removal (old records' top-level `approval_mode` is still ignored; the override lives in the snapshot as `approval_mode_override`). auto = codex `--approve-for-me` (sandbox stays workspace-write), claude `--permission-mode auto` with an overlay holding only the PermissionRequest relay hook (`engine_policies/claude-auto-approval.json`; no ask rules or PreToolUse hook, so the classifier decides). Engines without a native reviewer (kimi, omp, agy) run ask, with a note only when auto was requested explicitly; agy's ask already uses its `agentMode: auto` (D5). ask = the D2–D5 flags; bypass = the D1 full-bypass flags. Resolution: session `approval_mode`, else session `bypass_approvals` (true = bypass, false = default — not ask, so a reflexive false keeps auto), else the profile's `bypass_approvals: true` = bypass, else auto. Passing both with different meanings is rejected. Explicit session choices survive resume; inherited and legacy records resume in the current default (D9). Outputs add `approval_mode` and `approval_mode_override`; `high_risk_approval` stays true for auto and ask, false for bypass. Records from before D13 report the mode they ran (ask or bypass). Console launcher: 继承 Profile（默认自动审批）/ 自动审批 / 高风险操作需审批 / 绕过审批. The profile's bypass checkbox is unchanged; a profile cannot pin ask. |
| kept defaults | Owner kept: kimi's dangerous-command rules unchanged (no extra kimi coverage); omp does not ask for plain network fetches; codex's sandbox reaching the network through the local proxy is accepted. |
| Q2 dsh approvals | Owner: no dsh-security-mode; dsh-crew is to forward DSH's native approvals to the hub instead — separate work in the dsh-crew repo, branch `feat/forward-native-approvals`. |
| D10 native observation | Superseded by the owner on 2026-11-10: no worker bridge tools, CLI, HTTP endpoints or call instructions. Observe native transcripts/events; native hooks and TUI detection cover missing interactions. AGY no longer receives a Hub MCP config, bridge workspace or tool allow rule. |

## Kimi startup and message injection (2026-11-06, decision page kimi-injection-2026-11-06)

| id | decision | default kept |
|---|---|---|
| live-checked | A live message with Enter to a running TUI session (send_to_subagent, console send) goes through the checked submit (type, wait until the text has settled on the composer, Enter, proof, retry), in the background, one writer per session; `last_injection` shows `submitting` until the outcome. | yes |
| engines-checked | kimi and codex use the checked submit for live messages (`TUI_LIVE_SUBMIT_CHECKED`); omp and agy keep write + Enter after 0.25 s, recorded `ok:null` `live_write_unchecked`. Revised 2026-11-07 (owner): codex added after the approval live check saw it lose the Enter of a live message 2 of 5 times. | no (was: kimi, omp, codex and agy; then kimi only) |
| resume-gate | A resume (`kimi -S`) is not gated on `Welcome to Kimi Code!`; it keeps the composer + not busy + stable gate. Fresh kimi spawns wait for Welcome. | yes |
| flatten-newlines | Text typed through the checked submit has its newlines replaced by spaces, the same as the startup task. | yes |
| failed-message-turn | A live message whose submit failed for certain (`ok:false`) puts a completed session back to completed with its previous answer, if nothing else changed it meanwhile. | yes |
| active-proof | The engine's working indicator proves a submit only if it was not on screen before the Enter. | yes |

## Native sub-agent observation (2026-11-10, owner)

Codex delivery revision (2026-11-10, supplemental report): Codex uses bracketed paste with preserved newlines, waits for the complete visible payload or counted literal-prefix-plus-paste-placeholder to settle, then sends at most one Enter. A matching full native user message appended after the write is the only submission proof; screen activity, cleared input and unrelated transcript growth are not proof. An unsettled paste sends no Enter. Failed/unconfirmed drafts block subsequent sends until late native proof or explicit `send_to_subagent(text="", clear_input=true)`; clearing is serialized, requires a visible nonempty idle Codex composer, and confirms removal after one Ctrl+C. Empty sends are no-ops with delivered=false. Replies correlate submit_id, with delivered=true/false/null for confirmed/failed/unconfirmed submissions. Bounded ANSI and terminal dimensions on failures stay under the private data diagnostics directory. Hub-generated failure ANSI is not injected into the native Codex terminal screen. This supersedes flatten-newlines, active-proof and Enter retries for Codex only. Exact cause of the reported old screen drift remains unproven.

Transcript-first principle (2026-11-10, owner): use native transcript records wherever they describe the state. All five supported CLI engines now confirm checked submissions only from a matching full native user message; confirmation never reads screen contents or treats unrelated transcript growth as success. Missing evidence returns unconfirmed without another Enter. Screen inspection remains for draft/composer readiness and native prompts that are not exposed through transcript records. Existing unchecked routes remain explicitly unconfirmed.

DSH workspace permissions (2026-11-10, owner): before local Windows DSH task dispatch or continuation, check the listener process's user SID against the Hub user. On a user-owned workspace with effective WRITE_DAC and missing WRITE_OWNER, automatically add only that host user's non-inheritable WRITE_OWNER ACE on the workspace root. Do not grant FullControl, change ownership/labels, modify ancestors/children, or bypass the sandbox. Pin the directory path with handles, reject reparse/system/root/home paths, serialize by workspace, save a durable private recovery record before mutation, and verify effective rights. On failure remove only the newly added exact ACE from the current DACL, retaining concurrent edits; report rollback failures. Preview and empty re-attachment only check. Remote/unverified hosts skip with a reason; known local preparation failures prevent dispatch. `sandbox_preparation` reports permission preparation, never claims native sandbox startup. No global DSH configuration changes or live-Hub replacement.

Native DSH acceptance limitation: the isolated Modify-only fixture reproduces Win32 5 before repair; root-only WRITE_OWNER lets DSH initialize, read an existing file and create a new file, while outside writes remain denied. The same fixture's pre-existing file does not acquire DSH's capability ACE and remains unwritable by the confined child. Root permission readiness therefore does not guarantee access to existing descendants; repairing those is outside this root-only authorization.

| id | decision |
|---|---|
| native-only | Remove worker-facing MCP/CLI and HTTP reporting/polling interfaces, including discovery fields. Parent-facing querying, replies, approvals and event polling remain. This is a breaking removal, without forwarding shims. |
| passive-marker | Initial/resumed tasks carry only `[Hub Session: session_id=…]` for transcript ownership, with no reporting instructions. Strip identifiable old Hub launch additions on spawn/resume; preserve user configuration and historical transcripts/results. |
| observation | Prefer structured native transcript records; retain native permission hooks and TUI detection where transcripts lack records. DSH continues through its native host messages/results. Do not infer pending questions from prose punctuation. |
| native-completion | Intermediate assistant text is telemetry. A native end-of-turn signal captures the final answer and emits one completion per turn, including empty answers. Keep the previous final answer until the new turn produces an answer; summaries/screens remain low-priority fallbacks for inactive sessions. Drain native records for up to three seconds after process exit. |
