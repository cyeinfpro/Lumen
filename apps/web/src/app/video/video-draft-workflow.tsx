"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/primitives";
import type { VideoGenerationOut } from "@/lib/types";
import { officialDraftBlockedReason, draftWorkflowLabel, type VideoDraftMode } from "./video-draft-mode";
import { stageCopy } from "./video-task-model";

export function VideoDraftModeSelector({ mode = "direct", officialSupported = false, referenceSourceId, onChange, onClearSource }: {
  mode?: VideoDraftMode;
  officialSupported?: boolean;
  referenceSourceId?: string | null;
  onChange?: (mode: VideoDraftMode) => void;
  onClearSource?: () => void;
}) {
  if (!onChange) return null;
  return <section className="space-y-2 border-b border-[var(--border-subtle)] pb-3">
    <p className="type-caption text-[var(--fg-muted-aa)]">创作流程</p>
    <div className="flex flex-wrap gap-1.5" role="group" aria-label="视频草稿模式">
      {([["direct", "直接生成"], ["official", "草稿模式"], ["reference", "参考草稿模式"]] as const).map(([value, label]) =>
        <Button key={value} size="sm" variant={mode === value ? "primary" : "outline"}
          aria-pressed={mode === value} disabled={value === "official" && !officialSupported}
          onClick={() => onChange(value)}>{label}</Button>
      )}
    </div>
    <p className="type-caption leading-5 text-[var(--fg-muted-aa)]">
      {referenceSourceId ? "参考成片：原提示词与素材已带入，可切换成片模型。另行计费，画面不保证一致。"
        : mode === "official" ? "先生成 480p 草稿，预览后手动生成 1080p 成片。两步分别计费。"
        : mode === "reference" ? "先用所选模型生成参考草稿，再选成片模型参考重生成。两步分别计费，非官方转正。"
        : "直接按所选参数生成视频。"}
    </p>
    {!officialSupported && <p className="type-caption text-[var(--fg-muted-aa)]">官方草稿仅支持 Seedance 2.5 标准版，Flash 不支持。</p>}
    {referenceSourceId && <Button size="sm" variant="ghost" onClick={onClearSource}>退出成片</Button>}
  </section>;
}


function OfficialFinalAction({ item, blocked, finalizing, activeFinal, onFinalize }: {
  item: VideoGenerationOut;
  blocked: string | null;
  finalizing: boolean;
  activeFinal: boolean;
  onFinalize?: () => void;
}) {
  return <>
    <p className="type-caption leading-5 text-[var(--fg-muted-aa)]">沿用原提示词、素材和关键参数生成 1080p 成片，另行计费。</p>
    <p role="status" className="type-caption text-[var(--fg-muted-aa)]">{blocked ?? `有效至 ${new Date(item.draft_expires_at!).toLocaleString()}`}</p>
    <Button size="sm" variant="primary" disabled={Boolean(blocked) || finalizing || activeFinal || !onFinalize} loading={finalizing} onClick={onFinalize}>
      {activeFinal ? "成片生成中" : "生成1080p成片"}
    </Button>
  </>;
}
function SourceDraftLabel({ item }: { item: VideoGenerationOut }) {
  const sourceId = item.draft_generation_id || item.reference_generation_id;
  return sourceId ? <p className="type-caption text-[var(--fg-muted-aa)]">来源草稿：{sourceId.slice(0, 8)}</p> : null;
}

export function VideoDraftResultActions({ item, relatedItems = [], finalizing = false, onFinalize, onReferenceFinal, onRelatedPreview }: {
  item: VideoGenerationOut;
  relatedItems?: VideoGenerationOut[];
  finalizing?: boolean;
  onFinalize?: () => void;
  onReferenceFinal?: () => void;
  onRelatedPreview?: (item: VideoGenerationOut) => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const blocked = officialDraftBlockedReason(item, now);
  const activeFinal = relatedItems.some((task) => !["succeeded", "failed", "canceled", "expired"].includes(task.status));
  const ready = item.status === "succeeded" && Boolean(item.video);
  if (!item.draft && !item.reference_draft && !item.draft_generation_id && !item.reference_generation_id) return null;
  return <section className="mb-4 space-y-2 border-b border-[var(--border)] pb-4">
    <p className="type-body-sm font-medium text-[var(--fg-0)]">{draftWorkflowLabel(item)}</p>
    {item.draft && <OfficialFinalAction item={item} blocked={blocked} finalizing={finalizing} activeFinal={activeFinal} onFinalize={onFinalize} />}
    {item.reference_draft && <>
      <p className="type-caption leading-5 text-[var(--fg-muted-aa)]">带入原提示词和素材，追加本段视频。可改提示词、选择成片模型；重新生成另行计费，不保证画面一致。</p>
      <Button size="sm" variant="primary" disabled={!ready || !onReferenceFinal} onClick={onReferenceFinal}>选择成片模型</Button>
    </>}
    {relatedItems.map((task) => <div key={task.id} className="space-y-1 rounded-[var(--radius-control)] border border-[var(--border)] p-2">
      <p role="status" className="type-caption text-[var(--fg-1)]">{draftWorkflowLabel(task)} · {stageCopy(task).label} · {task.resolution}</p>
      {task.error_message && <p role="alert" className="type-caption text-[var(--danger-fg)]">{task.error_message}</p>}
      {task.video && <Button size="sm" variant="secondary" onClick={() => onRelatedPreview?.(task)}>查看成片</Button>}
      {["failed", "canceled", "expired"].includes(task.status) && <p className="type-caption text-[var(--fg-muted-aa)]">可回到上方重新生成成片，或在任务列表重试。</p>}
    </div>)}
    <SourceDraftLabel item={item} />
  </section>;
}
