import type { LightboxItem } from "./types";
import { getPrivateIdentitySnapshot, type PrivateIdentitySnapshot } from "@/lib/auth/privateIdentityEpoch";

export const GIF_EDITOR_EVENT = "lumen:edit-gif";
export interface GifEditorRequest { item: LightboxItem; identity: PrivateIdentitySnapshot; }
export function isGifItem(item: LightboxItem | null | undefined): item is LightboxItem {
  return !!item && [item.mime, item.mime_type, item.content_type, item.output_format]
    .some((value) => value === "image/gif" || value === "gif");
}
export function openGifEditor(item: LightboxItem, closeLightbox: () => void) {
  const identity = getPrivateIdentitySnapshot();
  if (!identity.userId) return;
  closeLightbox();
  window.dispatchEvent(new CustomEvent<GifEditorRequest>(GIF_EDITOR_EVENT, { detail: { item, identity } }));
}
