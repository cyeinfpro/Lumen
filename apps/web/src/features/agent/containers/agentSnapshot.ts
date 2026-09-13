import { getAgentActiveRun, listAgentMessages } from "../api/agentApi";
import { confirmObservedAgentRuns } from "../api/logicalAgentRequests";
import { getPrivateIdentitySnapshot } from "@/lib/auth/privateIdentityEpoch";
import { useAgentStore } from "@/store/agent/useAgentStore";

/** Shared recovery path for polling, focus and SSE snapshot reconciliation. */
export async function refreshAgentSnapshot(signal?: AbortSignal): Promise<void> {
  const sessionId = useAgentStore.getState().currentSessionId;
  if (!sessionId) return;
  const identity = getPrivateIdentitySnapshot();
  const [snapshot, run] = await Promise.all([
    listAgentMessages(sessionId, { limit: 100, includeTasks: true, signal }),
    getAgentActiveRun(sessionId, signal),
  ]);
  const currentIdentity = getPrivateIdentitySnapshot();
  const state = useAgentStore.getState();
  if (
    state.currentSessionId !== sessionId ||
    currentIdentity.userId !== identity.userId ||
    currentIdentity.epoch !== identity.epoch
  ) return;
  state.applySnapshot(sessionId, snapshot);
  if (run) state.applyRunSnapshot(run);
  await confirmObservedAgentRuns(identity, sessionId, snapshot.runs);
}
