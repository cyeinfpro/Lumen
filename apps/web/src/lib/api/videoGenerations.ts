import type { VideoCreateIn, VideoGenerationOut } from "../types";
import { apiFetch } from "./http";
import {
  idempotentPostRequest,
  semanticPostRequest,
  withSemanticPostIdempotency,
} from "./semanticIdempotency";

type VideoCreatePayload = Omit<VideoCreateIn, "idempotency_key">;

function validateVideoGenerationResponse(
  value: VideoGenerationOut,
): VideoGenerationOut {
  if (
    value === null ||
    typeof value !== "object" ||
    typeof value.id !== "string" ||
    value.id.length === 0
  ) {
    throw new TypeError("malformed video generation response");
  }
  return value;
}

function submitVideoGeneration(
  payload: VideoCreatePayload,
  idempotencyKey: string,
): Promise<VideoGenerationOut> {
  return apiFetch<VideoGenerationOut>(
    "/videos/generations",
    idempotentPostRequest({
      ...payload,
      idempotency_key: idempotencyKey,
    }),
  ).then(validateVideoGenerationResponse);
}

export function createVideoGeneration(
  body: VideoCreatePayload & { idempotency_key?: string },
  options: { idempotency_key?: string } = {},
): Promise<VideoGenerationOut> {
  const { idempotency_key: bodyKey, ...payload } = body;
  const explicitKey = bodyKey ?? options.idempotency_key;
  if (explicitKey) return submitVideoGeneration(payload, explicitKey);
  return withSemanticPostIdempotency(
    { operation: "video.generation.create" },
    payload,
    (idempotencyKey) => submitVideoGeneration(payload, idempotencyKey),
  );
}

export function cancelVideoGeneration(
  id: string,
): Promise<VideoGenerationOut> {
  return apiFetch<VideoGenerationOut>(
    `/videos/generations/${encodeURIComponent(id)}/cancel`,
    { method: "POST" },
  ).then(validateVideoGenerationResponse);
}

export function retryVideoGeneration(
  id: string,
): Promise<VideoGenerationOut> {
  const payload = {};
  return withSemanticPostIdempotency(
    { operation: "video.generation.retry", generationId: id },
    payload,
    (idempotencyKey) =>
      apiFetch<VideoGenerationOut>(
        `/videos/generations/${encodeURIComponent(id)}/retry`,
        semanticPostRequest(idempotencyKey),
      ).then(validateVideoGenerationResponse),
  );
}
