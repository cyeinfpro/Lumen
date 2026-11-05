"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Button } from "@/components/ui/primitives";
import { imageBinaryUrl, uploadImage } from "@/lib/apiClient";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { useChatStore } from "@/store/useChatStore";

import { GifImportButton } from "../../lightbox/GifImportButton";
import { LazyMaskCanvas } from "../LazyMaskCanvas";
import type { MaskExport } from "../../inpaint/MaskBoard";

export function Image26Controls() {
  const params = useChatStore((state) => state.composer.params);
  const setComplexity = useChatStore((state) => state.setImageComplexity);
  const setModel = useChatStore((state) => state.setImageModel);
  return (
    <section className="grid gap-2">
      <span className="type-caption text-[var(--fg-2)]">图像复杂度 · 默认高</span>
      <div className="flex flex-wrap gap-2" role="group" aria-label="图像复杂度">
        {([{ value: "low", label: "低" }, { value: "medium", label: "中" }, { value: "high", label: "高" }] as const).map((option) => (
          <Button key={option.value} variant="outline" size="sm"
            aria-pressed={(params.complexity ?? "high") === option.value}
            onClick={() => setComplexity(option.value)}>
            {option.label}
          </Button>
        ))}
        <Button variant="ghost" size="sm" onClick={() => setComplexity(undefined)}>恢复默认</Button>
      </div>
      <p className="type-caption text-[var(--fg-2)]">独立控制画面复杂度，不改变聊天推理强度。</p>
      <Button variant="outline" size="sm" onClick={() => { setModel("gpt-image-2.6-flare"); setComplexity("low"); }}>
        快速预设
      </Button>
      <p className="type-caption text-[var(--fg-2)]">Flare + 低复杂度，实际耗时取决于尺寸与服务负载。</p>
      <MarkupControl />
      <GifImportButton />
    </section>
  );
}

function MarkupControl() {
  const composer = useChatStore((state) => state.composer);
  const setMarkup = useChatStore((state) => state.setMarkup);
  const clearMarkup = useChatStore((state) => state.clearMarkup);
  const [target, setTarget] = useState<{ id: string; src: string; identity: ReturnType<typeof getPrivateIdentitySnapshot>; conversation: string | null } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const upload = useRef<AbortController | null>(null);
  useEffect(() => () => { upload.current?.abort(); }, []);
  const first = composer.attachments[0];
  const unavailable = !first || composer.params.output_format === "svg" || composer.params.output_format === "gif";
  const active = Boolean(composer.markup && composer.markup.target_attachment_id === first?.id);
  const isCurrent = () => {
    const state = useChatStore.getState();
    return target !== null && isPrivateIdentitySnapshotCurrent(target.identity) &&
      state.currentConvId === target.conversation && state.composer.attachments[0]?.id === target.id;
  };
  const confirm = async (layer: MaskExport) => {
    if (!target || upload.current) return;
    if (!isCurrent()) throw new Error("参考图或会话已变化，重新打开标注");
    if (layer.width * layer.height > 16_777_216 || layer.blob.size > 50 * 1024 * 1024) {
      throw new Error("标注图片超限");
    }
    const controller = new AbortController();
    upload.current = controller;
    setSubmitting(true);
    try {
      // Upload the native-sized transparent PNG without photo compression.
      const result = await uploadImage(new File([layer.blob], "markup.png", { type: "image/png" }), { signal: controller.signal, purpose: "image_markup" });
      if (controller.signal.aborted || !isCurrent()) return;
      setMarkup({ image_id: result.id, preview_data_url: layer.preview_data_url, target_attachment_id: target.id });
      setError(null);
      setTarget(null);
    } catch (failure) {
      if (!controller.signal.aborted) throw failure;
    } finally {
      if (upload.current === controller) { upload.current = null; setSubmitting(false); }
    }
  };
  return (
    <div className="grid gap-2 border-t border-[var(--border-subtle)] pt-3">
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" disabled={unavailable}
          onClick={() => {
            if (!first) return;
            setError(null);
            setTarget({ id: first.id, src: imageBinaryUrl(first.source_image_id ?? first.id), identity: getPrivateIdentitySnapshot(), conversation: useChatStore.getState().currentConvId });
          }}>
          {active ? "重画标注" : "添加标注"}
        </Button>
        {active ? <Button variant="ghost" size="sm" onClick={clearMarkup}>清除标注</Button> : null}
      </div>
      <p className="type-caption text-[var(--fg-2)]">
        {active ? "已设置独立标注层；发送时应用于第一张参考图。" : "标注第一张参考图，其他图片仍作为参考；与局部修改蒙版互斥。"}
      </p>
      {unavailable ? <p className="type-caption text-[var(--fg-2)]">标注需要参考图及 PNG、JPEG 或 WebP 输出。</p> : null}
      {error ? <p role="alert" className="type-caption text-[var(--danger-fg)]">{error}</p> : null}
      {target && typeof document !== "undefined" ? createPortal(
        <LazyMaskCanvas open imageSrc={target.src} exportMode="markup" submitting={submitting}
          onClose={() => { if (!submitting) setTarget(null); }}
          onConfirm={confirm} />, document.body,
      ) : null}
    </div>
  );
}
