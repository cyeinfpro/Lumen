import type {
  BackendGeneration,
  BackendImageMeta,
} from "@/lib/api/tasks";
import type {
  AspectRatio,
  GeneratedImage,
  Generation,
  GenerationStage,
  GenerationStatus,
} from "@/lib/types";
import type {
  AgentAssistantMessage,
  AgentBackendMessage,
  AgentGenerationProjection,
  AgentMessage,
  AgentMessageContent,
  AgentMessageList,
  AgentRun,
  AgentRunStatus,
  AgentUserMessage,
} from "./contracts";
import { isAgentRunTerminal } from "./contracts";

function runStatusFromMessage(value: string | null): AgentRunStatus | "pending" {
  if (value === "canceled" || value === "cancelled") return "cancelled";
  if (
    value === "queued" ||
    value === "running" ||
    value === "succeeded" ||
    value === "partial" ||
    value === "failed"
  ) {
    return value;
  }
  return "pending";
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is string => typeof item === "string" && item.length > 0,
  );
}

function userMessage(message: AgentBackendMessage): AgentUserMessage {
  return {
    id: message.id,
    role: "user",
    text: typeof message.content.text === "string" ? message.content.text : "",
    attachments: Array.isArray(message.content.attachments)
      ? message.content.attachments
      : [],
    createdAt: message.created_at,
  };
}

function nonnegativeContentInteger(value: unknown): number {
  return Number.isSafeInteger(value) ? Math.max(0, Number(value)) : 0;
}

function assistantOutput(
  content: AgentMessageContent,
  run: AgentRun | undefined,
): Pick<
  AgentAssistantMessage,
  "text" | "toolCalls" | "blocks" | "outputRevision" | "outputRuntimeSeq"
> {
  const outputRevision = nonnegativeContentInteger(content.output_revision);
  const outputRuntimeSeq = nonnegativeContentInteger(content.output_runtime_seq);
  const torn = outputRevision < (run?.output_revision ?? 0);
  return {
    text: !torn && typeof content.text === "string" ? content.text : "",
    toolCalls: !torn && Array.isArray(content.tool_calls) ? content.tool_calls : [],
    blocks: !torn && Array.isArray(content.blocks) ? content.blocks : [],
    outputRevision,
    outputRuntimeSeq,
  };
}

function assistantRunId(
  content: AgentMessageContent,
  run: AgentRun | undefined,
): string | null {
  if (run) return run.id;
  return typeof content.agent_run_id === "string" ? content.agent_run_id : null;
}

function assistantMessage(
  message: AgentBackendMessage,
  run: AgentRun | undefined,
): AgentAssistantMessage {
  const contentStatus = run?.status ?? runStatusFromMessage(message.status);
  return {
    id: message.id,
    role: "assistant",
    ...assistantOutput(message.content, run),
    status: contentStatus,
    agentRunId: assistantRunId(message.content, run),
    parentUserMessageId: message.parent_message_id,
    generationIds: stringList(message.content.generation_ids),
    createdAt: message.created_at,
    partial: contentStatus === "partial",
  };
}

export function adaptAgentMessages(
  items: AgentBackendMessage[],
  runs: AgentRun[],
): AgentMessage[] {
  const runsByAssistant = new Map(
    runs.map((run) => [run.assistant_message_id, run]),
  );
  return items
    .filter(
      (message) => message.role === "user" || message.role === "assistant",
    )
    .map((message) =>
      message.role === "user"
        ? userMessage(message)
        : assistantMessage(message, runsByAssistant.get(message.id)),
    )
    .sort((left, right) => {
      const byTime = Date.parse(left.createdAt) - Date.parse(right.createdAt);
      return byTime || left.id.localeCompare(right.id);
    });
}

