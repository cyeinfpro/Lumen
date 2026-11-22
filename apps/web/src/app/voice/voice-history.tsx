"use client";

import { useState } from "react";
import { Button, ConfirmDialog, Input } from "@/components/ui/primitives";
import type { SpeechJob, VoiceProfile } from "./voice-api";
import { hasUnknownOutcome, privateAudioPath, stateLabel, yuan } from "./voice-display";

type VoiceWithPreviewState = VoiceProfile & {
  previewPending?: "pending" | "unknown";
  previewFailure?: "retrieval" | "other";
};

export function VoiceLibrary({ voices, onPreview, onSave, onRevoke, busy, canGenerate, previewPrice }: {
  voices: VoiceWithPreviewState[];
  canGenerate: boolean;
  previewPrice?: number | null;
  onPreview: (id: string) => Promise<boolean>;
  onSave: (id: string, name: string) => Promise<boolean>;
  onRevoke: (id: string) => Promise<boolean>;
  busy: string | null;
}) {
  const [target, setTarget] = useState<VoiceProfile | null>(null);
  return (
    <section aria-labelledby="voice-library-title" className="grid gap-3">
      <div>
        <h2 id="voice-library-title" className="type-section-title font-semibold">我的音色</h2>
        <p className="mt-1 type-body-sm text-[var(--fg-muted-aa)]">先试听，再保存到你的私有音色库。</p>
      </div>
      {!voices.length && <p className="type-body-sm text-[var(--fg-muted-aa)]">还没有音色。上传录音后会出现在这里。</p>}
      <ul className="grid gap-2">
        {voices.map((voice) => (
          <VoiceRow key={voice.id} voice={voice} busy={busy !== null} canGenerate={canGenerate} previewPrice={previewPrice} onPreview={onPreview} onSave={onSave} onSelectRevoke={setTarget} />
        ))}
      </ul>
      {target && (
        <ConfirmDialog
          open
          resetKey={target.id}
          onOpenChange={(open) => { if (!open) setTarget(null); }}
          tone="danger"
          title={`撤销「${target.name}」？`}
          description="撤销后不能再用这个音色生成或试听。已生成的成品仍保留，已经下载的副本无法召回。"
          confirmText="撤销音色"
          onConfirm={async () => {
            if (await onRevoke(target.id)) setTarget(null);
            else throw new Error("撤销未确认");
          }}
        />
      )}
    </section>
  );
}

function VoiceRow({ voice, busy, canGenerate, previewPrice, onPreview, onSave, onSelectRevoke }: {
  voice: VoiceWithPreviewState;
  busy: boolean;
  canGenerate: boolean;
  previewPrice?: number | null;
  onPreview: (id: string) => Promise<boolean>;
  onSave: (id: string, name: string) => Promise<boolean>;
  onSelectRevoke: (voice: VoiceProfile) => void;
}) {
  const [nameDraft, setNameDraft] = useState<string | null>(null);
  const preview = privateAudioPath(voice.preview_url);
  const ready = voice.state === "ready";
  const removable = !["revoked", "revoking", "deleted"].includes(voice.state);
  const name = nameDraft ?? voice.name;
  const allowSave = ready && !voice.saved && !!preview;
  return (
    <li className="grid min-w-0 gap-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <p className="min-w-0 break-all type-body-sm font-semibold">{voice.name}</p>
        <p className="type-body-sm text-[var(--fg-muted-aa)]">{voice.saved && ready ? "已保存" : stateLabel(voice.state)}</p>
      </div>
      {preview && removable && <audio aria-label={`${voice.name}试听`} controls className="w-full min-w-0" src={preview} preload="none" />}
      {hasUnknownOutcome(voice.state) && <p className="type-body-sm text-[var(--fg-muted-aa)]">上游可能已收到请求。请稍后刷新，不要重复提交。</p>}
      <PreviewFailureNotice voice={voice} hasPreview={!!preview} />
      {allowSave && (
        <label className="grid gap-1 type-body-sm">保存为
          <Input value={name} maxLength={80} onChange={(event) => setNameDraft(event.target.value)} disabled={busy} />
        </label>
      )}
      <VoiceRowControls
        ready={ready} allowSave={allowSave} hasPreview={!!preview} removable={removable}
        pending={voice.previewPending} busy={busy} canGenerate={canGenerate} price={previewPrice}
        name={name} onPreview={() => void onPreview(voice.id)}
        onSave={() => void onSave(voice.id, name.trim())} onRevoke={() => onSelectRevoke(voice)}
      />
    </li>
  );
}

function PreviewFailureNotice({ voice, hasPreview }: { voice: VoiceWithPreviewState; hasPreview: boolean }) {
  if (voice.state !== "ready" || hasPreview || voice.previewPending || !voice.previewFailure) return null;
  return (
    <p role="status" className="type-body-sm text-[var(--danger-fg)]">
      {voice.previewFailure === "retrieval"
        ? "语音已合成，但未能取回试听文件。重新生成可能再次计费。"
        : "上次试听未完成。"}
    </p>
  );
}

