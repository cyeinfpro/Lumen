import type {
  AssistantMessage,
  Message,
} from "../../lib/types";

export interface PendingCompletionStreamPatch {
  msgId?: string;
  compId?: string;
  text: string;
  thinking: string;
  firstQueuedAt: number;
  updatedAt: number;
}

export function completionStreamPatchKey(
  messageId: string | undefined,
  completionId: string | undefined,
): string | null {
  if (completionId) return `comp:${completionId}`;
  if (messageId) return `msg:${messageId}`;
  return null;
}

export function createCompletionStreamPatch(
  messageId: string | undefined,
  completionId: string | undefined,
  now: number,
): PendingCompletionStreamPatch {
  return {
    msgId: messageId,
    compId: completionId,
    text: "",
    thinking: "",
    firstQueuedAt: now,
    updatedAt: now,
  };
}

export function mergeCompletionStreamPatch(
  target: PendingCompletionStreamPatch,
  source: PendingCompletionStreamPatch,
): void {
  target.msgId = target.msgId ?? source.msgId;
  target.compId = target.compId ?? source.compId;
  target.text += source.text;
  target.thinking += source.thinking;
  target.updatedAt = Math.max(target.updatedAt, source.updatedAt);
}

function isTerminalMessage(message: AssistantMessage): boolean {
  return (
    message.status === "succeeded" ||
    message.status === "failed" ||
    message.status === "canceled"
  );
}

function collectMessagePatches(
  message: AssistantMessage,
  patchEntries: Array<[string, PendingCompletionStreamPatch]>,
  pendingByCompletionId: ReadonlyMap<string, PendingCompletionStreamPatch>,
  appliedPatchKeys: Set<string>,
  appliedPendingCompletionIds: Set<string>,
): PendingCompletionStreamPatch[] {
  const patches: PendingCompletionStreamPatch[] = [];
  for (const [key, patch] of patchEntries) {
    const matches =
      (patch.msgId != null && message.id === patch.msgId) ||
      (patch.compId != null && message.completion_id === patch.compId);
    if (!matches) continue;
    appliedPatchKeys.add(key);
    patches.push(patch);
  }

  if (!message.completion_id) return patches;
  const pending = pendingByCompletionId.get(message.completion_id);
  if (!pending) return patches;
  appliedPendingCompletionIds.add(message.completion_id);
  patches.push(pending);
  return patches;
}

function appendPatchValue(
  current: string | undefined,
  incoming: string,
  terminal: boolean,
): string | undefined {
  if (!incoming) return current;
  const value = current ?? "";
  return terminal && value.endsWith(incoming) ? current : value + incoming;
}

function applyPatchesToMessage(
  message: AssistantMessage,
  patches: PendingCompletionStreamPatch[],
  now: number,
): AssistantMessage {
  if (patches.length === 0) return message;
  const next = { ...message };
  for (const patch of patches) {
    const terminal = isTerminalMessage(next);
    next.text = appendPatchValue(next.text, patch.text, terminal);
    next.thinking = appendPatchValue(
      next.thinking,
      patch.thinking,
      terminal,
    );
  }
  if (!isTerminalMessage(next)) {
    next.status = "streaming";
    next.stream_started_at ??= now;
  }
  next.last_delta_at = now;
  return next;
}

export function applyCompletionStreamPatches(
  messages: Message[],
  patchEntries: Array<[string, PendingCompletionStreamPatch]>,
  pendingByCompletionId: ReadonlyMap<string, PendingCompletionStreamPatch>,
  now: number,
): {
  messages: Message[];
  changed: boolean;
  appliedPatchKeys: Set<string>;
  appliedPendingCompletionIds: Set<string>;
} {
  let changed = false;
  const appliedPatchKeys = new Set<string>();
  const appliedPendingCompletionIds = new Set<string>();
  const nextMessages = messages.map((message) => {
    if (message.role !== "assistant") return message;
    const patches = collectMessagePatches(
      message,
      patchEntries,
      pendingByCompletionId,
      appliedPatchKeys,
      appliedPendingCompletionIds,
    );
    const next = applyPatchesToMessage(message, patches, now);
    changed ||= next !== message;
    return next;
  });
  return {
    messages: changed ? nextMessages : messages,
    changed,
    appliedPatchKeys,
    appliedPendingCompletionIds,
  };
}
