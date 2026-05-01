"use client";

// 生成卡视图：根据 Generation.status 分别呈现 queued/running/succeeded/failed/canceled。
// 进度环用 SVG circle + stroke-dashoffset，基于 stage 映射 0-100%。
// 取消按钮独立组件，调用 cancelTask；失败态带 shake 动画。

import { motion } from "framer-motion";
import { useRef, useState } from "react";
import { RotateCw, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { cancelTask, imageBinaryUrl } from "@/lib/apiClient";
import { errorCodeToMessage } from "@/lib/errors";
import { getErrorMessage, hasErrorMessage } from "@/lib/errorMessages";
import { PremiumImageCard } from "../PremiumImageCard";
import { ErrorState } from "@/components/ui/primitives";
import { IntentBadge } from "./IntentBadge";
import { StageTicker } from "./StageTicker";
import type { Generation, Intent } from "@/lib/types";

// 1x1 透明 PNG 占位符（DESIGN §14.2）
const PLACEHOLDER_PIXEL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

// stage → 粗粒度进度（0-100），用于进度环
const STAGE_PROGRESS: Record<Generation["stage"], number> = {
  queued: 8,
  understanding: 30,
  rendering: 70,
  finalizing: 92,
};

function getFallbackImageId(gen: Generation): string | null {
  const imageId = (gen as Generation & { image_id?: unknown }).image_id;
  return typeof imageId === "string" && imageId.trim() ? imageId : null;
}

interface GenerationViewProps {
  gen: Generation;
  currentIntent: Exclude<Intent, "auto">;
  canSwitchIntent: boolean;
  onEditImage: (imageId: string) => void;
  onRetry: (gen: Generation) => void;
  onRegenerate: (newIntent: Exclude<Intent, "auto">) => Promise<void>;
  compact?: boolean;
  ordinal?: number;
}

export function GenerationView({
  gen,
  currentIntent,
  canSwitchIntent,
  onEditImage,
  onRetry,
  onRegenerate,
  compact = false,
  ordinal,
}: GenerationViewProps) {
  const isCompletionToolImage = gen.id.startsWith("completion-tool-");
  const canSwitchThisIntent = canSwitchIntent && !isCompletionToolImage;
  const failureMessage =
    gen.error_code && hasErrorMessage(gen.error_code)
      ? getErrorMessage(gen.error_code)
      : gen.error_code
        ? (errorCodeToMessage(gen.error_code) ?? gen.error_message ?? "未知错误")
        : (gen.error_message ?? "未知错误");

  if (gen.status === "queued" || gen.status === "running") {
    return (
      <div className="flex flex-col gap-2.5 relative">
        <div className="relative">
          <PremiumImageCard
            id={gen.id}
            src={PLACEHOLDER_PIXEL}
            alt={gen.prompt}
            isStreaming
            className="aspect-[4/3] w-full"
          />
          {/* 左上：取消 */}
          <CancelButton genId={gen.id} className="absolute top-2 left-2" />
          {/* 右上：意图切换（禁用） */}
          <IntentBadge
            currentIntent={currentIntent}
            disabled
            onSwitch={onRegenerate}
            className="absolute top-2 right-2"
          />
          {/* 中央进度环 */}
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <ProgressRing pct={STAGE_PROGRESS[gen.stage] ?? 10} />
          </div>
          {ordinal && <OrdinalBadge value={ordinal} />}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-1">
          <p className="text-sm text-neutral-400 truncate flex-1 min-w-0 basis-[10rem]">
            {gen.prompt}
          </p>
          <StageTicker gen={gen} />
        </div>
      </div>
    );
  }

  if (gen.status === "failed") {
    return (
      <motion.div
        initial={{ x: 0 }}
        animate={{ x: [0, -4, 4, -3, 3, 0] }}
        transition={{ duration: 0.45, ease: "easeInOut" }}
        className="flex flex-col gap-2.5 relative"
      >
        <div
          className={cn(
            "aspect-[4/3] w-full rounded-2xl border-2",
            "border-[var(--danger,#E5484D)]/40 bg-[var(--danger,#E5484D)]/5",
            "flex flex-col items-center justify-center gap-3 p-6",
          )}
        >
          <p className="text-sm font-medium text-red-300">生成失败</p>
          {/* MED #11：移动端不要 text-xs，不然误触屏小屏几乎看不清 */}
          <p className="text-sm md:text-xs text-neutral-400 text-center max-w-full md:max-w-md leading-relaxed break-words [overflow-wrap:anywhere]">
            {failureMessage}
          </p>
          <button
            type="button"
            onClick={() => onRetry(gen)}
            className={cn(
              "inline-flex items-center gap-1.5 px-4 py-1.5 rounded-full",
              "bg-white/8 hover:bg-white/12 border border-white/15 text-sm text-neutral-100",
              "active:scale-[0.97] transition-all duration-150",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
            )}
          >
            <RotateCw className="w-3.5 h-3.5" />
            重试
          </button>
        </div>
        <IntentBadge
          currentIntent={currentIntent}
          disabled={!canSwitchThisIntent}
          onSwitch={onRegenerate}
          className="absolute top-2 right-2"
        />
        {ordinal && <OrdinalBadge value={ordinal} className="top-2 left-2" />}
        <p className="text-sm text-neutral-500 px-1 truncate">{gen.prompt}</p>
      </motion.div>
    );
  }

  if (gen.status === "succeeded" && gen.image) {
    const img = gen.image;
    const elapsed =
      gen.finished_at && gen.started_at
        ? Math.max(0, Math.round((gen.finished_at - gen.started_at) / 100) / 10)
        : null;
    return (
      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ type: "spring", damping: 28, stiffness: 340 }}
        className="flex flex-col gap-2.5 relative"
      >
        <PremiumImageCard
          id={img.id}
          src={img.data_url}
          previewSrc={img.preview_url ?? img.thumb_url}
          lightboxPreviewSrc={img.display_url ?? img.preview_url ?? img.thumb_url}
          alt={gen.prompt}
          compact={compact}
          className={cn(
            "w-full",
            compact
              ? "max-h-[42vh]"
              : img.height && img.width && img.height > img.width
              ? "max-h-[70vh]"
              : "max-h-[50vh]",
          )}
          style={
            img.width && img.height
              ? { aspectRatio: `${img.width}/${img.height}` }
              : { aspectRatio: "4/3" }
          }
          onEdit={() => onEditImage(img.id)}
        />
        <IntentBadge
          currentIntent={currentIntent}
          disabled={!canSwitchThisIntent}
          onSwitch={onRegenerate}
          className="absolute top-2 right-2"
        />
        {ordinal && <OrdinalBadge value={ordinal} className="top-2 left-2" />}
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-1">
          <p className={cn("truncate flex-1 min-w-0 basis-[10rem]", compact ? "text-xs text-neutral-400" : "text-sm text-neutral-300")}>
            {gen.prompt}
          </p>
          <div className="flex items-center gap-2 shrink-0 text-xs font-mono text-neutral-500 tabular-nums">
            {elapsed != null && <span>{elapsed}s</span>}
            {elapsed != null && <span className="text-neutral-700">·</span>}
            <span className="break-all">{img.size_actual}</span>
          </div>
        </div>
      </motion.div>
    );
  }

  if (gen.status === "canceled") {
    return (
      <div className="flex flex-col gap-2 px-1 opacity-70">
        <div
          className={cn(
            "aspect-[4/3] w-full rounded-2xl",
            "border border-dashed border-white/15 bg-white/[0.02]",
            "flex flex-col items-center justify-center gap-2",
          )}
        >
          <span className="px-2 py-0.5 text-[11px] uppercase tracking-wider rounded-md bg-neutral-700/60 text-neutral-300 border border-white/5">
            已取消
          </span>
          <p className="text-xs text-neutral-500">本次生成被取消</p>
        </div>
        <p className="text-sm text-neutral-500 truncate">{gen.prompt}</p>
      </div>
    );
  }

  // succeeded 但 image 缺失 —— 只有后端明确给出 image_id 时才走图片兜底。
  const fallbackImageId = getFallbackImageId(gen);
  if (!fallbackImageId) {
    return (
      <div className="flex flex-col gap-2.5 relative">
        <ErrorState
          title="图片结果不可用"
          description="生成已完成，但没有返回可加载的图片 ID。"
          detail={gen.id}
          className="aspect-[4/3] w-full"
        />
        <IntentBadge
          currentIntent={currentIntent}
          disabled={!canSwitchThisIntent}
          onSwitch={onRegenerate}
          className="absolute top-2 right-2"
        />
        {ordinal && <OrdinalBadge value={ordinal} className="top-2 left-2" />}
        <p className="text-sm text-neutral-400 px-1 truncate">{gen.prompt}</p>
      </div>
    );
  }

  const fallbackSrc = imageBinaryUrl(fallbackImageId);
  return (
    <div className="flex flex-col gap-2.5 relative">
      <PremiumImageCard
        id={fallbackImageId}
        src={fallbackSrc}
        alt={gen.prompt}
        className={cn(
          "w-full",
          compact ? "max-h-[42vh]" : "max-h-[50vh]",
        )}
        style={{ aspectRatio: "4/3" }}
      />
      <IntentBadge
        currentIntent={currentIntent}
        disabled={!canSwitchThisIntent}
        onSwitch={onRegenerate}
        className="absolute top-2 right-2"
      />
      {ordinal && <OrdinalBadge value={ordinal} className="top-2 left-2" />}
      <p className="text-sm text-neutral-400 px-1 truncate">{gen.prompt}</p>
    </div>
  );
}

