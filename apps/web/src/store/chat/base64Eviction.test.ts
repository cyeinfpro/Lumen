import assert from "node:assert/strict";
import test from "node:test";
import type {
  GeneratedImage,
  Generation,
} from "../../lib/types";

const { buildBase64EvictionPatch } = await import(
  new URL("./base64Eviction.ts", import.meta.url).href
);

function image(id: string): GeneratedImage {
  return {
    id,
    data_url: `data:image/png;base64,${id}`,
    width: 10,
    height: 10,
    parent_image_id: null,
    from_generation_id: `generation-${id}`,
    size_requested: "10x10",
    size_actual: "10x10",
  };
}

function generation(
  id: string,
  status: Generation["status"],
  generatedImage: GeneratedImage,
): Generation {
  return {
    id,
    message_id: `message-${id}`,
    action: "generate",
    prompt: "",
    size_requested: "10x10",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status,
    stage: status === "succeeded" ? "finalizing" : "rendering",
    image: generatedImage,
    attempt: 0,
    started_at: 0,
  };
}

test("base64 eviction releases inactive conversation images only", () => {
  const inactiveImage = image("inactive");
  const activeImage = image("active");
  const inactiveGeneration = generation(
    inactiveImage.from_generation_id,
    "succeeded",
    inactiveImage,
  );
  const activeGeneration = generation(
    activeImage.from_generation_id,
    "running",
    activeImage,
  );
  const releasedIds: string[] = [];

  const patch = buildBase64EvictionPatch(
    {
      currentConvId: "conversation-current",
      generations: {
        [inactiveGeneration.id]: inactiveGeneration,
        [activeGeneration.id]: activeGeneration,
      },
      imagesById: {
        [inactiveImage.id]: inactiveImage,
        [activeImage.id]: activeImage,
      },
    },
    {
      generationConversationId: () => "conversation-other",
      imageConversationId: () => "conversation-other",
      releaseImage: (candidate: GeneratedImage) => {
        releasedIds.push(candidate.id);
        return { ...candidate, data_url: `/images/${candidate.id}` };
      },
    },
  );

  assert.ok(patch);
  assert.equal(
    patch.generations[inactiveGeneration.id]?.image?.data_url,
    "/images/inactive",
  );
  assert.equal(
    patch.generations[activeGeneration.id],
    activeGeneration,
  );
  assert.equal(patch.imagesById.inactive?.data_url, "/images/inactive");
  assert.equal(patch.imagesById.active, activeImage);
  assert.deepEqual(releasedIds, ["inactive", "inactive"]);
});

test("base64 eviction preserves collection identities when nothing changes", () => {
  const currentImage = image("current");
  const currentGeneration = generation(
    currentImage.from_generation_id,
    "succeeded",
    currentImage,
  );
  const patch = buildBase64EvictionPatch(
    {
      currentConvId: "conversation-current",
      generations: { [currentGeneration.id]: currentGeneration },
      imagesById: { [currentImage.id]: currentImage },
    },
    {
      generationConversationId: () => "conversation-current",
      imageConversationId: () => "conversation-current",
      releaseImage: (candidate: GeneratedImage) => candidate,
    },
  );

  assert.equal(patch, null);
});
