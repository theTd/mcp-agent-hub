# Decisions

Design decisions confirmed by the owner. Change a line here before changing the behaviour it records.

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

## Kimi startup and message injection (2026-11-06, decision page kimi-injection-2026-11-06)

| id | decision | default kept |
|---|---|---|
| live-checked | A live message with Enter to a running TUI session (send_to_subagent, console send) goes through the checked submit (type, wait until the text has settled on the composer, Enter, proof, retry), in the background, one writer per session; `last_injection` shows `submitting` until the outcome. | yes |
| engines-checked | Only kimi uses the checked submit for live messages (`TUI_LIVE_SUBMIT_CHECKED`); omp, codex and agy keep write + Enter after 0.25 s, recorded `ok:null` `live_write_unchecked`. | no (was: kimi, omp, codex and agy) |
| resume-gate | A resume (`kimi -S`) is not gated on `Welcome to Kimi Code!`; it keeps the composer + not busy + stable gate. Fresh kimi spawns wait for Welcome. | yes |
| flatten-newlines | Text typed through the checked submit has its newlines replaced by spaces, the same as the startup task. | yes |
| failed-message-turn | A live message whose submit failed for certain (`ok:false`) puts a completed session back to completed with its previous answer, if nothing else changed it meanwhile. | yes |
| active-proof | The engine's working indicator proves a submit only if it was not on screen before the Enter. | yes |
