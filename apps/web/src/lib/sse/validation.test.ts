import { deepStrictEqual, equal } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../test-support/load-ts-module.mjs";

const { parseRealtimeEvent } = loadTsModule(
  new URL("./validation.ts", import.meta.url),
) as {
  parseRealtimeEvent(input: {
    name: string;
    data: unknown;
    cursor?: string;
    allowedDomainEvents: ReadonlySet<string>;
  }): { kind: string; event?: { kind: string } };
};

test("control and domain events are parsed separately", () => {
  deepStrictEqual(
    parseRealtimeEvent({
      name: "replay_truncated",
      data: JSON.stringify({
        reason: "too_many_events",
        cursor: "12-0",
        limit: 100,
      }),
      allowedDomainEvents: new Set(),
    }),
    {
      kind: "event",
      event: {
        kind: "control",
        type: "replay_truncated",
        version: 1,
        reason: "too_many_events",
        cursor: "12-0",
        limit: 100,
      },
    },
  );

  const domain = parseRealtimeEvent({
    name: "generation.succeeded",
    data: { generation_id: "gen-1" },
    cursor: "13-0",
    allowedDomainEvents: new Set(["generation.succeeded"]),
  });
  equal(domain.kind, "event");
  if (domain.kind === "event" && domain.event) {
    equal(domain.event.kind, "domain");
  }
});

test("invalid json, shape, version, and unknown event are observable", () => {
  equal(
    parseRealtimeEvent({
      name: "generation.succeeded",
      data: "{",
      allowedDomainEvents: new Set(["generation.succeeded"]),
    }).kind,
    "invalid",
  );
  equal(
    parseRealtimeEvent({
      name: "generation.succeeded",
      data: [],
      allowedDomainEvents: new Set(["generation.succeeded"]),
    }).kind,
    "invalid",
  );
  deepStrictEqual(
    parseRealtimeEvent({
      name: "generation.succeeded",
      data: { schema_version: 2 },
      allowedDomainEvents: new Set(["generation.succeeded"]),
    }),
    { kind: "invalid", reason: "unknown_version" },
  );
  deepStrictEqual(
    parseRealtimeEvent({
      name: "future.event",
      data: {},
      allowedDomainEvents: new Set(),
    }),
    { kind: "unknown", type: "future.event" },
  );
});