export function mergeAgentMessage(
  existing: AgentMessage | undefined,
  incoming: AgentMessage,
): AgentMessage {
  if (!existing || existing.role !== incoming.role) return incoming;
  if (existing.role === "user" && incoming.role === "user") {
    return { ...existing, ...incoming, optimistic: false };
  }
  if (existing.role === "assistant" && incoming.role === "assistant") {
    const existingRevision = [existing.outputRevision, existing.outputRuntimeSeq];
    const incomingRevision = [incoming.outputRevision, incoming.outputRuntimeSeq];
    const incomingOlder =
      incomingRevision[0] < existingRevision[0] ||
      (incomingRevision[0] === existingRevision[0] &&
        incomingRevision[1] < existingRevision[1]);
    if (incomingOlder) return { ...existing, optimistic: false };
    const equalRevision =
      incomingRevision[0] === existingRevision[0] &&
      incomingRevision[1] === existingRevision[1];
    if (equalRevision && existing.text !== incoming.text && !existing.optimistic) {
      return { ...existing, optimistic: false };
    }
    return { ...incoming, optimistic: false };
  }
  return incoming;
}

export function mergeAgentMessageLists(
  existing: AgentMessage[],
  incoming: AgentMessage[],
): AgentMessage[] {
  const byId = new Map(existing.map((message) => [message.id, message]));
  for (const message of incoming) {
    byId.set(message.id, mergeAgentMessage(byId.get(message.id), message));
  }
  return [...byId.values()].sort((left, right) => {
    const byTime = Date.parse(left.createdAt) - Date.parse(right.createdAt);
    return byTime || left.id.localeCompare(right.id);
  });
}

export function mergeAgentRun(
  existing: AgentRun | undefined,
  incoming: AgentRun,
): AgentRun {
  if (!existing) return incoming;
  if (incoming.execution_epoch < existing.execution_epoch) return existing;
  if (
    incoming.execution_epoch === existing.execution_epoch &&
    incoming.last_event_seq < existing.last_event_seq
  ) {
    return existing;
  }
  if (
    isAgentRunTerminal(existing.status) &&
    incoming.status !== existing.status
  ) {
    return existing;
  }
  return {
    ...existing,
    ...incoming,
    references:
      incoming.references.length > 0 ? incoming.references : existing.references,
    tool_calls:
      incoming.tool_calls.length >= existing.tool_calls.length
        ? incoming.tool_calls
        : existing.tool_calls,
  };
}

function generatedImage(
  generation: BackendGeneration,
  image: BackendImageMeta,
): GeneratedImage {
  return {
    id: image.id,
    data_url: image.url,
    mime: image.mime,
    display_url: image.display_url ?? undefined,
    preview_url: image.preview_url ?? undefined,
    thumb_url: image.thumb_url ?? undefined,
    width: image.width,
    height: image.height,
    parent_image_id: image.parent_image_id,
    from_generation_id: generation.id,
    size_requested: generation.size_requested,
    size_actual: `${image.width}x${image.height}`,
    metadata_jsonb: image.metadata_jsonb,
    source: generation.source,
    action_source: generation.action_source,
    trace_id: generation.trace_id,
    attachment_roles: generation.attachment_roles,
  };
}

function generationWithImage(
  generation: BackendGeneration,
  image: BackendImageMeta | undefined,
): Generation {
  const projected = generationFromBackend(generation);
  if (!image) return projected;
  return {
    ...projected,
    status: generation.status === "succeeded" ? "succeeded" : projected.status,
    image: generatedImage(generation, image),
  };
}

const ASPECT_RATIOS = new Set<AspectRatio>([
  "1:1",
  "16:9",
  "9:16",
  "21:9",
  "9:21",
  "10:7",
  "7:10",
  "4:5",
  "3:4",
  "4:3",
  "3:2",
  "2:3",
]);

function generationStatus(value: BackendGeneration["status"]): GenerationStatus {
  return value === "running" ||
    value === "succeeded" ||
    value === "failed" ||
    value === "canceled"
    ? value
    : "queued";
}

function generationStage(value: string): GenerationStage {
  return value === "understanding" ||
    value === "rendering" ||
    value === "finalizing"
    ? value
    : "queued";
}

