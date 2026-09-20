import {
  getPrivateIdentitySnapshot,
  isPrivateIdentitySnapshotCurrent,
} from "../auth/privateIdentityEpoch";
import { apiFetch } from "./http";
import { getWorkflow, type WorkflowRun } from "./workflows";

/** Refresh the materialized workflow, without submitting or charging for work. */
export async function refreshWorkflow(
  id: string,
  signal?: AbortSignal,
): Promise<WorkflowRun> {
  const identity = getPrivateIdentitySnapshot();
  const assertCurrent = () => {
    signal?.throwIfAborted();
    if (!isPrivateIdentitySnapshotCurrent(identity)) {
      throw new DOMException("Workflow refresh belongs to a previous session", "AbortError");
    }
  };
  assertCurrent();
  const snapshot = await getWorkflow(id, signal);
  assertCurrent();

  // GET deliberately has no write side effects. These two workflow kinds
  // need their existing, CSRF-protected projection sync after task completion.
  const supported = snapshot.type === "apparel_model_showcase"
    || snapshot.type === "poster_design";
  const unsettled = snapshot.status === "running"
    || snapshot.status === "needs_review"
    || snapshot.steps.some((step) => step.status === "running");
  if (!supported || !unsettled) return snapshot;

  const reconciled = await apiFetch<WorkflowRun>(
    `/workflows/${encodeURIComponent(id)}/reconcile`,
    { method: "POST", signal },
  );
  assertCurrent();
  return reconciled;
}
