import type {
  GeneratedImage,
  Generation,
} from "../../lib/types";

interface Base64EvictionState {
  currentConvId: string | null;
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
}

interface Base64EvictionResolvers {
  generationConversationId: (generation: Generation) => string | null;
  imageConversationId: (
    imageId: string,
    image: GeneratedImage,
    generation: Generation | undefined,
  ) => string | null;
  releaseImage: (image: GeneratedImage) => GeneratedImage;
}

function isInflightGeneration(generation: Generation | undefined): boolean {
  return generation?.status === "queued" || generation?.status === "running";
}

function belongsToCurrentConversation(
  currentConvId: string | null,
  conversationId: string | null,
): boolean {
  return currentConvId != null && conversationId === currentConvId;
}

function evictGenerationImages(
  state: Base64EvictionState,
  resolvers: Base64EvictionResolvers,
): {
  generations: Record<string, Generation>;
  changed: boolean;
} {
  let changed = false;
  const generations: Record<string, Generation> = {};
  for (const [id, generation] of Object.entries(state.generations)) {
    const keep =
      isInflightGeneration(generation) ||
      belongsToCurrentConversation(
        state.currentConvId,
        resolvers.generationConversationId(generation),
      );
    if (keep || !generation.image) {
      generations[id] = generation;
      continue;
    }
    const released = resolvers.releaseImage(generation.image);
    generations[id] =
      released === generation.image
        ? generation
        : { ...generation, image: released };
    changed ||= released !== generation.image;
  }
  return { generations, changed };
}

function evictStandaloneImages(
  state: Base64EvictionState,
  resolvers: Base64EvictionResolvers,
): {
  imagesById: Record<string, GeneratedImage>;
  changed: boolean;
} {
  let changed = false;
  const imagesById: Record<string, GeneratedImage> = {};
  for (const [id, image] of Object.entries(state.imagesById)) {
    const generation = image.from_generation_id
      ? state.generations[image.from_generation_id]
      : undefined;
    const keep =
      isInflightGeneration(generation) ||
      belongsToCurrentConversation(
        state.currentConvId,
        resolvers.imageConversationId(id, image, generation),
      );
    if (keep) {
      imagesById[id] = image;
      continue;
    }
    const released = resolvers.releaseImage(image);
    imagesById[id] = released;
    changed ||= released !== image;
  }
  return { imagesById, changed };
}

export function buildBase64EvictionPatch(
  state: Base64EvictionState,
  resolvers: Base64EvictionResolvers,
): Pick<Base64EvictionState, "generations" | "imagesById"> | null {
  const generationResult = evictGenerationImages(state, resolvers);
  const imageResult = evictStandaloneImages(state, resolvers);
  if (!generationResult.changed && !imageResult.changed) return null;
  return {
    generations: generationResult.changed
      ? generationResult.generations
      : state.generations,
    imagesById: imageResult.changed ? imageResult.imagesById : state.imagesById,
  };
}
