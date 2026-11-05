"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/primitives/Button";
import { uploadEditableGif } from "@/lib/api/gifUpload";
import { imageBinaryUrl } from "@/lib/apiClient";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { GifRecentList } from "./GifRecentList";
import { GIF_EDITOR_EVENT, type GifEditorRequest } from "./gifEditorEvents";

export function GifImportButton() {
  const input = useRef<HTMLInputElement>(null);
  const running = useRef(false);
  const alive = useRef(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const importFile = async (file: File | undefined) => {
    if (!file || running.current) return;
    const identity = getPrivateIdentitySnapshot();
    if (!identity.userId) return;
    running.current = true; setBusy(true); setError("");
    try {
      const result = await uploadEditableGif(file);
      if (!alive.current || !isPrivateIdentitySnapshotCurrent(identity)) return;
      window.dispatchEvent(new CustomEvent<GifEditorRequest>(GIF_EDITOR_EVENT, { detail: {
        item: { ...result, url: imageBinaryUrl(result.id), mime: "image/gif" }, identity,
      } }));
    } catch (reason) {
      if (alive.current && isPrivateIdentitySnapshotCurrent(identity)) setError(reason instanceof Error ? reason.message : "导入失败");
    } finally {
      running.current = false;
      if (alive.current) setBusy(false);
    }
  };
  return <div className="grid gap-2 border-t border-[var(--border-subtle)] pt-3">
    <Button variant="outline" size="sm" disabled={busy} onClick={() => input.current?.click()}>{busy ? "导入中" : "导入 GIF 编辑"}</Button>
    <input ref={input} type="file" accept="image/gif,.gif" aria-label="导入 GIF 编辑" className="sr-only" tabIndex={-1}
      onChange={(event) => { void importFile(event.target.files?.[0]); event.target.value = ""; }} />
    <p className="type-caption text-[var(--fg-2)]">已有 GIF 可本地调速、选帧替换。独立导入，不作为模型参考图；不调用生成模型。</p>
    <GifRecentList />
    {error ? <p role="alert" className="type-caption text-[var(--danger-fg)]">{error}</p> : null}
  </div>;
}
