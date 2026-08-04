"use client";

// DevelopingCard：queued/running 状态的显影卡；失败态带重试。
// 扫光用 globals.css 的 .lumen-developing 类；失败时红底。

import { useEffect, useState, useSyncExternalStore } from "react";
import { RotateCcw, X } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import type { Generation } from "@/lib/types";
import { aspectRatioToCss } from "@/lib/sizing";
import { cn } from "@/lib/utils";

// prefers-reduced-motion 降级：保留静态状态提示，不再移动扫光。
function ReducedMotionBar() {
  return (
    <div
      aria-hidden
      className="absolute inset-y-0 left-0 w-[3px] bg-accent shadow-[var(--shadow-amber)]"
    />
  );
}

interface DevelopingCardProps {
  gen: Generation;
  onRetry: (genId: string) => void;
  onCancel?: (genId: string) => void;
}

const STAGE_COPY: Record<Generation["stage"], string> = {
  queued: "排队中",
  understanding: "打光中…",
  rendering: "细化中…",
  finalizing: "收尾中",
};

function getReducedMotionSnapshot() {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function subscribeReducedMotion(onStoreChange: () => void) {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
  const onChange = () => onStoreChange();
  mq.addEventListener?.("change", onChange);
  return () => mq.removeEventListener?.("change", onChange);
}

// "16:9" 基准分辨率 → 展示尾行 3840 × 2160 等（粗略映射，显示用）
function sizeLabel(ratio: string, sizeRequested: string): string {
  if (sizeRequested && sizeRequested !== "auto") {
    return sizeRequested.replace(/(\d)\s*x\s*(\d)/gi, "$1 × $2");
  }
  switch (ratio) {
    case "16:9":
      return "3840 × 2160";
    case "9:16":
      return "2160 × 3840";
    case "10:7":
      return "3424 × 2400";
    case "7:10":
      return "2400 × 3424";
    case "1:1":
      return "2048 × 2048";
    case "4:5":
      return "2048 × 2560";
    case "3:4":
      return "1920 × 2560";
    case "21:9":
      return "3360 × 1440";
    default:
      return "auto";
  }
}

export function DevelopingCard({
  gen,
  onRetry,
  onCancel,
}: DevelopingCardProps) {
  const failed = gen.status === "failed";
  const isDeveloping = gen.status === "running" || gen.status === "queued";
  const startedAt =
    typeof gen.started_at === "number" && gen.started_at > 0
      ? gen.started_at
      : null;
  const isQueued = gen.status === "queued";
  const ratioCss = aspectRatioToCss(gen.aspect_ratio);
  const size = sizeLabel(gen.aspect_ratio, gen.size_requested);
  const stageText = STAGE_COPY[gen.stage] ?? "生成中...";

  const prefersReduced = useSyncExternalStore(
    subscribeReducedMotion,
    getReducedMotionSnapshot,
    () => false,
  );

  // 粗略"已 Ns"：后端 started_at 是权威时间，后续用 performance.now delta 推进。
  const [elapsedLabel, setElapsedLabel] = useState<string>("生成中...");
  useEffect(() => {
    if (isQueued || !isDeveloping || !startedAt) return;
    let raf = 0;
    const initialElapsedMs = Math.max(0, Date.now() - startedAt);
    const perfAnchor = performance.now();
    let lastCommit = Number.NEGATIVE_INFINITY;
    const commit = (elapsedMs: number) => {
      const s = Math.floor(elapsedMs / 1000);
      setElapsedLabel(s > 0 ? `已 ${s}s` : "生成中...");
    };
    const tick = (t: number) => {
      if (t - lastCommit >= 250) {
        lastCommit = t;
        commit(initialElapsedMs + Math.max(0, t - perfAnchor));
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [isDeveloping, isQueued, startedAt]);

  if (failed) {
    return (
      <div className="flex flex-col gap-1.5">
        <div
          className={cn(
            "relative w-full overflow-hidden max-h-[280px]",
            "rounded-[var(--radius-card)] border border-danger-border",
            "bg-danger-soft flex flex-col items-center justify-center gap-2.5 p-5",
          )}
          style={{ aspectRatio: ratioCss }}
        >
          <p className="type-label text-danger">
            生成失败
          </p>
          {(gen.diagnostics?.safe_error_summary || gen.error_message) && (
            <p className="type-caption text-[var(--fg-1)] text-center max-w-[90%] break-words [overflow-wrap:anywhere]">
              {gen.diagnostics?.safe_error_summary ?? gen.error_message}
            </p>
          )}
          <Button
            size="sm"
            variant="secondary"
            onClick={() => onRetry(gen.id)}
            className="min-h-11 px-4"
            aria-label="重试生成"
            leftIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}
          >
            重试
          </Button>
        </div>
        <p
          className="px-1 type-caption tabular-nums text-[var(--fg-2)]"
          style={{ fontFamily: "var(--font-mono)" }}
        >
          {gen.aspect_ratio} · {size}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div
        className={cn(
          "relative w-full overflow-hidden max-h-[280px]",
          "rounded-[var(--radius-card)] border border-[var(--border-subtle)]",
          "bg-[var(--bg-1)]",
        )}
        style={{ aspectRatio: ratioCss }}
        aria-live="polite"
        aria-label={stageText}
      >
        {prefersReduced ? (
          <ReducedMotionBar />
        ) : (
          <div className="absolute inset-0 lumen-developing" aria-hidden />
        )}
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <span
            className="type-label text-[var(--fg-0)] pointer-events-none"
            style={{ fontFamily: "var(--font-zh-display)" }}
          >
            {stageText}
          </span>
          {onCancel && (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => onCancel(gen.id)}
              className="min-h-11 bg-[var(--bg-1)]/70 px-3.5 text-[var(--fg-1)] backdrop-blur-sm"
              aria-label="取消生成"
              leftIcon={<X className="h-3.5 w-3.5" aria-hidden />}
            >
              取消
            </Button>
          )}
        </div>
      </div>
      <p
        className="mt-0.5 px-1 type-caption tabular-nums text-[var(--fg-2)]"
        style={{ fontFamily: "var(--font-mono)" }}
      >
        {gen.aspect_ratio} · {size} ·{" "}
        {isQueued ? "排队中" : startedAt ? elapsedLabel : "生成中..."}
      </p>
    </div>
  );
}
