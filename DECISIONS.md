# Decisions

Design decisions confirmed by the owner. Change a line here before changing the behaviour it records.

## Web console (2026-11-05, console UI redesign)

| id | decision | default kept |
|---|---|---|
| file-structure | Console front end is split into `static/css/hub-console.css` + ES modules under `static/js/`, served by the `/static` mount in `server.py` (no bundler). | yes |
| brand-name | Top bar shows "Sub-Agent Hub" with the Chinese subtitle "MCP 聚合网关 · 非无头终端总控台". | yes |
| profile-id-edit | A saved profile's ID is read-only; renaming is "复制为新 Profile" (duplicate) then delete the old one. | yes |
| terminate-confirm | Closing a session asks for confirmation only while it is live (starting / running / waiting_parent); finished sessions close in one click. | yes |