export default GenerationView;

function OrdinalBadge({ value, className }: { value: number; className?: string }) {
  return (
    <span
      className={cn(
        "pointer-events-none absolute top-2 left-2 z-10 inline-flex h-6 min-w-6 items-center justify-center rounded-full",
        "border border-white/15 bg-black/55 px-2 text-[10px] font-mono text-white/85 backdrop-blur-md",
        className,
      )}
      aria-hidden
    >
      {value}
    </span>
  );
}

// ——————————————————— 进度环 ———————————————————
function ProgressRing({ pct }: { pct: number }) {
  const size = 56;
  const stroke = 3.5;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(100, pct));
  const offset = c * (1 - clamped / 100);

  return (
    <motion.svg
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className="drop-shadow-[0_0_10px_rgba(242,169,58,0.4)]"
      aria-hidden
    >
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="rgba(255,255,255,0.1)"
        strokeWidth={stroke}
      />
      <motion.circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="var(--color-lumen-amber)"
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={c}
        initial={false}
        animate={{ strokeDashoffset: offset }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
    </motion.svg>
  );
}

// ——————————————————— 取消按钮 ———————————————————
function CancelButton({
  genId,
  className,
}: {
  genId: string;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 同步 ref guard:在 setState 落库之前就拒绝重复点击,防止 React 批处理窗口内连点。
  const inFlightRef = useRef(false);

  const handle = async () => {
    if (inFlightRef.current || busy) return;
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await cancelTask("generations", genId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "取消失败");
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  };

  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => void handle()}
        disabled={busy}
        aria-label="取消本次生成"
        title="取消本次生成"
        className={cn(
          "inline-flex items-center gap-1 px-3 py-1.5 rounded-full",
          "text-[10px] font-medium border backdrop-blur-md",
          "border-white/15 bg-black/60 text-neutral-200",
          "hover:border-red-400/60 hover:text-red-200",
          "active:scale-[0.97] transition-all duration-150",
          "disabled:opacity-60 disabled:cursor-wait",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400/50",
        )}
      >
        <X className="w-3 h-3" aria-hidden />
        {busy ? "取消中" : "取消"}
      </button>
      {error && (
        <p className="absolute mt-1 text-[10px] text-red-300 max-w-[200px]">
          {error}
        </p>
      )}
    </div>
  );
}
