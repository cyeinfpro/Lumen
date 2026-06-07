"use client";

import { motion } from "framer-motion";
import {
  Aperture,
  ArrowRight,
  ImageDown,
  Layers3,
  Sparkles,
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
      className="relative flex min-h-[calc(100dvh-13rem)] w-full flex-col items-center justify-center px-3 pb-[calc(2rem+env(safe-area-inset-bottom))] pt-6 text-center sm:px-4 md:pb-16 md:pt-10"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: "easeOut" }}
    >
      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, delay: 0.08 }}
        className="mb-3 inline-flex items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-3 py-1 text-[11px] font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)] backdrop-blur-md"
      >
        <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" strokeWidth={2.2} />
        Lumen 工作室
      </motion.p>

      <motion.h1
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.12 }}
        className="type-page-title mx-auto max-w-[22rem] break-words text-balance sm:max-w-2xl md:text-[30px]"
      >
        从一句话开始，
        <span className="text-[var(--accent)]">慢慢把画面定下来。</span>
      </motion.h1>

      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.16 }}
        className="type-body mx-auto mt-3 max-w-lg text-pretty px-1 text-[var(--fg-2)]"
      >
        选一个起点，或直接写下你想要的画面。
      </motion.p>

      <div className="mt-5 grid w-full max-w-4xl grid-cols-1 gap-2.5 sm:mt-7 sm:grid-cols-2 sm:gap-3 lg:grid-cols-4">
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
              "group relative min-w-0 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/86 p-4 text-left shadow-[var(--shadow-1)] backdrop-blur-sm",
              "transition-[border-color,background-color,transform,box-shadow] duration-200 hover:border-[var(--border-amber)]/35 hover:bg-[var(--bg-2)]/72 hover:shadow-[var(--shadow-2)]",
              "cursor-pointer",
              "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60 active:scale-[0.99] disabled:cursor-wait disabled:opacity-60",
              loading && "pointer-events-none",
            )}
          >
            <div className="flex items-start gap-3 lg:block">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-1)] transition-colors group-hover:text-[var(--accent)]">
                {preset.icon}
              </span>
              <span className="min-w-0 flex-1 lg:mt-3 lg:block">
                <span className="block text-[13px] font-medium leading-tight text-[var(--fg-0)]">
                  {loading ? "处理中…" : preset.title}
                </span>
                <span className="mt-1 block text-[11px] leading-snug text-[var(--fg-2)]">
                  {preset.hint}
                </span>
              </span>
            </div>
            <ArrowRight
              className="absolute bottom-3 right-3 h-3.5 w-3.5 text-[var(--fg-3)] opacity-0 transition-[opacity,transform,color] duration-200 group-hover:translate-x-0.5 group-hover:text-[var(--accent)] group-hover:opacity-100"
              strokeWidth={2.2}
            />
          </motion.button>
        ))}
      </div>

      {loading && (
        <div
          role="status"
          aria-live="polite"
          className="absolute inset-0 flex items-center justify-center rounded-[var(--radius-panel)] bg-[var(--bg-0)]/50 backdrop-blur-[2px]"
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
