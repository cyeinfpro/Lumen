import type { RealtimeRuntime } from "@/features/realtime/model/runtime";

import { createRealtimeRuntime } from "./factory";

const runtimes = new Map<string, RealtimeRuntime>();

export interface RealtimeRuntimeLease {
  key: string;
  runtime: RealtimeRuntime;
}

function channelKey(channels: readonly string[]): string {
  return [...channels].sort().join(",");
}

export function acquireRealtimeRuntime(
  channels: readonly string[],
): RealtimeRuntimeLease {
  const key = channelKey(channels);
  let runtime = runtimes.get(key);
  if (!runtime) {
    runtime = createRealtimeRuntime({
      channels: key.split(",").filter(Boolean),
    });
    runtimes.set(key, runtime);
  }
  return { key, runtime };
}

export function releaseRealtimeRuntime(
  lease: RealtimeRuntimeLease,
): void {
  if (
    runtimes.get(lease.key) === lease.runtime &&
    !lease.runtime.active()
  ) {
    runtimes.delete(lease.key);
  }
}

export function invalidateRealtimeRuntimes(): void {
  for (const runtime of runtimes.values()) {
    runtime.invalidateSession();
  }
}
