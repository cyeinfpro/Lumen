"use client";

// DESIGN §12.6：比例 + 尺寸模式选择器（v2）。
// - 顶部"横构图 / 竖构图"segmented toggle（反转方向时 1:1 不变）
// - 5 对比例预设：1:1 / 3:2 / 4:3 / 16:9 / 21:9；选中 amber 描边 + 实心色块
// - Size mode：auto / fixed；底部提示 resolved 提交值
// - 4K 快捷预设：一键切到显式 fixed 3840x2160 / 2160x3840（更慢、更大）
//
// popover 用原生 <dialog>：浏览器 top-layer 一等公民，永远在最上面，
// 不受任何祖先 z-index / overflow / transform 影响。showModal() 自带
// backdrop + Esc 关闭。

import { motion } from "framer-motion";
import { useMemo, useRef, useState } from "react";
import { ChevronDown, Ratio, RotateCw, Sparkles } from "lucide-react";
import { useChatStore } from "@/store/useChatStore";
import {
  PRESET_4K_LANDSCAPE,
  PRESET_4K_PORTRAIT,
  resolveSize,
  validateExplicitSize,
} from "@/lib/sizing";
import type { AspectRatio, SizeMode } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAIRS: ReadonlyArray<{
  label: string;
  horizontal: AspectRatio;
  vertical: AspectRatio;
}> = [
  { label: "1:1", horizontal: "1:1", vertical: "1:1" },
  { label: "3:2", horizontal: "3:2", vertical: "2:3" },
  { label: "4:3", horizontal: "4:3", vertical: "3:4" },
  { label: "16:9", horizontal: "16:9", vertical: "9:16" },
  { label: "21:9", horizontal: "21:9", vertical: "9:21" },
];

type Orientation = "horizontal" | "vertical";

// 缩略矩形基础尺寸（横构图基准）
const SHAPE_H: Record<string, { w: number; h: number }> = {
  "1:1": { w: 26, h: 26 },
  "3:2": { w: 34, h: 23 },
  "4:3": { w: 32, h: 24 },
  "16:9": { w: 36, h: 20 },
  "21:9": { w: 38, h: 16 },
};

function inferOrientation(aspect: AspectRatio): Orientation {
  for (const p of PAIRS) {
    if (p.vertical === aspect && p.horizontal !== p.vertical) return "vertical";
  }
  return "horizontal";
}

