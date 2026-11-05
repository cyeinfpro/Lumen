import { apiFetch } from "./http";
import { apiTransport } from "./transport";
import { semanticPostRequest, withSemanticPostIdempotency } from "./semanticIdempotency";

export interface GifInfo {
  width: number;
  height: number;
  frame_count: number;
  durations_ms: number[];
  total_duration_ms: number;
  loop: number | null;
}
export interface GifResult { id: string; width: number; height: number; mime: string; }
const root = (id: string) => `/images/${encodeURIComponent(id)}/gif`;
export const inspectGif = (id: string, signal: AbortSignal) =>
  apiFetch<GifInfo>(root(id), { signal });
export const extractGifFrame = (id: string, index: number, signal: AbortSignal) =>
  apiTransport.requestRaw<Blob>(`${root(id)}/frames/${index}`,
    { method: "GET", requestClass: "query", signal }, (response) => response.blob());
export function retimeGif(id: string, speed: number) {
  return withSemanticPostIdempotency(["gif-retime", id], { speed }, (key) =>
    apiFetch<GifResult>(`${root(id)}/retime`, semanticPostRequest(key, { body: JSON.stringify({ speed }) })));
}
export async function replaceGifFrame(id: string, index: number, file: File) {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  const sha256 = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
  return withSemanticPostIdempotency(["gif-replace", id, index], { sha256 }, (key) => {
    const body = new FormData();
    body.append("file", file);
    return apiFetch<GifResult>(`${root(id)}/frames/${index}`, semanticPostRequest(key, { body }));
  });
}
