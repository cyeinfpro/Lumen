import type { UpdateStepRecord } from "@/lib/apiClient";

export type UpdateOutcome = "idle" | "running" | "failed" | "complete" | "incomplete";

export function updateOutcome(
  running: boolean,
  phases: readonly UpdateStepRecord[],
  launchFailed = false,
): UpdateOutcome {
  if (running) return "running";
  if (launchFailed || phases.some((phase) =>
    phase.status === "done" && phase.rc != null && phase.rc !== 0,
  )) return "failed";
  // A warm pull, info-only check, or completed cleanup does not prove that
  // final readiness and the durable update commit succeeded.
  if (phases.some((phase) =>
    (phase.phase === "complete" || phase.phase === "rollback")
    && phase.status === "done" && phase.rc === 0,
  )) return "complete";
  return phases.length > 0 ? "incomplete" : "idle";
}
