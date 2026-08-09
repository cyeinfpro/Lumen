import { deepEqual, equal, ok } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";
import type { ConnectionState } from "./connectionMachine";

type TestMachineConfig = {
  now(): number;
  retryDelay(attempt: number): number;
};

const { transitionConnection } = loadTsModule(
  new URL("./connectionMachine.ts", import.meta.url),
) as {
  transitionConnection(
    state: ConnectionState,
    event: Record<string, unknown>,
    config: TestMachineConfig,
  ): {
    state: ConnectionState;
    effects: Array<{ kind: string }>;
  };
};

const config = {
  now: () => 0,
  retryDelay: (attempt: number) => 1000 * (attempt + 1),
};

test("connection machine retries indefinitely and stays pure", () => {
  let state: ConnectionState = { kind: "idle" };
  state = transitionConnection(state, { type: "start" }, config).state;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const failed = transitionConnection(
      state,
      { type: "error", at: attempt * 1000 },
      config,
    );
    equal(failed.state.kind, "backoff");
    ok(failed.effects.some((effect) => effect.kind === "scheduleRetry"));
    state = transitionConnection(
      failed.state,
      { type: "retry_timer" },
      config,
    ).state;
    equal(state.kind, "connecting");
  }
});

test("offline, replay recovery, failure, success, and unauthorized are explicit", () => {
  const open: ConnectionState = {
    kind: "open",
    cursor: "8-0",
    openedAt: 1,
  };
  const offline = transitionConnection(open, { type: "offline" }, config);
  equal(offline.state.kind, "offline");
  ok(offline.effects.some((effect) => effect.kind === "closeSource"));

  const gap = transitionConnection(
    open,
    {
      type: "replay_gap",
      reason: "too_many_events",
      cursor: "9-0",
    },
    config,
  );
  equal(gap.state.kind, "snapshot_recovering");
  ok(gap.effects.some((effect) => effect.kind === "recoverSnapshot"));

  const required = transitionConnection(
    open,
    {
      type: "recovery_required",
      reason: "connection_slot_lost",
      cursor: "9-1",
    },
    config,
  );
  equal(required.state.kind, "snapshot_recovering");
  if (required.state.kind === "snapshot_recovering") {
    equal(required.state.reason.kind, "recovery_required");
    equal(required.state.reason.reason, "connection_slot_lost");
  }

  const failed = transitionConnection(
    gap.state,
    { type: "snapshot_failure", at: 100 },
    config,
  );
  equal(failed.state.kind, "snapshot_recovering");
  ok(failed.effects.some((effect) => effect.kind === "scheduleRetry"));

  const recovered = transitionConnection(
    failed.state,
    { type: "snapshot_success", cursor: "10-0" },
    config,
  );
  equal(recovered.state.kind, "connecting");
  if (recovered.state.kind === "connecting") {
    equal(recovered.state.snapshotReady, true);
  }
  ok(recovered.effects.some((effect) => effect.kind === "openSource"));

  const unauthorized = transitionConnection(
    open,
    { type: "unauthorized" },
    config,
  );
  equal(unauthorized.state.kind, "unauthorized");
  equal(
    transitionConnection(
      unauthorized.state,
      { type: "manual_reconnect" },
      config,
    ).state.kind,
    "unauthorized",
  );
});

test("initial transport open is gated by snapshot recovery with exponential retry", () => {
  const delays: number[] = [];
  const exponentialConfig = {
    now: () => 0,
    retryDelay(attempt: number) {
      const delay = 1000 * 2 ** attempt;
      delays.push(delay);
      return delay;
    },
  };
  let state: ConnectionState = transitionConnection(
    { kind: "idle" },
    { type: "start" },
    exponentialConfig,
  ).state;

  const firstOpen = transitionConnection(
    state,
    { type: "open", at: 10, snapshotRequired: true },
    exponentialConfig,
  );
  equal(firstOpen.state.kind, "snapshot_recovering");
  ok(firstOpen.effects.some((effect) => effect.kind === "closeSource"));
  ok(firstOpen.effects.some((effect) => effect.kind === "recoverSnapshot"));
  equal(firstOpen.effects.some((effect) => effect.kind === "openSource"), false);

  const firstFailure = transitionConnection(
    firstOpen.state,
    { type: "snapshot_failure", at: 100 },
    exponentialConfig,
  );
  equal(firstFailure.state.kind, "snapshot_recovering");
  if (firstFailure.state.kind === "snapshot_recovering") {
    equal(firstFailure.state.attempt, 1);
    equal(firstFailure.state.retryAt, 1100);
  }

  const firstRetry = transitionConnection(
    firstFailure.state,
    { type: "retry_timer" },
    exponentialConfig,
  );
  ok(firstRetry.effects.some((effect) => effect.kind === "recoverSnapshot"));

  const secondFailure = transitionConnection(
    firstRetry.state,
    { type: "snapshot_failure", at: 1100 },
    exponentialConfig,
  );
  equal(secondFailure.state.kind, "snapshot_recovering");
  if (secondFailure.state.kind === "snapshot_recovering") {
    equal(secondFailure.state.attempt, 2);
    equal(secondFailure.state.retryAt, 3100);
  }
  deepEqual(delays, [1000, 2000]);

  const recovered = transitionConnection(
    secondFailure.state,
    { type: "snapshot_success", cursor: "12-0" },
    exponentialConfig,
  );
  equal(recovered.state.kind, "connecting");
  if (recovered.state.kind !== "connecting") return;
  equal(recovered.state.snapshotReady, true);
  state = recovered.state;

  const finalOpen = transitionConnection(
    state,
    { type: "open", at: 4000, snapshotRequired: true },
    exponentialConfig,
  );
  equal(finalOpen.state.kind, "open");
  equal(
    finalOpen.effects.some((effect) => effect.kind === "recoverSnapshot"),
    false,
  );
});
