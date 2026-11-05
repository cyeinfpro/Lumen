"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/primitives/Button";
import { apiFetch, imageBinaryUrl } from "@/lib/apiClient";
import type { GifResult } from "@/lib/api/gifEditing";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { CLOSE_EVENT } from "@/lib/lightbox/types";
import { GIF_EDITOR_EVENT, type GifEditorRequest } from "./gifEditorEvents";

export function GifRecentList() {
  const [items, setItems] = useState<GifResult[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const owner = useRef(getPrivateIdentitySnapshot());
  useEffect(() => {
    const clear = () => {
      if (!isPrivateIdentitySnapshotCurrent(owner.current)) {
        sequence.current += 1; setItems(null); setError(""); setBusy(false);
        owner.current = getPrivateIdentitySnapshot();
      }
    };
    window.addEventListener(CLOSE_EVENT, clear);
    return () => { sequence.current += 1; window.removeEventListener(CLOSE_EVENT, clear); };
  }, []);
  const load = async () => {
    const identity = getPrivateIdentitySnapshot();
    owner.current = identity;
    const current = ++sequence.current;
    setBusy(true); setError("");
    try {
      const value = await apiFetch<GifResult[]>("/images/gif/history");
      if (current === sequence.current && isPrivateIdentitySnapshotCurrent(identity)) setItems(value);
    } catch (reason) {
      if (current === sequence.current && isPrivateIdentitySnapshotCurrent(identity)) setError(reason instanceof Error ? reason.message : "读取失败");
    } finally { if (current === sequence.current) setBusy(false); }
  };
  return <div className="space-y-2">
    <Button variant="ghost" size="sm" disabled={busy} onClick={() => void load()}>{busy ? "加载中" : "最近 GIF"}</Button>
    {error ? <p role="alert" className="type-caption text-[var(--danger-fg)]">{error}</p> : null}
    {items ? <div className="max-h-48 space-y-1 overflow-y-auto" aria-label="最近编辑或导入的 GIF">
      {!items.length ? <p className="type-caption text-[var(--fg-2)]">暂无</p> : items.map((item, index) =>
        <Button key={item.id} variant="outline" size="sm" fullWidth onClick={() => {
          const identity = getPrivateIdentitySnapshot();
          if (!identity.userId || !isPrivateIdentitySnapshotCurrent(owner.current)) return;
          window.dispatchEvent(new CustomEvent<GifEditorRequest>(GIF_EDITOR_EVENT, { detail: {
            item: { ...item, mime: "image/gif", url: imageBinaryUrl(item.id) }, identity,
          } }));
        }}>GIF {index + 1} · {item.width} × {item.height}</Button>)}
    </div> : null}
  </div>;
}
