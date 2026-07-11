import assert from "node:assert/strict";
import test from "node:test";
import type { ChatState } from "./types";
import "./moduleResolution.test-helper.mjs";

const actionNames = [
  "setCurrentUser",
  "setCurrentConv",
  "applyRuntimeDefaults",
  "setComposerError",
  "setText",
  "setMode",
  "setForceIntent",
  "setAspectRatio",
  "setSizeMode",
  "setFixedSize",
  "setQuality",
  "setRenderQuality",
  "setImageCount",
  "setReasoningEffort",
  "setFast",
  "setWebSearch",
  "setFileSearch",
  "setCodeInterpreter",
  "setImageGeneration",
  "addAttachment",
  "removeAttachment",
  "moveAttachment",
  "setMask",
  "clearMask",
  "clearComposer",
  "promoteImageToReference",
  "uploadAttachment",
  "sendMessage",
  "loadHistoricalMessages",
  "retryAssistant",
  "retryGeneration",
  "regenerateAssistant",
  "upscaleImage",
  "rerollImage",
  "submitInpaintTask",
  "appendUserMessage",
  "appendAssistantMessage",
  "upsertGeneration",
  "attachImageToGeneration",
  "applySSEEvent",
  "pollInflightTasks",
  "hydrateActiveTasks",
  "refreshCompletionText",
  "reset",
] as const satisfies ReadonlyArray<keyof ChatState>;

test("all public chat actions and singleton methods retain references", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalFetch = globalThis.fetch;
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    writable: true,
    value: new EventTarget(),
  });

  const chatModule = await import(
    new URL("../useChatStore.ts", import.meta.url).href
  );
  try {
    const { useChatStore } = chatModule;
    const initial = useChatStore.getState();
    const actionReferences = Object.fromEntries(
      actionNames.map((name) => [name, initial[name]]),
    );
    const getStateReference = useChatStore.getState;
    const setStateReference = useChatStore.setState;

    useChatStore.setState({ composerError: "changed" });
    const updated = useChatStore.getState();
    for (const name of actionNames) {
      assert.equal(updated[name], actionReferences[name]);
    }
    assert.equal(useChatStore.getState, getStateReference);
    assert.equal(useChatStore.setState, setStateReference);

    updated.reset();
    const reset = useChatStore.getState();
    for (const name of actionNames) {
      assert.equal(reset[name], actionReferences[name]);
    }

    reset.setText("draft before identity");
    reset.setCurrentUser("user-a");
    assert.equal(useChatStore.getState().currentUserId, "user-a");
    assert.equal(
      useChatStore.getState().composer.text,
      "draft before identity",
    );

    useChatStore.getState().setCurrentConv("conv-a");
    useChatStore.getState().setText("private draft");
    useChatStore.getState().setCurrentUser("user-b");
    assert.equal(useChatStore.getState().currentUserId, "user-b");
    assert.equal(useChatStore.getState().currentConvId, null);
    assert.equal(useChatStore.getState().composer.text, "");

    useChatStore.getState().setCurrentConv("conv-b");
    useChatStore.setState({ messagesLoading: true });
    let historyRequests = 0;
    globalThis.fetch = async () => {
      historyRequests += 1;
      return new Response(
        JSON.stringify({
          items: [],
          generations: [],
          completions: [],
          images: [],
          next_cursor: null,
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    };

    await useChatStore.getState().loadHistoricalMessages("conv-b");
    assert.equal(historyRequests, 1);
    assert.equal(useChatStore.getState().messagesLoading, false);
  } finally {
    chatModule.disposeChatStoreRuntime();
    globalThis.fetch = originalFetch;
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", originalWindow);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
  }
});
