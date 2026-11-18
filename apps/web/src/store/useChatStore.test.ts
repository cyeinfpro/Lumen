import assert from "node:assert/strict";
import test from "node:test";
import type {
  AssistantMessage,
  GeneratedImage,
  Generation,
} from "../lib/types";
import type { ChatState } from "./chat/types";
import "./chat/moduleResolution.test-helper.mjs";

function makeImage(id: string, generationId: string): GeneratedImage {
  return {
    id,
    data_url: `/api/images/${id}/binary`,
    width: 512,
    height: 512,
    parent_image_id: null,
    from_generation_id: generationId,
    size_requested: "512x512",
    size_actual: "512x512",
  };
}

function makeGeneration(
  id: string,
  status: Generation["status"],
  image: GeneratedImage,
  convId?: string,
  timestamp = 1,
): Generation {
  return {
    id,
    message_id: `msg-${id}`,
    action: "generate",
    prompt: `prompt ${id}`,
    size_requested: "512x512",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status,
    stage: status === "succeeded" ? "finalizing" : "rendering",
    image,
    conversation_id: convId ?? null,
    attempt: 0,
    started_at: timestamp,
    finished_at: status === "succeeded" || status === "failed" ? timestamp : undefined,
  };
}

function makeAssistantMessage(
  id: string,
  generationId: string,
  status: AssistantMessage["status"] = "succeeded",
): AssistantMessage {
  return {
    id,
    role: "assistant",
    parent_user_message_id: `user-${id}`,
    intent_resolved: "text_to_image",
    status,
    generation_id: generationId,
    generation_ids: [generationId],
    created_at: 1,
  };
}

