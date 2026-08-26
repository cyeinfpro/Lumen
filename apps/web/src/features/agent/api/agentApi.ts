import {
  apiFetch,
} from "@/lib/api/http";
import { idempotentPostRequest } from "@/lib/api/semanticIdempotency";
import type {
  AgentMessageCreateInput,
  AgentMessageCreateResult,
  AgentMessageList,
  AgentRun,
  AgentSession,
  AgentSessionImageList,
  AgentSessionCreateInput,
  AgentSessionList,
  AgentSessionPatchInput,
  AgentStatus,
} from "../model/contracts";
import {
  validateAgentMessageCreate,
  validateAgentMessages,
  validateAgentRun,
  validateAgentSession,
  validateAgentSessionImages,
  validateAgentSessionList,
  validateAgentStatus,
  validateNullableAgentRun,
} from "./validators";

export interface ListAgentSessionsOptions {
  cursor?: string;
  q?: string;
  limit?: number;
  signal?: AbortSignal;
}

export function listAgentSessions(
  options: ListAgentSessionsOptions = {},
): Promise<AgentSessionList> {
  const query = new URLSearchParams();
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.q?.trim()) query.set("q", options.q.trim());
  if (options.limit) query.set("limit", String(options.limit));
  const suffix = query.size ? `?${query.toString()}` : "";
  return apiFetch<AgentSessionList>(`/agent/sessions${suffix}`, {
    signal: options.signal,
    validate: validateAgentSessionList,
  });
}

export function getAgentStatus(signal?: AbortSignal): Promise<AgentStatus> {
  return apiFetch<AgentStatus>("/agent/status", {
    signal,
    validate: validateAgentStatus,
  });
}

export function createAgentSession(
  body: AgentSessionCreateInput = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return apiFetch<AgentSession>("/agent/sessions", {
    method: "POST",
    signal,
    body: JSON.stringify(body),
    validate: validateAgentSession,
  });
}

export function getAgentSession(
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentSession> {
  return apiFetch<AgentSession>(
    `/agent/sessions/${encodeURIComponent(sessionId)}`,
    { signal, validate: validateAgentSession },
  );
}

export function patchAgentSession(
  sessionId: string,
  body: AgentSessionPatchInput,
): Promise<AgentSession> {
  return apiFetch<AgentSession>(
    `/agent/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
      validate: validateAgentSession,
    },
  );
}

export async function deleteAgentSession(sessionId: string): Promise<void> {
  await apiFetch<{ ok: boolean }>(
    `/agent/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}

export function listAgentSessionImages(
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentSessionImageList> {
  return apiFetch<AgentSessionImageList>(
    `/agent/sessions/${encodeURIComponent(sessionId)}/images`,
    { signal, validate: validateAgentSessionImages },
  );
}

export function ejectAgentSessionImage(
  sessionId: string,
  imageId: string,
): Promise<AgentSessionImageList> {
  return apiFetch<AgentSessionImageList>(
    `/agent/sessions/${encodeURIComponent(sessionId)}/images/${encodeURIComponent(imageId)}`,
    { method: "DELETE", validate: validateAgentSessionImages },
  );
}

export interface ListAgentMessagesOptions {
  cursor?: string;
  since?: string;
  limit?: number;
  includeTasks?: boolean;
  signal?: AbortSignal;
}

export function listAgentMessages(
  sessionId: string,
  options: ListAgentMessagesOptions = {},
): Promise<AgentMessageList> {
  const query = new URLSearchParams();
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.since) query.set("since", options.since);
  if (options.limit) query.set("limit", String(options.limit));
  if (options.includeTasks !== false) query.set("include", "tasks");
  const suffix = query.size ? `?${query.toString()}` : "";
  return apiFetch<AgentMessageList>(
    `/agent/sessions/${encodeURIComponent(sessionId)}/messages${suffix}`,
    { signal: options.signal, validate: validateAgentMessages },
  );
}

export function postAgentMessage(
  sessionId: string,
  body: AgentMessageCreateInput,
  signal?: AbortSignal,
): Promise<AgentMessageCreateResult> {
  return apiFetch<AgentMessageCreateResult>(
    `/agent/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      ...idempotentPostRequest(body, { signal }),
      validate: validateAgentMessageCreate,
    },
  );
}

export function getAgentActiveRun(
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentRun | null> {
  return apiFetch<AgentRun | null>(
    `/agent/sessions/${encodeURIComponent(sessionId)}/active-run`,
    { signal, validate: validateNullableAgentRun },
  );
}

export function getAgentRun(
  runId: string,
  signal?: AbortSignal,
): Promise<AgentRun> {
  return apiFetch<AgentRun>(`/agent/runs/${encodeURIComponent(runId)}`, {
    signal,
    validate: validateAgentRun,
  });
}

export function cancelAgentRun(runId: string): Promise<AgentRun> {
  return apiFetch<AgentRun>(
    `/agent/runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST", body: JSON.stringify({}), validate: validateAgentRun },
  );
}

export function continueAgentRun(
  runId: string,
  idempotencyKey: string,
): Promise<AgentRun> {
  return apiFetch<AgentRun>(
    `/agent/runs/${encodeURIComponent(runId)}/continue`,
    {
      ...idempotentPostRequest({ idempotency_key: idempotencyKey }),
      validate: validateAgentRun,
    },
  );
}
