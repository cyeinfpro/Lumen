import { deepStrictEqual, equal, rejects } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../test-support/load-ts-module.mjs";
import type {
  SnapshotAdapter,
  SnapshotResult,
} from "./replayCoordinator";
import type { RecoveryReason } from "./contracts";

const { ReplayCoordinator } = loadTsModule(
  new URL("./replayCoordinator.ts", import.meta.url),
) as {
  ReplayCoordinator: new (adapter: SnapshotAdapter) => {
    recover(reason: RecoveryReason): Promise<SnapshotResult>;
    lastSuccessfulSyncAt(): number;
  };
};

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
  const first = coordinator.recover(reason);
  const second = coordinator.recover(reason);
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
  await rejects(coordinator.recover(reason), /offline/);
  deepStrictEqual(await coordinator.recover(reason), {
    cursor: "21-0",
    syncedAt: 60,
  });
  equal(calls, 2);
});