test("useChatStore LRU GC preserves active conv, 32 cached convs, and non-terminal tasks while bounding terminal items", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    writable: true,
    value: new EventTarget(),
  });

  const chatModule = await import(
    new URL("./useChatStore.ts", import.meta.url).href
  );
  const {
    CONVERSATION_HISTORY_MEDIA_CACHE_LIMIT,
    MAX_RETAINED_TERMINAL_MEDIA_ITEMS,
    runTerminalMediaLruGc,
    useChatStore,
  } = chatModule;

  try {
    useChatStore.getState().reset();
    useChatStore.getState().setCurrentUser("user-gc");

    // 1. Seed 40 historical conversations:
    //    - conv-0..conv-6 (7 oldest conversations, pushed out of the 32-conversation cache)
    //    - conv-7..conv-38 (32 recently cached conversations, must be 100% retained!)
    //    - Also add non-terminal background tasks (queued / running) in old conv-0 and conv-1!
    for (let i = 0; i < 39; i += 1) {
      const convId = `conv-${i}`;
      useChatStore.getState().setCurrentConv(convId);
      const genId = `gen-conv-${i}`;
      const img = makeImage(`img-conv-${i}`, genId);
      const gen = makeGeneration(genId, "succeeded", img, convId, 100 + i);
      useChatStore.getState().upsertGeneration(gen);
      useChatStore.getState().attachImageToGeneration(genId, img);
      useChatStore.getState().appendAssistantMessage(
        makeAssistantMessage(`assistant-${i}`, genId, "succeeded"),
      );
    }

    // Add background non-terminal tasks belonging to old evicted conversations (conv-0, conv-1)
    const queuedImg = makeImage("img-bg-queued", "gen-bg-queued");
    const queuedGen = makeGeneration(
      "gen-bg-queued",
      "queued",
      queuedImg,
      "conv-0",
      10,
    );
    queuedGen.input_image_ids = ["img-bg-input"];
    const bgInputImg = makeImage("img-bg-input", "");

    const runningImg = makeImage("img-bg-running", "gen-bg-running");
    const runningGen = makeGeneration(
      "gen-bg-running",
      "running",
      runningImg,
      "conv-1",
      11,
    );

    useChatStore.setState((s: ChatState) => ({
      generations: {
        ...s.generations,
        [queuedGen.id]: queuedGen,
        [runningGen.id]: runningGen,
      },
      imagesById: {
        ...s.imagesById,
        [queuedImg.id]: queuedImg,
        [bgInputImg.id]: bgInputImg,
        [runningImg.id]: runningImg,
      },
    }));

    // Switch to active conversation conv-active with its own terminal item
    useChatStore.getState().setCurrentConv("conv-active");
    const activeImg = makeImage("img-active", "gen-active");
    const activeGen = makeGeneration(
      "gen-active",
      "succeeded",
      activeImg,
      "conv-active",
      999,
    );
    useChatStore.getState().upsertGeneration(activeGen);
    useChatStore.getState().attachImageToGeneration("gen-active", activeImg);
    useChatStore.getState().appendAssistantMessage(
      makeAssistantMessage("assistant-active", "gen-active", "succeeded"),
    );

    // Touch gen-conv-2 so it becomes newer in LRU than gen-conv-0,1,3,4,5,6
    useChatStore.getState().upsertGeneration({
      ...useChatStore.getState().generations["gen-conv-2"]!,
      prompt: "updated recently",
    });

    // Run LRU GC with maxRetained = 2 for inactive terminal items (outside active + 32 cached convs)
    // Note: conv-active is current; conv-7..conv-38 are the 32 cached recent convs (39 - 32 = 7 evicted convs: conv-0..conv-6).
    const changed = runTerminalMediaLruGc(2);
    assert.equal(changed, true);

    const afterSmallCap = useChatStore.getState();
    // Active conversation item is strictly retained
    assert.ok(afterSmallCap.generations["gen-active"]);
    assert.ok(afterSmallCap.imagesById["img-active"]);

    // All 32 cached conversations (conv-7..conv-38) are strictly retained
    assert.equal(CONVERSATION_HISTORY_MEDIA_CACHE_LIMIT, 32);
    for (let i = 7; i < 39; i += 1) {
      assert.ok(
        afterSmallCap.generations[`gen-conv-${i}`],
        `cached conv-${i} generation should be retained`,
      );
      assert.ok(
        afterSmallCap.imagesById[`img-conv-${i}`],
        `cached conv-${i} image should be retained`,
      );
    }

    // Non-terminal background tasks & their input/output images are strictly retained
    assert.ok(afterSmallCap.generations["gen-bg-queued"]);
    assert.ok(afterSmallCap.generations["gen-bg-running"]);
    assert.ok(afterSmallCap.imagesById["img-bg-queued"]);
    assert.ok(afterSmallCap.imagesById["img-bg-input"]);
    assert.ok(afterSmallCap.imagesById["img-bg-running"]);

    // Among conv-0..conv-6 (7 terminal inactive items), only the 2 most recently touched (conv-6 and touched conv-2) survive!
    assert.ok(afterSmallCap.generations["gen-conv-2"]);
    assert.ok(afterSmallCap.generations["gen-conv-6"]);
    for (const evictedIdx of [0, 1, 3, 4, 5]) {
      assert.equal(afterSmallCap.generations[`gen-conv-${evictedIdx}`], undefined);
      assert.equal(afterSmallCap.imagesById[`img-conv-${evictedIdx}`], undefined);
    }

    // 2. Verify automatic default MAX_RETAINED_TERMINAL_MEDIA_ITEMS = 1000 cap during setCurrentConv
    assert.equal(MAX_RETAINED_TERMINAL_MEDIA_ITEMS, 1_000);
    const bulkGenerations: Record<string, Generation> = {
      ...afterSmallCap.generations,
    };
    const bulkImages: Record<string, GeneratedImage> = {
      ...afterSmallCap.imagesById,
    };
    for (let i = 0; i < 1_040; i += 1) {
      const gid = `bulk-term-${i}`;
      const iid = `bulk-img-${i}`;
      const img = makeImage(iid, gid);
      bulkGenerations[gid] = makeGeneration(
        gid,
        i % 3 === 0 ? "failed" : i % 5 === 0 ? "canceled" : "succeeded",
        img,
        `ancient-conv-${i}`,
        1_000 + i,
      );
      bulkImages[iid] = img;
    }
    useChatStore.setState({
      generations: bulkGenerations,
      imagesById: bulkImages,
    });

    // Switching conversation triggers setCurrentConv -> automatic bounded LRU GC at 1000
    useChatStore.getState().setCurrentConv("conv-final");
    const finalState = useChatStore.getState();

    let remainingBulkCount = 0;
    for (let i = 0; i < 1_040; i += 1) {
      if (finalState.generations[`bulk-term-${i}`]) remainingBulkCount += 1;
    }
    // 2 earlier inactive terminal items (gen-conv-2, gen-conv-6 -> plus conv-7 pushed out when conv-final entered 32-cache) + bulk <= 1000 total inactive terminal items
    assert.ok(remainingBulkCount <= MAX_RETAINED_TERMINAL_MEDIA_ITEMS);
    assert.equal(finalState.generations["bulk-term-0"], undefined);
    assert.ok(finalState.generations["bulk-term-1039"]);
    // Non-terminal tasks remain untouched
    assert.ok(finalState.generations["gen-bg-queued"]);
    assert.ok(finalState.generations["gen-bg-running"]);
  } finally {
    chatModule.disposeChatStoreRuntime();
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", originalWindow);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
  }
});
