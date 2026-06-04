"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Clapperboard,
  Copy,
  Play,
  RefreshCw,
  Send,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react";

import {
  cancelVideoGeneration,
  createVideoGeneration,
  deleteVideo,
  getVideoGeneration,
  getVideoOptions,
  listVideoGenerations,
  retryVideoGeneration,
  uploadImage,
  videoBinaryUrl,
  videoPosterUrl,
} from "@/lib/apiClient";
import { useSSE } from "@/lib/useSSE";
import type { VideoAction, VideoGenerationOut, VideoOptionsOut } from "@/lib/types";
import { Button, Card, toast } from "@/components/ui/primitives";
import { DesktopTopNav, MobileTabBar } from "@/components/ui/shell";
import { formatRmb } from "@/lib/money";

type VideoGenerationWithVideo = VideoGenerationOut & {
  video: NonNullable<VideoGenerationOut["video"]>;
};

const VIDEO_EVENTS = [
  "video.queued",
  "video.submitted",
  "video.progress",
  "video.fetching",
  "video.succeeded",
  "video.failed",
  "video.canceled",
];

function firstModelForAction(options: VideoOptionsOut | undefined, action: VideoAction): string {
  return options?.models.find((item) => item.actions.includes(action))?.model ?? "";
}

function mergeById(
  current: VideoGenerationOut[],
  updates: VideoGenerationOut[],
): VideoGenerationOut[] {
  const map = new Map(current.map((item) => [item.id, item]));
  for (const item of updates) map.set(item.id, item);
  return Array.from(map.values()).sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
}

function estimateHoldMicro(
  options: VideoOptionsOut | undefined,
  {
    model,
    action,
    resolution,
    durationS,
  }: {
    model: string;
    action: VideoAction;
    resolution: string;
    durationS: number;
  },
): { tokens: number; micro: number } | null {
  const tokenMap = options?.hold_estimates?.[model];
  if (!tokenMap || typeof tokenMap !== "object") return null;
  const actionMap = (tokenMap as Record<string, unknown>)[action];
  if (!actionMap || typeof actionMap !== "object") return null;
  const tokensRaw = (actionMap as Record<string, unknown>)[`${resolution}:${durationS}`];
  const tokens = Number(tokensRaw);
  if (!Number.isFinite(tokens) || tokens <= 0) return null;
  const price = options?.pricing.find(
    (item) => item.model === model && item.action === action && item.enabled,
  );
  if (!price) return { tokens, micro: 0 };
  return { tokens, micro: Math.round((tokens * price.price.micro) / 1_000_000) };
}

function videoSrc(id: string): string {
  return videoBinaryUrl(id);
}

function posterSrc(id: string, posterUrl?: string | null): string | undefined {
  return posterUrl ? videoPosterUrl(id) : undefined;
}

function hasVideo(item: VideoGenerationOut): item is VideoGenerationWithVideo {
  return item.video != null;
}