function VoiceRowControls({ ready, allowSave, hasPreview, removable, pending, busy, canGenerate, price, name, onPreview, onSave, onRevoke }: {
  ready: boolean;
  allowSave: boolean;
  hasPreview: boolean;
  removable: boolean;
  pending?: "pending" | "unknown";
  busy: boolean;
  canGenerate: boolean;
  price?: number | null;
  name: string;
  onPreview: () => void;
  onSave: () => void;
  onRevoke: () => void;
}) {
  const showPreview = ready && !hasPreview;
  const previewDisabled = busy || !canGenerate || pending !== undefined;
  return (
    <>
      {showPreview && !pending && <p className="type-body-sm text-[var(--fg-muted-aa)]">生成试听按计费字符收费，成功后可直接回听。</p>}
      <div className="flex flex-wrap gap-2">
        {showPreview && <Button size="sm" disabled={previewDisabled} onClick={onPreview}>{previewLabel(pending, price)}</Button>}
        {allowSave && <Button variant="primary" size="sm" disabled={busy || !name.trim()} onClick={onSave}>保存音色</Button>}
        {removable && <Button variant="ghost" size="sm" disabled={busy} onClick={onRevoke}>撤销</Button>}
      </div>
    </>
  );
}

function previewLabel(pending?: "pending" | "unknown", price?: number | null) {
  if (pending === "unknown") return "试听结果待确认";
  if (pending === "pending") return "试听处理中";
  return price == null ? "生成试听" : `生成试听 · ${yuan(price)}`;
}

export function SpeechHistory({ jobs, onRetry, busy }: {
  jobs: SpeechJob[];
  onRetry: (id: string, segment: number, revision?: number) => Promise<boolean>;
  busy: boolean;
}) {
  return (
    <section aria-labelledby="voice-jobs-title" className="grid gap-3">
      <div>
        <h2 id="voice-jobs-title" className="type-section-title font-semibold">配音任务</h2>
        <p className="mt-1 type-body-sm text-[var(--fg-muted-aa)]">离开页面不会中断。回来后可从这里查看。</p>
      </div>
      {!jobs.length && <p className="type-body-sm text-[var(--fg-muted-aa)]">生成的配音会出现在这里。</p>}
      <ul className="grid gap-2">
        {jobs.map((job) => <JobRow key={job.id} job={job} busy={busy} onRetry={onRetry} />)}
      </ul>
    </section>
  );
}

function JobRow({ job, busy, onRetry }: {
  job: SpeechJob;
  busy: boolean;
  onRetry: (id: string, segment: number, revision?: number) => Promise<boolean>;
}) {
  const url = privateAudioPath(job.download_url);
  const segments = job.segments ?? [];
  const mayRetry = ["failed", "ready", "completed", "succeeded"].includes(job.state);
  const [retryTarget, setRetryTarget] = useState<{ number: number; revision?: number; index: number } | null>(null);
  const done = segments.filter((segment) => ["completed", "succeeded", "ready"].includes(segment.state)).length;
  return (
    <li className="grid min-w-0 gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 type-body-sm">
        <p className="font-semibold">生成配音 <span className="text-[var(--fg-muted-aa)]">#{job.id.slice(0, 8)}</span></p>
        <p className="text-[var(--fg-muted-aa)]">{job.state === "ready" ? "已完成" : stateLabel(job.state)}{segments.length > 0 ? ` · ${done}/${segments.length} 段` : ""}</p>
      </div>
      {hasUnknownOutcome(job.state) && <p className="type-body-sm text-[var(--fg-muted-aa)]">结果和扣费可能尚未回传，请稍后刷新。</p>}
      {url && (
        <div className="grid gap-2">
          <audio aria-label="AI 合成音频" controls preload="none" src={url} className="w-full min-w-0" />
          <a className="w-fit text-[var(--link-fg)] underline" href={url} download={`Lumen-${job.id.slice(0, 8)}.wav`}>下载 AI 合成 WAV</a>
        </div>
      )}
      {segments.length > 0 && (
        <details>
          <summary className="min-h-11 cursor-pointer py-3 type-body-sm">查看分段</summary>
          <ol className="grid gap-1">
            {segments.map((segment, index) => (
              <li className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border-subtle)] py-1 type-body-sm" key={segment.segment_no}>
                <span>第 {index + 1} 段 · {segment.state === "ready" ? "已完成" : stateLabel(segment.state)}</span>
                {mayRetry && ["failed", "completed", "succeeded", "ready"].includes(segment.state) && (
                  <Button variant="secondary" size="sm" disabled={busy} onClick={() => setRetryTarget({ number: segment.segment_no, revision: segment.revision, index })}>只重做此段</Button>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
      {retryTarget && (
        <ConfirmDialog open
          onOpenChange={(open) => { if (!open) setRetryTarget(null); }}
          title={`重做第 ${retryTarget.index + 1} 段？`}
          description="只重新生成这段文字；此段按原任务单价重新计费。"
          confirmText="重做此段"
          onConfirm={async () => {
            if (await onRetry(job.id, retryTarget.number, retryTarget.revision)) setRetryTarget(null);
            else throw new Error("重做未确认");
          }}
        />
      )}
    </li>
  );
}
