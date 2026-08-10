import { match, strictEqual } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import type { ProviderItemOut } from "@/lib/types";

const modelUrl = new URL("./model.ts", import.meta.url);
const { emptyDraft, providerOutToIn, toDraft } = (await import(
  modelUrl.href
)) as typeof import("./model");

const provider: ProviderItemOut = {
  name: "Flux",
  base_url: "https://flux.example/v1",
  api_key_hint: "****test",
  priority: 0,
  weight: 1,
  enabled: true,
  purposes: ["image"],
  proxy: null,
  image_jobs_enabled: false,
  image_streaming_enabled: true,
  image_jobs_endpoint: "generations",
  image_jobs_endpoint_lock: true,
  image_jobs_base_url: "",
  image_edit_input_transport: "url",
  image_concurrency: 16,
};

test("provider drafts preserve streaming and default it off", () => {
  strictEqual(toDraft(provider).image_streaming_enabled, true);
  strictEqual(providerOutToIn(provider).image_streaming_enabled, true);
  strictEqual(emptyDraft().image_streaming_enabled, false);
});

test("provider save payload and editor expose image streaming", () => {
  const stateSource = readFileSync(
    new URL("./useProviderPanelState.ts", import.meta.url),
    "utf8",
  );
  const editorSource = readFileSync(
    new URL("./editor.tsx", import.meta.url),
    "utf8",
  );

  match(
    stateSource,
    /image_streaming_enabled: draft\.image_streaming_enabled/,
  );
  match(editorSource, /label="流式生图"/);
  match(
    editorSource,
    /支持 Images API stream，最终图片事件到达后立即结束等待。/,
  );
});
