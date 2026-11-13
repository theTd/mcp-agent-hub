// Loaded into every omp the hub starts with approvals on (--extension, see approval_launch_flags in
// session_manager.py). It appends omp's own tool approval events to the JSONL file named by
// HUB_OMP_APPROVAL_LOG, so the hub learns that an approval box is open, and for which tool call,
// from omp itself rather than from the box's text on the screen.
//
// omp emits tool_approval_requested just before it draws the box and tool_approval_resolved once
// the box is answered. Both are observe-only: omp ignores what a handler returns, and the answer
// still comes from the box. This file registers no tool_call handler: one that throws blocks the
// tool call. Nothing here may throw into omp.
import { appendFileSync } from "node:fs";

type ApprovalEvent = {
  sessionId?: string;
  toolName?: string;
  toolCallId?: string;
  reason?: string;
  approvalMode?: string;
  approved?: boolean;
};

export default function hubApprovalEventLog(api: { on(event: string, handler: (event: ApprovalEvent) => void): void }) {
  const logPath = process.env.HUB_OMP_APPROVAL_LOG;
  if (!logPath) return;
  const write = (record: Record<string, unknown>) => {
    try {
      appendFileSync(logPath, JSON.stringify({ ts: Date.now() / 1000, pid: process.pid, ...record }) + "\n");
    } catch {
      // The hub falls back to reading the box from the screen.
    }
  };
  try {
    api.on("tool_approval_requested", (event) => {
      write({
        event: "tool_approval_requested",
        call_id: event?.toolCallId,
        tool_name: event?.toolName,
        reason: event?.reason,
        approval_mode: event?.approvalMode,
        omp_session_id: event?.sessionId,
      });
    });
    api.on("tool_approval_resolved", (event) => {
      write({
        event: "tool_approval_resolved",
        call_id: event?.toolCallId,
        tool_name: event?.toolName,
        approved: event?.approved,
        reason: event?.reason,
        omp_session_id: event?.sessionId,
      });
    });
    write({ event: "extension_loaded" });
  } catch {
    // An omp without these events: the hub keeps reading boxes from the screen.
  }
}