export default function VideoPage() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [action, setAction] = useState<VideoAction>("t2v");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [durationS, setDurationS] = useState(5);
  const [resolution, setResolution] = useState("720p");
  const [aspectRatio, setAspectRatio] = useState("16:9");
  const [fps, setFps] = useState<number | "">("");
  const [generateAudio, setGenerateAudio] = useState(false);
  const [seed, setSeed] = useState("");
  const [inputImageId, setInputImageId] = useState("");
  const [uploadedLabel, setUploadedLabel] = useState("");
  const [items, setItems] = useState<VideoGenerationOut[]>([]);

  const optionsQ = useQuery({
    queryKey: ["video", "options"],
    queryFn: getVideoOptions,
    retry: false,
  });
  const historyQ = useQuery({
    queryKey: ["video", "generations"],
    queryFn: () => listVideoGenerations({ limit: 40 }),
    retry: false,
  });

  const options = optionsQ.data;
  const effectiveItems = useMemo(
    () => mergeById(historyQ.data?.items ?? [], items),
    [historyQ.data?.items, items],
  );
  const activeItems = useMemo(
    () =>
      effectiveItems.filter((item) =>
        ["queued", "submitting", "submitted", "running"].includes(item.status),
      ),
    [effectiveItems],
  );
  const recentVideoItems = useMemo(
    () => effectiveItems.filter(hasVideo).slice(0, 3),
    [effectiveItems],
  );
  const channels = useMemo(
    () => activeItems.map((item) => `task:${item.id}`),
    [activeItems],
  );

  const refreshGeneration = useCallback(
    async (id: string) => {
      const next = await getVideoGeneration(id);
      setItems((prev) => mergeById(prev, [next]));
      await qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    [qc],
  );

  const handlers = useMemo(
    () =>
      Object.fromEntries(
        VIDEO_EVENTS.map((eventName) => [
          eventName,
          (data: unknown) => {
            const id =
              typeof data === "object" && data !== null
                ? (data as { video_generation_id?: unknown }).video_generation_id
                : null;
            if (typeof id === "string" && id) void refreshGeneration(id);
          },
        ]),
      ),
    [refreshGeneration],
  );
  useSSE(channels, handlers);

  const availableModels = useMemo(
    () => options?.models.filter((item) => item.actions.includes(action)) ?? [],
    [action, options?.models],
  );
  const selectedModel = model || firstModelForAction(options, action);
  const estimate = estimateHoldMicro(options, {
    model: selectedModel,
    action,
    resolution,
    durationS,
  });

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadImage(file),
    onSuccess: (img) => {
      setInputImageId(img.id);
      setUploadedLabel(`${img.width}x${img.height}`);
      toast.success("首帧已上传");
    },
    onError: (err) => toast.error("上传失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const createMut = useMutation({
    mutationFn: () =>
      createVideoGeneration({
        action,
        model: selectedModel,
        prompt: prompt.trim(),
        input_image_id: action === "i2v" ? inputImageId.trim() : null,
        duration_s: durationS,
        resolution: resolution as "720p" | "1080p",
        aspect_ratio: aspectRatio,
        fps: fps === "" ? null : Number(fps),
        generate_audio: generateAudio,
        seed: seed.trim() ? Number(seed) : null,
        watermark: false,
      }),
    onSuccess: (gen) => {
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("视频任务已创建");
      void qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    onError: (err) => toast.error("创建失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const cancelMut = useMutation({
    mutationFn: cancelVideoGeneration,
    onSuccess: (gen) => {
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("已请求取消");
    },
    onError: (err) => toast.error("取消失败", { description: err instanceof Error ? err.message : undefined }),
  });
  const retryMut = useMutation({
    mutationFn: retryVideoGeneration,
    onSuccess: (gen) => {
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("已重新生成");
    },
    onError: (err) => toast.error("重试失败", { description: err instanceof Error ? err.message : undefined }),
  });
  const deleteMut = useMutation({
    mutationFn: deleteVideo,
    onSuccess: async (_data, videoId) => {
      setItems((prev) =>
        prev.map((item) =>
          item.video?.id === videoId ? { ...item, video: null } : item,
        ),
      );
      toast.success("视频已删除");
      await qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    onError: (err) => toast.error("删除失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const canSubmit =
    Boolean(options?.enabled) &&
    Boolean(selectedModel) &&
    prompt.trim().length > 0 &&
    (action === "t2v" || inputImageId.trim().length > 0) &&
    estimate !== null &&
    !createMut.isPending;

  return (
    <div className="min-h-screen bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="hidden md:block">
        <DesktopTopNav active="video" />
      </div>
      <main className="mx-auto grid w-full max-w-[1440px] gap-5 px-4 pb-24 pt-4 md:grid-cols-[minmax(420px,520px)_1fr] md:px-6 md:pb-10">
        <section className="space-y-4">
          <Card variant="subtle" padding="lg" className="space-y-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="type-card-title">视频创作</p>
                <p className="type-body-sm text-[var(--fg-2)]">
                  {options?.enabled ? "Seedance 任务队列" : options?.unavailable_reason ?? "video_disabled"}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void optionsQ.refetch()}
                leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
              >
                刷新
              </Button>
            </div>

            <div className="grid grid-cols-2 gap-2">
              {[
                ["t2v", "文字生成"],
                ["i2v", "首帧生成"],
              ].map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => {
                    const next = key as VideoAction;
                    setAction(next);
                    setModel(firstModelForAction(options, next));
                  }}
                  className={[
                    "h-10 rounded-[var(--radius-control)] border text-sm transition-colors",
                    action === key
                      ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
                      : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)] hover:text-[var(--fg-0)]",
                  ].join(" ")}
                >
                  {label}
                </button>
              ))}
            </div>

            <label className="space-y-1.5">
              <span className="type-caption text-[var(--fg-2)]">Prompt</span>
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                rows={7}
                maxLength={10000}
                className="w-full resize-none rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-sm outline-none focus:border-[var(--accent)]/50"
              />
            </label>

            {action === "i2v" && (
              <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
                <label className="space-y-1.5">
                  <span className="type-caption text-[var(--fg-2)]">首帧图片 ID</span>
                  <input
                    value={inputImageId}
                    onChange={(event) => {
                      setInputImageId(event.target.value);
                      setUploadedLabel("");
                    }}
                    className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
                  />
                </label>
                <div className="flex items-end">
                  <input
                    ref={fileRef}
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) uploadMut.mutate(file);
                      event.target.value = "";
                    }}
                  />
                  <Button
                    variant="outline"
                    size="md"
                    loading={uploadMut.isPending}
                    onClick={() => fileRef.current?.click()}
                    leftIcon={<Upload className="h-4 w-4" />}
                  >
                    上传
                  </Button>
                </div>
                {uploadedLabel && (
                  <p className="type-caption text-[var(--fg-2)] sm:col-span-2">
                    {uploadedLabel}
                  </p>
                )}
              </div>
            )}

            <div className="grid gap-3 sm:grid-cols-2">
              <SelectField
                label="模型"
                value={selectedModel}
                onChange={setModel}
                options={availableModels.map((item) => item.model)}
              />
              <SelectField
                label="时长"
                value={String(durationS)}
                onChange={(value) => setDurationS(Number(value))}
                options={(options?.durations_s ?? [5, 10]).map(String)}
              />
              <SelectField
                label="分辨率"
                value={resolution}
                onChange={setResolution}
                options={options?.resolutions ?? ["720p", "1080p"]}
              />
              <SelectField
                label="比例"
                value={aspectRatio}
                onChange={setAspectRatio}
                options={options?.aspect_ratios ?? ["16:9", "9:16", "1:1"]}
              />
              <SelectField
                label="FPS"
                value={fps === "" ? "" : String(fps)}
                onChange={(value) => setFps(value ? Number(value) : "")}
                options={["", ...(options?.fps ?? [24, 30]).map(String)]}
              />
              <label className="space-y-1.5">
                <span className="type-caption text-[var(--fg-2)]">Seed</span>
                <input
                  value={seed}
                  onChange={(event) => setSeed(event.target.value)}
                  inputMode="numeric"
                  className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
                />
              </label>
            </div>

            <label className="flex items-center justify-between rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-sm">
              <span>生成音频</span>
              <input
                type="checkbox"
                checked={generateAudio}
                onChange={(event) => setGenerateAudio(event.target.checked)}
              />
            </label>

            <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3">
              <div>
                <p className="type-caption text-[var(--fg-2)]">预扣</p>
                <p className="text-base font-semibold tabular-nums">
                  {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
                </p>
              </div>
              <div>
                <p className="type-caption text-[var(--fg-2)]">Token 上界</p>
                <p className="text-base font-semibold tabular-nums">
                  {estimate ? estimate.tokens.toLocaleString() : "-"}
                </p>
              </div>
              <Button
                variant="primary"
                size="md"
                disabled={!canSubmit}
                loading={createMut.isPending}
                onClick={() => createMut.mutate()}
                leftIcon={<Send className="h-4 w-4" />}
              >
                提交
              </Button>
            </div>
          </Card>
        </section>

        <section className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
            <Card variant="subtle" padding="lg" className="space-y-4">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Clapperboard className="h-4 w-4 text-[var(--fg-2)]" />
                  <p className="type-card-title">活跃任务</p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void historyQ.refetch()}
                  leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                >
                  刷新
                </Button>
              </div>
              <div className="space-y-3">
                {activeItems.length === 0 && (
                  <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-6 text-center text-sm text-[var(--fg-2)]">
                    暂无活跃任务
                  </div>
                )}
                {activeItems.map((item) => (
                  <TaskRow
                    key={item.id}
                    item={item}
                    onCancel={() => cancelMut.mutate(item.id)}
                    onRetry={() => retryMut.mutate(item.id)}
                    onCopy={() => {
                      void navigator.clipboard?.writeText(item.prompt);
                      toast.success("Prompt 已复制");
                    }}
                  />
                ))}
              </div>
            </Card>

            <Card variant="subtle" padding="lg" className="space-y-4">
              <p className="type-card-title">最近完成</p>
              <div className="space-y-3">
                {recentVideoItems.map((item) => (
                    <article key={item.id} className="space-y-2">
                      <video
                        controls
                        preload="metadata"
                        poster={posterSrc(item.video.id, item.video.poster_url)}
                        src={videoSrc(item.video.id)}
                        className="aspect-video w-full rounded-[var(--radius-card)] bg-[var(--bg-2)] object-contain"
                      />
                      <div className="flex items-center justify-between gap-2">
                        <p className="line-clamp-2 text-xs text-[var(--fg-2)]">{item.prompt}</p>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => item.video && deleteMut.mutate(item.video.id)}
                          leftIcon={<Trash2 className="h-3.5 w-3.5" />}
                        >
                          删除
                        </Button>
                      </div>
                    </article>
                  ))}
              </div>
            </Card>
          </div>

          <Card variant="subtle" padding="lg" className="space-y-4">
            <p className="type-card-title">历史</p>
            <div className="grid gap-3">
              {effectiveItems.map((item) => (
                <TaskRow
                  key={item.id}
                  item={item}
                  onCancel={() => cancelMut.mutate(item.id)}
                  onRetry={() => retryMut.mutate(item.id)}
                  onCopy={() => {
                    void navigator.clipboard?.writeText(item.prompt);
                    toast.success("Prompt 已复制");
                  }}
                  onDelete={() => item.video && deleteMut.mutate(item.video.id)}
                />
              ))}
              {!historyQ.isLoading && effectiveItems.length === 0 && (
                <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-8 text-center text-sm text-[var(--fg-2)]">
                  暂无历史
                </div>
              )}
            </div>
          </Card>
        </section>
      </main>
      <div className="md:hidden">
        <MobileTabBar />
      </div>
    </div>
  );
}

