"use client";

import { createPortal } from "react-dom";
import { imageBinaryUrl } from "@/lib/apiClient";
import { CLOSE_EVENT } from "@/lib/lightbox/types";
import { useEffect, useState } from "react";
import { isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { GifEditorDialog } from "./GifEditorDialog";
import { GIF_EDITOR_EVENT, type GifEditorRequest } from "./gifEditorEvents";

export function GifEditorHost() {
  const [request, setRequest] = useState<GifEditorRequest | null>(null);
  useEffect(() => {
    const open = (event: Event) => {
      const detail = (event as CustomEvent<GifEditorRequest>).detail;
      if (detail?.item?.id && detail.identity && isPrivateIdentitySnapshotCurrent(detail.identity)) setRequest(detail);
    };
    const close = () => setRequest(null);
    const identityClose = () => setRequest((current) => current && isPrivateIdentitySnapshotCurrent(current.identity) ? current : null);
    window.addEventListener(GIF_EDITOR_EVENT, open);
    window.addEventListener("popstate", close);
    window.addEventListener(CLOSE_EVENT, identityClose);
    return () => {
      window.removeEventListener(GIF_EDITOR_EVENT, open);
      window.removeEventListener("popstate", close);
      window.removeEventListener(CLOSE_EVENT, identityClose);
    };
  }, []);
  return request ? createPortal(<GifEditorDialog key={request.item.id} request={request} onClose={() => setRequest(null)}
    onContinue={(result) => setRequest({ ...request, item: { ...result, mime: "image/gif", url: imageBinaryUrl(result.id) } })} />, document.body) : null;
}
