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
  accent: string;
}

const PRESETS: Preset[] = [
  {
    icon: <Aperture className="w-4 h-4" />,
    title: "电影感雨夜东京街角",
    hint: "文生图 · 16:9 · 夜景",
    text: "雨夜东京街角，霓虹倒影，35mm 胶片质感，浅景深，暖橙与青蓝色调，画面留出呼吸感",
    mode: "image",
    accent: "from-cyan-300/20 to-[var(--accent)]/20",
  },
  {
    icon: <Layers3 className="w-4 h-4" />,
    title: "做 4 张产品海报",
    hint: "多图 · 可选 4/6/8 张",
    text: "黑色极简咖啡杯，暗色背景，柔和边缘光，留白充足，做成一组安静的产品海报",
    mode: "image",
    accent: "from-white/10 to-[var(--accent)]/20",
  },
  {
    icon: <ImageDown className="w-4 h-4" />,
    title: "分析一张图片的构图",
    hint: "视觉理解 · 上传图片后发送",
    text: "看一下这张图的构图、光线和色彩，指出最值得调整的地方",
    mode: "chat",
    accent: "from-emerald-300/15 to-white/10",
  },
  {
    icon: <Wand2 className="w-4 h-4" />,
    title: "把草图整理成设定图",
    hint: "图生图 · 拖入参考图后使用",
    text: "保留主体轮廓和姿态，整理成干净的角色设定图，材质清楚，细节不过度堆满",
    mode: "image",
    accent: "from-purple-300/15 to-[var(--accent)]/20",
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
      <div
        aria-hidden
        className="absolute top-0 h-48 w-48 rounded-full bg-[var(--accent)]/8 blur-3xl md:h-64 md:w-64"
      />

      <motion.div
        initial={{ opacity: 0, scale: 0.9 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.5, delay: 0.05, ease: "easeOut" }}
        className="relative mb-4 md:mb-5"
      >
        <div className="relative flex h-14 w-14 items-center justify-center rounded-[1.2rem] border border-white/15 bg-white/[0.06] shadow-[0_16px_48px_-14px_rgba(242,169,58,0.7)] backdrop-blur-xl md:h-16 md:w-16 md:rounded-[1.35rem]">
          <div className="absolute inset-1 rounded-[0.95rem] bg-gradient-to-br from-[var(--accent)] to-orange-200 md:rounded-[1.1rem]" />
          <Sparkles className="relative z-10 h-6 w-6 text-black/75 md:h-7 md:w-7" strokeWidth={2.2} />
        </div>
      </motion.div>

      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, delay: 0.08 }}
        className="mb-2 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-[11px] font-medium text-[var(--accent)] backdrop-blur-md"
      >
        Lumen Studio
      </motion.p>

      <motion.h1
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.12 }}
        className="max-w-[20rem] break-words text-balance text-xl font-semibold leading-[1.12] tracking-tight text-[var(--fg-0)] sm:max-w-2xl sm:text-2xl md:text-[clamp(2rem,5vw,2.75rem)]"
      >
        从一句话开始，
        <span className="text-[var(--accent)]">
          慢慢把画面定下来。
        </span>
      </motion.h1>

      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.16 }}
        className="mt-3 max-w-lg text-pretty px-1 text-[13px] leading-6 text-[var(--fg-1)] md:text-sm md:leading-7"
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
            whileHover={loading ? undefined : { y: -2 }}
            className={cn(
              "group relative min-w-0 overflow-hidden rounded-2xl border border-white/10 bg-white/[0.035] p-3.5 text-left",
              "shadow-[0_12px_40px_-20px_rgba(0,0,0,0.8)] backdrop-blur-md",
              "transition-[border-color,background-color,box-shadow] duration-200 hover:border-[var(--accent)]/35 hover:bg-white/[0.06]",
              "cursor-pointer",
              "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60 active:scale-[0.99] disabled:cursor-wait disabled:opacity-60",
              loading && "pointer-events-none",
            )}
          >
            <div
              aria-hidden
              className={cn(
                "absolute inset-0 bg-gradient-to-br opacity-0 transition-opacity duration-300 group-hover:opacity-100",
                preset.accent,
              )}
            />
            <div className="relative flex items-start gap-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-black/20 text-neutral-300 transition-colors group-hover:text-[var(--accent)]">
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
          <span className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-neutral-300">
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
    <kbd className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 font-mono text-[10px] text-neutral-300">
      {children}
    </kbd>
  );
}
