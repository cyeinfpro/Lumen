import { apiFetch } from "./http";
import { semanticPostRequest, withSemanticPostIdempotency } from "./semanticIdempotency";
import type { GifResult } from "./gifEditing";

export async function uploadEditableGif(file: File) {
  if (file.size > 32 * 1024 * 1024) throw new Error("GIF 不得超过 32 MiB");
  const data = await file.arrayBuffer();
  const signature = new TextDecoder().decode(data.slice(0, 6));
  if (signature !== "GIF87a" && signature !== "GIF89a") throw new Error("请选择有效的 GIF 文件");
  const hash = await crypto.subtle.digest("SHA-256", data);
  const sha256 = Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, "0")).join("");
  return withSemanticPostIdempotency("gif-upload", { sha256 }, (key) => {
    const body = new FormData(); body.append("file", file);
    return apiFetch<GifResult>("/images/gif/upload", semanticPostRequest(key, { body }));
  });
}
