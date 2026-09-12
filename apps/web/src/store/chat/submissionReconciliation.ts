import type { AssistantMessage, Generation, Message } from "../../lib/types";

function submissionGenerationIds(message: AssistantMessage): string[] {
  return message.generation_ids ?? (message.generation_id ? [message.generation_id] : []);
}

function normalizeAcknowledgement(message: Message): Message {
  if (message.role !== "assistant") return message;
  const ids = [...new Set(submissionGenerationIds(message))];
  return ids.length > 0
    ? { ...message, generation_ids: ids, generation_id: ids[0] }
    : message;
}

function preserveCurrentMessage(current: Message, ack: Message | undefined): Message {
  if (ack?.role !== "assistant" || current.role !== "assistant") return current;
  const ids = [...new Set([
    ...submissionGenerationIds(ack),
    ...submissionGenerationIds(current),
  ])];
  return {
    ...ack,
    ...current,
    ...(ids.length > 0 ? { generation_ids: ids, generation_id: ids[0] } : {}),
  };
}

// These are submission acknowledgements, not authoritative task updates. A
// realtime/history snapshot already present always outranks a late HTTP ack.
export function mergeSubmissionMessages(
  existing: Message[],
  incoming: Message[],
  aliases: Record<string, string> = {},
): Message[] {
  const snapshots = new Map(incoming.map((message) =>
    [message.id, normalizeAcknowledgement(message)] as const,
  ));
  for (const current of existing) {
    if (Object.hasOwn(aliases, current.id)) continue;
    snapshots.set(current.id, preserveCurrentMessage(current, snapshots.get(current.id)));
  }
  const result: Message[] = [];
  const seen = new Set<string>();
  for (const message of [...existing, ...incoming]) {
    const id = Object.hasOwn(aliases, message.id) ? aliases[message.id]! : message.id;
    const snapshot = snapshots.get(id);
    if (!snapshot || seen.has(id)) continue;
    seen.add(id);
    result.push(snapshot);
  }
  return result;
}

export function mergeSubmissionGenerations(
  current: Record<string, Generation>,
  placeholders: Record<string, Generation>,
): Record<string, Generation> {
  const result = { ...current };
  for (const [id, placeholder] of Object.entries(placeholders)) {
    if (!Object.hasOwn(result, id)) result[id] = placeholder;
  }
  return result;
}