export function AspectRatioPicker() {
  const params = useChatStore((s) => s.composer.params);
  const setAspectRatio = useChatStore((s) => s.setAspectRatio);
  const setSizeMode = useChatStore((s) => s.setSizeMode);
  const setFixedSize = useChatStore((s) => s.setFixedSize);

  const dialogRef = useRef<HTMLDialogElement>(null);
  const [isOpen, setIsOpen] = useState(false);

  const [orientation, setOrientation] = useState<Orientation>(() =>
    inferOrientation(params.aspect_ratio),
  );
  // 外部改了 aspect_ratio（刷新后载入历史等）→ 用 prev-check 在 render 阶段同步
  const [prevAspect, setPrevAspect] = useState(params.aspect_ratio);
  if (prevAspect !== params.aspect_ratio) {
    setPrevAspect(params.aspect_ratio);
    setOrientation(inferOrientation(params.aspect_ratio));
  }

  const openDialog = () => {
    const d = dialogRef.current;
    if (!d) return;
    if (d.open) {
      d.close();
    } else {
      try {
        d.showModal();
        setIsOpen(true);
      } catch {
        d.setAttribute("open", "");
        setIsOpen(true);
      }
    }
  };

  const closeDialog = () => {
    const d = dialogRef.current;
    if (!d) return;
    if (d.open) d.close();
    setIsOpen(false);
  };

  const resolved = useMemo(() => {
    try {
      return resolveSize({
        aspect: params.aspect_ratio,
        mode: params.size_mode,
        fixed: params.fixed_size,
      });
    } catch {
      return null;
    }
  }, [params.aspect_ratio, params.size_mode, params.fixed_size]);

  const is4KLandscape =
    params.size_mode === "fixed" && params.fixed_size === PRESET_4K_LANDSCAPE;
  const is4KPortrait =
    params.size_mode === "fixed" && params.fixed_size === PRESET_4K_PORTRAIT;

  const submitLabel = !resolved
    ? "提交：尺寸非法，请重新选择"
    : is4KLandscape
      ? "提交：4K 横图 3840×2160"
      : is4KPortrait
        ? "提交：4K 竖图 2160×3840"
        : resolved.size === "auto"
          ? "提交：auto（默认 4K 按比例分配 + 比例指令）"
          : `提交：${resolved.width}×${resolved.height}`;

  const selectAspect = (target: AspectRatio) => {
    setAspectRatio(target);
    if (params.fixed_size) setFixedSize(undefined);
  };

  const selectSizeMode = (m: SizeMode) => {
    setSizeMode(m);
    if (m === "auto" && params.fixed_size) setFixedSize(undefined);
  };

  const select4K = (orientation4K: Orientation) => {
    const targetAspect: AspectRatio =
      orientation4K === "horizontal" ? "16:9" : "9:16";
    const targetSize =
      orientation4K === "horizontal" ? PRESET_4K_LANDSCAPE : PRESET_4K_PORTRAIT;
    const [w, h] = targetSize.split("x").map((value) => Number.parseInt(value, 10));
    if (validateExplicitSize(w, h)) return;
    setAspectRatio(targetAspect);
    setSizeMode("fixed");
    setFixedSize(targetSize);
    setOrientation(orientation4K);
  };

  const toggleOrientation = (next: Orientation) => {
    if (next === orientation) return;
    const currentPair = PAIRS.find(
      (p) =>
        p.horizontal === params.aspect_ratio ||
        p.vertical === params.aspect_ratio,
    );
    setOrientation(next);
    if (currentPair) {
      const target =
        next === "horizontal" ? currentPair.horizontal : currentPair.vertical;
      if (target !== params.aspect_ratio) setAspectRatio(target);
    }
  };

  const flipOrientation = () => {
    toggleOrientation(orientation === "horizontal" ? "vertical" : "horizontal");
  };

  return (
    <>
      <button
        type="button"
        onClick={openDialog}
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        aria-label="选择宽高比与尺寸模式"
        className={cn(
          "inline-flex items-center gap-1.5 px-2.5 h-7 rounded-full",
          "text-xs font-medium text-neutral-300",
          "bg-white/5 hover:bg-white/10 border border-white/10",
          "hover:text-white active:scale-[0.96]",
          "transition-all duration-150",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
        )}
      >
        <Ratio className="h-3.5 w-3.5" aria-hidden />
        <span className="tabular-nums">{params.aspect_ratio}</span>
        <span
          className={cn(
            "text-[9px] uppercase tracking-wider px-1 py-0.5 rounded",
            params.size_mode === "auto"
              ? "bg-white/10 text-neutral-300"
              : "bg-[var(--color-lumen-amber)]/20 text-[var(--color-lumen-amber)]",
          )}
        >
          {params.size_mode}
        </span>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 transition-transform duration-200",
            isOpen && "rotate-180",
          )}
          aria-hidden
        />
      </button>

      <dialog
        ref={dialogRef}
        onClose={() => setIsOpen(false)}
        onClick={(e) => {
          // backdrop 点击关闭（事件 target 是 dialog 本身时）
          if (e.target === dialogRef.current) closeDialog();
        }}
        className={cn(
          "p-3 w-[min(22rem,calc(100vw-1.5rem))] max-h-[min(80vh,560px)] overflow-y-auto",
          "rounded-2xl bg-neutral-900/96 backdrop-blur-xl",
          "border border-white/12 shadow-2xl shadow-black/60",
          "text-neutral-100",
          "backdrop:bg-black/40 backdrop:backdrop-blur-[2px]",
        )}
        aria-label="宽高比 / 尺寸模式"
      >
        {/* 顶部：横/竖 toggle + 反转按钮 */}
        <div className="flex items-center gap-2 mb-3">
          <div
            className="flex-1 flex p-0.5 rounded-lg bg-white/5 border border-white/10"
            role="radiogroup"
            aria-label="构图方向"
          >
            {(
              [
                { id: "horizontal", label: "横构图" },
                { id: "vertical", label: "竖构图" },
              ] as const
            ).map((opt) => {
              const active = orientation === opt.id;
              return (
                <button
                  key={opt.id}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => toggleOrientation(opt.id)}
                  className={cn(
                    "flex-1 px-2 py-1 text-xs font-medium rounded-md",
                    "transition-colors duration-150",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
                    active
                      ? "bg-[var(--color-lumen-amber)] text-black shadow-[0_0_10px_rgba(242,169,58,0.35)]"
                      : "text-neutral-400 hover:text-neutral-100",
                  )}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
          <motion.button
            type="button"
            onClick={flipOrientation}
            aria-label="反转方向"
            title="反转横 / 竖"
            animate={{ rotate: orientation === "vertical" ? 180 : 0 }}
            transition={{ type: "spring", damping: 22, stiffness: 260 }}
            className={cn(
              "inline-flex items-center justify-center w-7 h-7 rounded-md",
              "bg-white/5 border border-white/10 text-neutral-300",
              "hover:bg-white/10 hover:text-white active:scale-[0.94]",
              "transition-colors duration-150",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
            )}
          >
            <RotateCw className="w-3.5 h-3.5" />
          </motion.button>
        </div>

        {/* 比例网格 */}
        <div className="grid grid-cols-5 gap-1.5">
          {PAIRS.map((pair) => {
            const target =
              orientation === "horizontal" ? pair.horizontal : pair.vertical;
            const active = params.aspect_ratio === target;
            const baseShape = SHAPE_H[pair.label];
            const shape =
              orientation === "vertical" && pair.label !== "1:1"
                ? { w: baseShape.h, h: baseShape.w }
                : baseShape;
            return (
              <button
                key={pair.label}
                type="button"
                onClick={() => selectAspect(target)}
                aria-label={`比例 ${pair.label}`}
                aria-pressed={active}
                className={cn(
                  "group flex flex-col items-center justify-center gap-1.5",
                  "h-16 rounded-lg border transition-all duration-150",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
                  "active:scale-[0.96]",
                  active
                    ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]/12 shadow-[0_0_12px_rgba(242,169,58,0.22)]"
                    : "border-white/10 bg-white/5 hover:bg-white/10 hover:border-white/20",
                )}
              >
                <motion.span
                  layout
                  style={{ width: shape.w, height: shape.h }}
                  className={cn(
                    "rounded-[3px] transition-colors",
                    active
                      ? "bg-[var(--color-lumen-amber)]"
                      : "bg-neutral-500 group-hover:bg-neutral-300",
                  )}
                />
                <span
                  className={cn(
                    "text-[10px] font-medium tabular-nums transition-colors",
                    active
                      ? "text-[var(--color-lumen-amber)]"
                      : "text-neutral-400 group-hover:text-neutral-200",
                  )}
                >
                  {pair.label}
                </span>
              </button>
            );
          })}
        </div>

        {/* Size mode */}
        <div className="mt-3 text-[11px] uppercase tracking-wider text-neutral-500 mb-2">
          Size mode
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          {(["auto", "fixed"] as SizeMode[]).map((m) => {
            const active = params.size_mode === m;
            return (
              <button
                key={m}
                type="button"
                onClick={() => selectSizeMode(m)}
                aria-pressed={active}
                className={cn(
                  "px-2 py-1.5 rounded-lg text-xs font-medium border",
                  "transition-all duration-150 active:scale-[0.97]",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
                  active
                    ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
                    : "border-white/10 bg-white/5 text-neutral-300 hover:bg-white/10",
                )}
              >
                {m === "auto" ? "自动（根据参考图）" : "固定"}
              </button>
            );
          })}
        </div>

        {/* 4K 快捷预设 */}
        <div className="mt-3 text-[11px] uppercase tracking-wider text-neutral-500 mb-2 flex items-center gap-1.5">
          <Sparkles
            className="w-3 h-3 text-[var(--color-lumen-amber)]/80"
            aria-hidden
          />
          4K 预设
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          <button
            type="button"
            onClick={() => select4K("horizontal")}
            aria-pressed={is4KLandscape}
            className={cn(
              "px-2 py-1.5 rounded-lg text-xs font-medium border",
              "transition-all duration-150 active:scale-[0.97]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
              is4KLandscape
                ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
                : "border-white/10 bg-white/5 text-neutral-300 hover:bg-white/10",
            )}
          >
            4K 横 3840×2160
          </button>
          <button
            type="button"
            onClick={() => select4K("vertical")}
            aria-pressed={is4KPortrait}
            className={cn(
              "px-2 py-1.5 rounded-lg text-xs font-medium border",
              "transition-all duration-150 active:scale-[0.97]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
              is4KPortrait
                ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
                : "border-white/10 bg-white/5 text-neutral-300 hover:bg-white/10",
            )}
          >
            4K 竖 2160×3840
          </button>
        </div>
        <p className="mt-1.5 text-[10px] text-neutral-500 leading-snug">
          默认 preset 已按 4K 级别自动分配；这两个按钮直接下发 3840×2160 /
          2160×3840。
        </p>

        <div
          className={cn(
            "mt-3 pt-2.5 border-t border-white/10",
            "text-[11px] leading-snug",
            resolved ? "text-neutral-400" : "text-red-300",
          )}
        >
          {submitLabel}
        </div>
      </dialog>
    </>
  );
}
