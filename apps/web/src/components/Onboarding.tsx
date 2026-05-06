"use client";

import { motion } from "framer-motion";
import {
  Aperture,
  ImageDown,
  Keyboard,
  Layers3,
  Sparkles,
  Upload,
  Wand2,
} from "lucide-react";

import { cn } from "@/lib/utils";

type ComposerMode = "image" | "chat";

interface Preset {
  icon: React.ReactNode;
  title: string;
  hint: string;
  text: string;
  mode: ComposerMode;
}

const PRESETS: Preset[] = [
  {
    icon: <Aperture className="w-4 h-4" />,
    title: "电影感雨夜东京街角",
    hint: "文生图 · 16:9 · 夜景",
    text: "雨夜东京街角，霓虹倒影，35mm 胶片质感，浅景深，暖橙与青蓝色调，画面留出呼吸感",
    mode: "image",
  },
  {
    icon: <Layers3 className="w-4 h-4" />,
    title: "做 4 张产品海报",
    hint: "多图 · 可选 4/6/8 张",
    text: "黑色极简咖啡杯，暗色背景，柔和边缘光，留白充足，做成一组安静的产品海报",
    mode: "image",
  },
  {
    icon: <ImageDown className="w-4 h-4" />,
    title: "分析一张图片的构图",
    hint: "视觉理解 · 上传图片后发送",
    text: "看一下这张图的构图、光线和色彩，指出最值得调整的地方",
    mode: "chat",
  },
  {
    icon: <Wand2 className="w-4 h-4" />,
    title: "把草图整理成设定图",
    hint: "图生图 · 拖入参考图后使用",
    text: "保留主体轮廓和姿态，整理成干净的角色设定图，材质清楚，细节不过度堆满",
    mode: "image",
  },
];

export function Onboarding({
  onPick,
  loading = false,
}: {
  onPick: (text: string, mode: ComposerMode) => void;
  loading?: boolean;
}) {
  return (
    <motion.div
      className="relative flex flex-1 flex-col items-center px-3 pt-6 pb-[calc(2rem+env(safe-area-inset-bottom))] text-center sm:px-4 md:pt-12 md:pb-16"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: "easeOut" }}
    >
      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, delay: 0.08 }}
        className="mb-3 inline-flex items-center gap-2 rounded-full border border-[var(--border)] bg-white/[0.04] px-3 py-1 text-[11px] font-medium text-[var(--fg-1)] backdrop-blur-md"
      >
        <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" strokeWidth={2.2} />
        Lumen Studio
      </motion.p>

      <motion.h1
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.12 }}
        className="type-page-title max-w-[20rem] break-words text-balance sm:max-w-2xl md:text-[28px]"
      >
        从一句话开始，
        <span className="text-[var(--accent)]">慢慢把画面定下来。</span>
      </motion.h1>

      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.16 }}
        className="type-body mt-3 max-w-lg text-pretty px-1 text-[var(--fg-2)]"
      >
        选一个起点，或直接写下你想要的画面。
      </motion.p>

      <div className="mt-5 sm:mt-7 grid w-full max-w-4xl grid-cols-1 gap-2.5 sm:gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {PRESETS.map((preset, index) => (
          <motion.button
            key={preset.title}
            type="button"
            disabled={loading}
            aria-disabled={loading || undefined}
            onClick={() => {
              if (loading) return;
              onPick(preset.text, preset.mode);
            }}
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.35, delay: 0.18 + index * 0.04 }}
            whileHover={loading ? undefined : { y: -1 }}
            className={cn(
              "group relative min-w-0 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-4 text-left",
              "transition-[border-color,background-color,transform] duration-200 hover:border-[var(--border-amber)]/35 hover:bg-white/[0.04]",
              "cursor-pointer",
              "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60 active:scale-[0.99] disabled:cursor-wait disabled:opacity-60",
              loading && "pointer-events-none",
            )}
          >
            <div className="flex items-start gap-3">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-[var(--border)] bg-white/[0.04] text-[var(--fg-1)] transition-colors group-hover:text-[var(--accent)]">
                {preset.icon}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] font-medium leading-tight text-[var(--fg-0)]">
                  {loading ? "处理中…" : preset.title}
                </span>
                <span className="mt-1 block text-[11px] leading-snug text-[var(--fg-2)]">
                  {preset.hint}
                </span>
              </span>
            </div>
          </motion.button>
        ))}
      </div>

      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4, delay: 0.4 }}
        className="mt-6 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-[11px] text-[var(--fg-2)]"
      >
        <span className="inline-flex items-center gap-1.5">
          <Upload className="h-3 w-3" />
          拖拽或粘贴图片
        </span>
        <span className="inline-flex items-center gap-1.5">
          <Keyboard className="h-3 w-3" />
          <Kbd>⌘</Kbd>
          <Kbd>Enter</Kbd>
          <span className="ml-0.5">发送</span>
        </span>
      </motion.div>

      {loading && (
        <div
          role="status"
          aria-live="polite"
          className="absolute inset-0 flex items-center justify-center rounded-xl bg-[var(--bg-0)]/50 backdrop-blur-[2px]"
        >
          <span className="inline-flex items-center gap-2 rounded-full border border-[var(--border)] bg-white/5 px-3 py-1.5 text-sm text-[var(--fg-1)]">
            <Sparkles className="h-4 w-4 animate-spin text-[var(--accent)]" />
            处理中…
          </span>
        </div>
      )}
    </motion.div>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="rounded border border-[var(--border)] bg-white/5 px-1.5 py-0.5 font-mono text-[10px] text-[var(--fg-1)]">
      {children}
    </kbd>
  );
}
