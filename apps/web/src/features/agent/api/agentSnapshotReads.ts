import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import {
  getAgentActiveRun,
  listAgentMessages,
  type ListAgentMessagesOptions,
} from "./agentApi";

type PendingRead = {
  controller: AbortController;
  promise: Promise<unknown>;
  consumers: number;
};

/** Per-workspace, in-flight reads only: never a response or cross-user cache. */
export class AgentSnapshotReads {
  private readonly pending = new Map<string, PendingRead>();

  readonly messages = (sessionId: string, options: ListAgentMessagesOptions = {}) => {
    const { signal, ...params } = options;
    return this.read(
      ["messages", sessionId, params.cursor ?? "", params.since ?? "",
        params.limit ?? null, params.includeTasks !== false],
      signal,
      (sharedSignal) => listAgentMessages(sessionId, { ...params, signal: sharedSignal }),
    );
  };

  readonly activeRun = (sessionId: string, signal?: AbortSignal) => this.read(
    ["active-run", sessionId], signal,
    (sharedSignal) => getAgentActiveRun(sessionId, sharedSignal),
  );

  private read<T>(
    parts: unknown[],
    signal: AbortSignal | undefined,
    load: (signal: AbortSignal) => Promise<T>,
  ): Promise<T> {
    if (signal?.aborted) return Promise.reject(signal.reason);
    const identity = getPrivateIdentitySnapshot();
    const key = JSON.stringify([identity.userId, identity.epoch, ...parts]);
    const assertCurrent = () => {
      if (!isPrivateIdentitySnapshotCurrent(identity)) {
        throw new DOMException("Agent snapshot belongs to a previous session", "AbortError");
      }
    };
    let pending = this.pending.get(key);
    if (!pending) {
      const controller = new AbortController();
      pending = {
        controller,
        promise: Promise.resolve().then(async () => {
          controller.signal.throwIfAborted();
          assertCurrent();
          const result = await load(controller.signal);
          assertCurrent();
          return result;
        }),
        consumers: 0,
      };
      this.pending.set(key, pending);
      const created = pending;
      const clear = () => {
        if (this.pending.get(key) === created) this.pending.delete(key);
      };
      void created.promise.then(clear, clear);
    }
    const entry = pending;
    entry.consumers += 1;
    return new Promise<T>((resolve, reject) => {
      let settled = false;
      const finish = () => {
        if (settled) return false;
        settled = true;
        signal?.removeEventListener("abort", onAbort);
        entry.consumers -= 1;
        // One observer unmounting must not cancel another observer's recovery.
        // Once all observers leave, release both the network and the map entry.
        if (entry.consumers === 0 && this.pending.get(key) === entry) {
          this.pending.delete(key);
          entry.controller.abort();
        }
        return true;
      };
      const onAbort = () => {
        if (finish()) reject(signal?.reason);
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      void entry.promise.then(
        (value) => { if (finish()) resolve(value as T); },
        (error: unknown) => { if (finish()) reject(error); },
      );
    });
  }
}