function generationFromBackend(generation: BackendGeneration): Generation {
  const aspectRatio = ASPECT_RATIOS.has(generation.aspect_ratio as AspectRatio)
    ? (generation.aspect_ratio as AspectRatio)
    : "1:1";
  return {
    id: generation.id,
    message_id: generation.message_id,
    parent_generation_id: generation.parent_generation_id ?? null,
    action: generation.action === "edit" ? "edit" : "generate",
    prompt: generation.prompt,
    size_requested: generation.size_requested,
    aspect_ratio: aspectRatio,
    input_image_ids: generation.input_image_ids,
    primary_input_image_id: generation.primary_input_image_id,
    status: generationStatus(generation.status),
    stage: generationStage(generation.progress_stage),
    error_code: generation.error_code ?? undefined,
    error_message: generation.error_message ?? undefined,
    source: generation.source,
    conversation_id: generation.conversation_id,
    agent_session_id: generation.agent_session_id,
    agent_run_id: generation.agent_run_id,
    agent_tool_call_id: generation.agent_tool_call_id,
    action_source: generation.action_source,
    trace_id: generation.trace_id,
    attachment_roles: generation.attachment_roles,
    attempt: generation.attempt,
    execution_epoch: generation.execution_epoch ?? 0,
    created_at: generation.created_at
      ? Date.parse(generation.created_at)
      : undefined,
    started_at: generation.started_at
      ? Date.parse(generation.started_at)
      : Date.now(),
    finished_at: generation.finished_at
      ? Date.parse(generation.finished_at)
      : undefined,
  };
}

export function projectAgentGenerations(
  generations: BackendGeneration[],
  images: BackendImageMeta[],
  sessionId: string,
): AgentGenerationProjection {
  const imageByGeneration = new Map<string, BackendImageMeta>();
  for (const image of images) {
    if (image.owner_generation_id) {
      imageByGeneration.set(image.owner_generation_id, image);
    }
  }
  const byId: Record<string, Generation> = {};
  const orderedIds: string[] = [];
  for (const generation of generations) {
    if (
      generation.agent_session_id &&
      generation.agent_session_id !== sessionId
    ) {
      continue;
    }
    if (
      generation.source &&
      generation.source !== "agent" &&
      !generation.agent_session_id
    ) {
      continue;
    }
    byId[generation.id] = generationWithImage(
      generation,
      imageByGeneration.get(generation.id),
    );
    orderedIds.push(generation.id);
  }
  return { byId, orderedIds };
}

export interface ReconciledAgentSnapshot {
  messages: AgentMessage[];
  runs: Record<string, AgentRun>;
  generations: AgentGenerationProjection;
}

export function reconcileAgentSnapshot(
  existingMessages: AgentMessage[],
  existingRuns: Record<string, AgentRun>,
  snapshot: AgentMessageList,
  sessionId: string,
): ReconciledAgentSnapshot {
  const incomingMessages = adaptAgentMessages(snapshot.items, snapshot.runs);
  const incomingById = new Map(incomingMessages.map((message) => [message.id, message]));
  const authoritativeAssistantIds = new Set<string>();
  const runs = { ...existingRuns };
  let retainedMessages = existingMessages;
  for (const run of snapshot.runs) {
    const existingRun = runs[run.id];
    const incomingAssistant = incomingById.get(run.assistant_message_id);
    const runIsCurrent =
      !existingRun ||
      run.execution_epoch > existingRun.execution_epoch ||
      (run.execution_epoch === existingRun.execution_epoch &&
        run.last_event_seq >= existingRun.last_event_seq);
    const outputMatches =
      incomingAssistant?.role === "assistant" &&
      incomingAssistant.outputRevision === (run.output_revision ?? 0) &&
      incomingAssistant.outputRuntimeSeq === (run.output_runtime_seq ?? 0);
    if (runIsCurrent && outputMatches) {
      authoritativeAssistantIds.add(run.assistant_message_id);
    }
    const optimistic = Object.values(runs).find(
      (candidate) =>
        candidate.id.startsWith("optimistic:") &&
        candidate.idempotency_key === run.idempotency_key,
    );
    if (optimistic) {
      delete runs[optimistic.id];
      retainedMessages = retainedMessages.filter(
        (message) =>
          message.id !== optimistic.user_message_id &&
          message.id !== optimistic.assistant_message_id,
      );
    }
    runs[run.id] = mergeAgentRun(runs[run.id], run);
  }
  retainedMessages = retainedMessages.filter(
    (message) => !authoritativeAssistantIds.has(message.id),
  );
  return {
    messages: mergeAgentMessageLists(retainedMessages, incomingMessages),
    runs,
    generations: projectAgentGenerations(
      snapshot.generations,
      snapshot.images,
      sessionId,
    ),
  };
}
