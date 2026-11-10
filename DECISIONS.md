# Decisions

Design decisions confirmed by the owner. Change a line here before changing the behaviour it records.

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
| profile-identity | A Profile is a launch recipe (engine, command, args, task mode, default model, default thinking level, when_to_use). Model and thinking level are chosen at launch; variants per model/thinking are not Profiles. Old variant ids were dropped without aliases. |
| agent-guidance | Guidance for parent agents lives in three places: Profile `when_to_use`, per-model notes (`engine_policies.<engine>.model_notes`), and the thinking rule of thumb in `global_routing_instructions`. |
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

| id | decision |
|---|---|
| native-only | Remove worker-facing MCP/CLI and HTTP reporting/polling interfaces, including discovery fields. Parent-facing querying, replies, approvals and event polling remain. This is a breaking removal, without forwarding shims. |
| passive-marker | Initial/resumed tasks carry only `[Hub Session: session_id=…]` for transcript ownership, with no reporting instructions. Strip identifiable old Hub launch additions on spawn/resume; preserve user configuration and historical transcripts/results. |
| observation | Prefer structured native transcript records; retain native permission hooks and TUI detection where transcripts lack records. DSH continues through its native host messages/results. Do not infer pending questions from prose punctuation. |
| native-completion | Intermediate assistant text is telemetry. A native end-of-turn signal captures the final answer and emits one completion per turn, including empty answers. Keep the previous final answer until the new turn produces an answer; summaries/screens remain low-priority fallbacks for inactive sessions. Drain native records for up to three seconds after process exit. |