function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
}) {
  return (
    <label className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
      >
        {options.map((item) => (
          <option key={item || "auto"} value={item}>
            {item || "自动"}
          </option>
        ))}
      </select>
    </label>
  );
}

function TaskRow({
  item,
  onCancel,
  onRetry,
  onCopy,
  onDelete,
}: {
  item: VideoGenerationOut;
  onCancel: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onDelete?: () => void;
}) {
  const active = ["queued", "submitting", "submitted", "running"].includes(item.status);
  return (
    <article className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--fg-2)]">
            <span>{item.model}</span>
            <span>{item.action.toUpperCase()}</span>
            <span>{item.resolution}</span>
            <span>{item.duration_s}s</span>
          </div>
          <p className="mt-1 line-clamp-2 text-sm text-[var(--fg-0)]">{item.prompt}</p>
        </div>
        <StatusPill item={item} />
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div
          className="h-full rounded-full bg-[var(--accent)] transition-[width]"
          style={{ width: `${Math.max(0, Math.min(100, item.progress_pct))}%` }}
        />
      </div>
      {item.video && (
        <video
          controls
          preload="metadata"
          poster={posterSrc(item.video.id, item.video.poster_url)}
          src={videoSrc(item.video.id)}
          className="mt-3 aspect-video w-full rounded-[var(--radius-card)] bg-[var(--bg-2)] object-contain"
        />
      )}
      {item.error_message && (
        <p className="mt-2 text-xs text-[var(--danger-fg)]">{item.error_message}</p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {active && (
          <Button
            variant="outline"
            size="sm"
            onClick={onCancel}
            leftIcon={<XCircle className="h-3.5 w-3.5" />}
          >
            取消
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          onClick={onRetry}
          leftIcon={<Play className="h-3.5 w-3.5" />}
        >
          重新生成
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onCopy}
          leftIcon={<Copy className="h-3.5 w-3.5" />}
        >
          复制
        </Button>
        {onDelete && item.video && (
          <Button
            variant="outline"
            size="sm"
            onClick={onDelete}
            leftIcon={<Trash2 className="h-3.5 w-3.5" />}
          >
            删除
          </Button>
        )}
      </div>
    </article>
  );
}

function StatusPill({ item }: { item: VideoGenerationOut }) {
  const terminalOk = item.status === "succeeded";
  const terminalBad = ["failed", "canceled", "expired"].includes(item.status);
  return (
    <span
      className={[
        "rounded-full border px-2 py-1 text-xs",
        terminalOk
          ? "border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-fg)]"
          : terminalBad
          ? "border-[var(--danger-border)] bg-[var(--danger-bg)] text-[var(--danger-fg)]"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]",
      ].join(" ")}
    >
      {item.status} · {item.progress_stage}
    </span>
  );
}
