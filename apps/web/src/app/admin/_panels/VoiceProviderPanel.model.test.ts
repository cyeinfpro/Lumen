import { deepStrictEqual, equal, ok } from "node:assert/strict";
import { test } from "node:test";

const model = new URL("./VoiceProviderPanel.model.ts", import.meta.url);
const { BEIJING, SINGAPORE, voiceProviderPatch, voiceProviderStatusLabel } =
  (await import(model.href)) as typeof import("./VoiceProviderPanel.model");

const empty: import("./VoiceProviderPanel.model").VoiceProviderStatus = { configured: false, ready: false, region: BEIJING, workspace_id: "" };
const configured = { ...empty, configured: true };

test("a Beijing key alone can bootstrap the provider", () => {
  deepStrictEqual(voiceProviderPatch(empty, {}, " sample-key-001 "), {
    body: { api_key: "sample-key-001" },
  });
  ok("error" in voiceProviderPatch(empty, {}, ""));
});

test("blank input preserves the existing key; a scope change needs a replacement", () => {
  deepStrictEqual(voiceProviderPatch(configured, {}, ""), { body: {} });
  ok("error" in voiceProviderPatch(configured, { region: SINGAPORE }, ""));
  deepStrictEqual(voiceProviderPatch(configured, { region: SINGAPORE, workspace: " ABC-1 " }, "sample-key-002"), {
    body: { api_key: "sample-key-002", region: SINGAPORE, workspace_id: "abc-1" },
  });
  deepStrictEqual(voiceProviderPatch({ ...configured, workspace_id: "old" }, { workspace: " " }, "sample-key-003"), {
    body: { api_key: "sample-key-003", workspace_id: "" },
  });
});

test("invalid inputs never enter the request body", () => {
  ok("error" in voiceProviderPatch(empty, { workspace: "https://server" }, "sample-key-001"));
  ok("error" in voiceProviderPatch(empty, {}, "key with spaces"));
});

test("a stored key is not presented as ready until a worker acknowledges it", () => {
  equal(voiceProviderStatusLabel(empty), "未配置");
  equal(voiceProviderStatusLabel(configured), "已保存 · 等待服务就绪");
  equal(voiceProviderStatusLabel({ ...configured, ready: true }), "已启用");
});
