import { deepStrictEqual, equal, rejects } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";
import type {
  SnapshotAdapter,
  SnapshotExecutionContext,
  SnapshotResult,
} from "./replayCoordinator";
import type { RecoveryReason } from "./contracts";

const { ReplayCoordinator } = loadTsModule(
  new URL("./replayCoordinator.ts", import.meta.url),
) as {
  ReplayCoordinator: new (adapter: SnapshotAdapter) => {
    recover(
      reason: RecoveryReason,
      signal: AbortSignal,
      context: SnapshotExecutionContext,
    ): Promise<SnapshotResult>;
    lastSuccessfulSyncAt(): number;
  };
};

function snapshotContext(
  isCurrent: () => boolean = () => true,
): SnapshotExecutionContext {
  return {
    connectionGeneration: 1,
    userScope: "user:u1",
    isCurrent,
  };
}

test("replay recovery is singleflight and commits only successful cursor", async () => {
  let calls = 0;
  let release!: (value: { cursor: string; syncedAt: number }) => void;
  const pending = new Promise<{ cursor: string; syncedAt: number }>((resolve) => {
    release = resolve;
  });
  const coordinator = new ReplayCoordinator(async (scopes) => {
    calls += 1;
    deepStrictEqual(scopes, [
      "identity",
      "conversations",
      "activeTasks",
      "wallet",
      "runtimeDefaults",
    ]);
    return pending;
  });
  const reason = { kind: "replay_gap", reason: "gap" } as const;
  const signal = new AbortController().signal;
  const context = snapshotContext();
  const first = coordinator.recover(reason, signal, context);
  const second = coordinator.recover(reason, signal, context);
  equal(calls, 1);
  release({ cursor: "20-0", syncedAt: 50 });
  deepStrictEqual(await first, { cursor: "20-0", syncedAt: 50 });
  deepStrictEqual(await second, { cursor: "20-0", syncedAt: 50 });
  equal(coordinator.lastSuccessfulSyncAt(), 50);
});

test("failed snapshot remains retryable", async () => {
  let calls = 0;
  const coordinator = new ReplayCoordinator(async () => {
    calls += 1;
    if (calls === 1) throw new Error("offline");
    return { cursor: "21-0", syncedAt: 60 };
  });
  const reason = { kind: "replay_gap", reason: "gap" } as const;
  const signal = new AbortController().signal;
  const context = snapshotContext();
  await rejects(coordinator.recover(reason, signal, context), /offline/);
  deepStrictEqual(await coordinator.recover(reason, signal, context), {
    cursor: "21-0",
    syncedAt: 60,
  });
  equal(calls, 2);
});

test("stale connection generation cannot commit a snapshot result", async () => {
  let release!: () => void;
  let current = true;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const coordinator = new ReplayCoordinator(async () => {
    await pending;
    return { cursor: "stale-22-0", syncedAt: 70 };
  });
  const recovery = coordinator.recover(
    { kind: "replay_gap", reason: "gap" },
    new AbortController().signal,
    snapshotContext(() => current),
  );

  current = false;
  release();

  await rejects(recovery, (error: unknown) => {
    return error instanceof Error && error.name === "AbortError";
  });
  equal(coordinator.lastSuccessfulSyncAt(), 0);
});
