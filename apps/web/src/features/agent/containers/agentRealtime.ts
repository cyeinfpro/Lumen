import type { Generation } from "@/lib/types";

export const AGENT_GENERATION_EVENT_NAMES = [
  "generation.queued",
  "generation.started",
  "generation.progress",
  "generation.partial_image",
  "generation.succeeded",
  "generation.failed",
  "generation.canceled",
  "generation.retrying",
] as const;

export function selectAgentGenerationChannelIds(
  generations: Record<string, Generation>,
  owners: Record<string, string>,
  sessionId: string | null,
  maximum = 63,
): string[] {
  if (!sessionId) return [];
  return Object.values(generations)
    .filter(
      (generation) =>
        owners[generation.id] === sessionId &&
        (generation.status === "queued" || generation.status === "running"),
    )
    .sort((left, right) => {
      const byCreated =
        (right.created_at ?? right.started_at) -
        (left.created_at ?? left.started_at);
      return byCreated || right.id.localeCompare(left.id);
    })
    .slice(0, maximum)
    .map((generation) => generation.id);
}

export function mergeAgentGeneration(
  existing: Generation | undefined,
  incoming: Generation,
): Generation {
  if (!existing) return incoming;
  const existingEpoch = existing.execution_epoch ?? 0;
  const incomingEpoch = incoming.execution_epoch ?? 0;
  if (incomingEpoch < existingEpoch) return existing;
  if (incomingEpoch > existingEpoch) {
    return {
      ...incoming,
      image: incoming.image,
      error_code: incoming.error_code,
      error_message: incoming.error_message,
      finished_at: incoming.finished_at,
    };
  }
  const existingTerminal =
    existing.status === "succeeded" ||
    existing.status === "failed" ||
    existing.status === "canceled";
  const incomingTerminal =
    incoming.status === "succeeded" ||
    incoming.status === "failed" ||
    incoming.status === "canceled";
  if (existingTerminal && !incomingTerminal) return existing;
  if (incoming.attempt < existing.attempt) return existing;
  return {
    ...existing,
    ...incoming,
    image: incoming.image ?? existing.image,
  };
}

export class AgentRefreshCoordinator {
  private running: Promise<void> | null = null;
  private trailing: (() => Promise<void>) | null = null;

  request(refresh: () => Promise<void>): Promise<void> {
    if (this.running) {
      this.trailing = refresh;
      return this.running;
    }
    const execute = async () => {
      let next: (() => Promise<void>) | null = refresh;
      let lastError: unknown = null;
      while (next) {
        const current = next;
        this.trailing = null;
        try {
          await current();
          lastError = null;
        } catch (error) {
          lastError = error;
        }
        next = this.trailing;
      }
      if (lastError) throw lastError;
    };
    const pending = execute().finally(() => {
      if (this.running === pending) this.running = null;
    });
    this.running = pending;
    return pending;
  }
}
