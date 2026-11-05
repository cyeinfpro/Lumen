"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/primitives";
import { imageBinaryUrl } from "@/lib/api/images";
import { triggerImageDownload } from "@/components/ui/lightbox/utils";
import { retryVisionSvgArtifact } from "@/lib/api/visionSvg";
import type { AssistantMessage } from "@/lib/types";
import { useChatStore } from "@/store/useChatStore";

const LABELS = {
  pending: "SVG 本地交付尚未完成",
  ready: "SVG 已交付",
  invalid: "SVG 未通过安全或格式校验，未交付媒体",
  processing_failed: "模型输出已完成，SVG 本地处理失败",
} as const;

function canRepairArtifact(msg: AssistantMessage): boolean {
  return Boolean(msg.completion_id && msg.status === "succeeded" &&
    (msg.artifact_status === "processing_failed" || msg.artifact_status === "pending"));
}

function artifactLabel(msg: AssistantMessage): string {
  if (msg.status === "pending" || msg.status === "streaming") return "视觉模型重建 SVG 进行中";
  if (msg.status === "failed") return "视觉模型请求失败，未交付 SVG";
  if (msg.status === "canceled") return "任务已取消";
  return msg.artifact_status ? LABELS[msg.artifact_status] : "SVG 交付状态未知，请刷新结果";
}

function VerifiedSvgDownload({ imageId, onError }: { imageId: string; onError: (message: string) => void }) {
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  async function download() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    onError("");
    try {
      await triggerImageDownload(imageBinaryUrl(encodeURIComponent(imageId)), `vision-rebuilt-${imageId}.svg`);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : "下载失败，请重试");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  return <Button type="button" variant="secondary" disabled={busy} onClick={() => void download()}>
    {busy ? "准备 SVG 下载…" : "下载已验证 SVG"}
  </Button>;
}

export function VisionSvgArtifact({ msg }: { msg: AssistantMessage }) {
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const canRepair = canRepairArtifact(msg);
  const source = msg.artifact_source_text;
  const sourceLimit = 100_000;

  async function refresh() {
    const state = useChatStore.getState();
    if (msg.completion_id) await state.refreshCompletionText(msg.completion_id);
    const current = useChatStore.getState();
    if (current.currentConvId && current.currentConvId === state.currentConvId) {
      await current.loadHistoricalMessages(current.currentConvId);
    }
    // A fresh read may still report the same failure, so permit another local attempt.
    setNotice("");
  }

  async function repair() {
    if (!msg.completion_id || !canRepair || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await retryVisionSvgArtifact(msg.completion_id);
      setNotice("已提交本地处理，等待结果更新；不会重新调用模型或再次计费。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "本地处理请求失败");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  const label = artifactLabel(msg);
  return (
    <section aria-label="视觉 SVG 结果" className="grid min-w-0 max-w-full grid-cols-1 gap-2 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-[var(--fg-0)]">
      <p role="status" className="type-body-sm font-medium">视觉模型重建 SVG · {label}</p>
      <p className="type-caption text-[var(--fg-2)]">这是独立视觉聊天流程，不是 Image 2.6 原生编辑。已产生的文本 token 仍按配置计费；校验失败不代表免费，不会自动重发模型。</p>
      {msg.artifact_error && <p className="break-words type-caption text-[var(--danger-fg)]">{msg.artifact_error}</p>}
      <div className="flex flex-wrap gap-2">
        {msg.artifact_status === "ready" && msg.artifact_image_id && <VerifiedSvgDownload imageId={msg.artifact_image_id} onError={setError} />}
        {canRepair && <Button type="button" variant="secondary" disabled={busy || Boolean(notice)} onClick={() => void repair()}>
          {busy ? "提交本地处理…" : "仅重试本地交付（不重新生成）"}
        </Button>}
        <Button type="button" variant="ghost" disabled={busy} onClick={() => void refresh().catch(() => setError("刷新失败，请稍后重试"))}>刷新结果</Button>
      </div>
      {notice && canRepair && <p role="status" className="type-caption text-[var(--fg-1)]">{notice}</p>}
      {error && <p role="alert" className="type-caption text-[var(--danger-fg)]">{error}</p>}
      {source && <details>
        <summary className="cursor-pointer type-caption text-[var(--fg-1)]">查看模型原始文本（仅文本，不执行）</summary>
        <pre className="mt-2 min-w-0 max-w-full max-h-64 overflow-auto whitespace-pre-wrap break-words [overflow-wrap:anywhere] rounded-[var(--radius-control)] bg-[var(--bg-2)] p-2 type-caption">{source.slice(0, sourceLimit)}</pre>
        {source.length > sourceLimit && <p className="type-caption text-[var(--fg-2)]">仅显示前 {sourceLimit} 个字符。</p>}
      </details>}
    </section>
  );
}
