import { ApiError } from "@/lib/api/errors";
import {
  semanticPostIdempotency,
  isAmbiguousRequestFailure,
  withSemanticPostIdempotency,
} from "@/lib/api/semanticIdempotency";
import {
  getPrivateIdentitySnapshot,
  isPrivateIdentitySnapshotCurrent,
  type PrivateIdentitySnapshot,
} from "@/lib/auth/privateIdentityEpoch";
import type { AgentMessageCreateInput, AgentRun } from "../model/contracts";
import { continueAgentRun, postAgentMessage } from "./agentApi";

export function assertAgentRequestIdentity(identity: PrivateIdentitySnapshot): void {
  if (!isPrivateIdentitySnapshotCurrent(identity)) {
    throw new ApiError({ code: "identity_changed", status: 0, message: "Agent request identity changed" });
  }
}

function requestIdentity(userId: string): PrivateIdentitySnapshot {
  const identity = getPrivateIdentitySnapshot();
  assertAgentRequestIdentity(identity);
  if (identity.userId !== userId) {
    throw new ApiError({ code: "identity_changed", status: 0, message: "Agent request owner changed" });
  }
  return identity;
}

function retryableTransportError(error: unknown): boolean {
  return error instanceof ApiError && (
    error.status === 0 || error.code === "network_error" || error.code === "request_timeout"
  );
}

export function submitLogicalAgentMessage(input: {
  userId: string;
  sessionId: string;
  payload: Omit<AgentMessageCreateInput, "idempotency_key">;
  retryKey?: string;
  onAttempt: (key: string) => void;
}) {
  const identity = requestIdentity(input.userId);
  const request = async (key: string) => {
      assertAgentRequestIdentity(identity);
      input.onAttempt(key);
      const body = { ...input.payload, idempotency_key: key };
      const post = () => {
        assertAgentRequestIdentity(identity);
        return postAgentMessage(input.sessionId, body);
      };
      const result = await post().catch((error: unknown) => {
        if (!retryableTransportError(error)) throw error;
        return post();
      });
      assertAgentRequestIdentity(identity);
      return result;
  };
  // A different tab may retire the shared lease while this editor is unresolved.
  if (input.retryKey) {
    const key = input.retryKey;
    return request(key).then(async (result) => {
      await semanticPostIdempotency.confirmPendingKey(input.userId, key);
      assertAgentRequestIdentity(identity);
      return result;
    }, async (error: unknown) => {
      if (isPrivateIdentitySnapshotCurrent(identity) && !isAmbiguousRequestFailure(error)) {
        await semanticPostIdempotency.confirmPendingKey(input.userId, key);
      }
      throw error;
    });
  }
  return withSemanticPostIdempotency(
    { operation: "agent.message.create", userId: input.userId, sessionId: input.sessionId },
    input.payload,
    request,
  );
}

export function continueLogicalAgentRun(input: {
  userId: string;
  sessionId: string;
  runId: string;
}) {
  const identity = requestIdentity(input.userId);
  return withSemanticPostIdempotency(
    { operation: "agent.run.continue", userId: input.userId, sessionId: input.sessionId, sourceRunId: input.runId },
    { sourceRunId: input.runId },
    async (key) => {
      assertAgentRequestIdentity(identity);
      const run = await continueAgentRun(input.runId, key);
      assertAgentRequestIdentity(identity);
      return run;
    },
  );
}

export async function confirmObservedAgentRuns(
  identity: PrivateIdentitySnapshot,
  sessionId: string,
  runs: AgentRun[],
): Promise<void> {
  for (const run of runs) {
    assertAgentRequestIdentity(identity);
    if (run.agent_session_id !== sessionId || run.id.startsWith("optimistic:")) continue;
    await semanticPostIdempotency.confirmPendingKey(identity.userId!, run.idempotency_key);
  }
}
