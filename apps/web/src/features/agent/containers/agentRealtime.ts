import type { Generation } from "@/lib/types";


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
      let firstError: unknown = null;
      while (next) {
        const current = next;
        this.trailing = null;
        try {
          await current();
        } catch (error) {
          firstError ??= error;
        }
        next = this.trailing;
      }
      if (firstError) throw firstError;
    };
    const pending = execute().finally(() => {
      if (this.running === pending) this.running = null;
    });
    this.running = pending;
    return pending;
  }
}
