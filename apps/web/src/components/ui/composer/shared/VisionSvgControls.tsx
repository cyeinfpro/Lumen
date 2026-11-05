"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/primitives";
import { createVisionSvgSnapshot, visionSvgSendError } from "@/store/chat/visionSvg";
import { useChatStore } from "@/store/useChatStore";

export function VisionSvgControls() {
  const composer = useChatStore((state) => state.composer);
  const sendMessage = useChatStore((state) => state.sendMessage);
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const snapshot = createVisionSvgSnapshot(composer);
  const unavailable = visionSvgSendError(snapshot, "vision_qa");

  async function submit() {
    if (inFlight.current) return;
    setError("");
    setNotice("");
    if (!snapshot.text.trim()) {
      setError("请先在提示词中填写 SVG 重建要求");
      return;
    }
    if (unavailable) { setError(unavailable); return; }
    inFlight.current = true;
    setBusy(true);
    try {
      await sendMessage({ composerSnapshot: snapshot, throwOnError: true });
      setNotice("已提交独立视觉 SVG 任务，关闭设置可查看结果。原生生图设置和参考图已保留。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交失败，请查看错误后再试");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <section aria-label="独立视觉 SVG 重建" className="grid gap-2 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-2)] p-3">
      <p className="type-body-sm font-medium">独立流程：视觉模型重建 SVG</p>
      <p className="type-caption text-[var(--fg-2)]">
        使用已配置的视觉聊天模型（不使用上方 SVG 作者 ID）读取一张静态参考图并写 SVG，不是 Image 2.6 原生图生 SVG，也不保证照片或复杂图形无损描摹。
        仅发送本次提示词和参考图，不使用历史、快速模型或工具。本次输出上限 16,000 tokens，按文本模型实际输入/输出 token 用量计费。SVG 校验或本地交付失败仍可能产生文本费用；不会自动重新调用模型。
      </p>
      {unavailable && <p className="type-caption text-[var(--fg-2)]">{unavailable}</p>}
      <Button type="button" variant="secondary" disabled={busy || Boolean(notice) || Boolean(unavailable)} onClick={() => void submit()}>
        {busy ? "正在提交独立任务…" : "用视觉模型重建 SVG（文本计费）"}
      </Button>
      {notice && <p role="status" className="type-caption text-[var(--fg-1)]">{notice}</p>}
      {error && <p role="alert" className="type-caption text-[var(--danger-fg)]">{error}</p>}
    </section>
  );
}
