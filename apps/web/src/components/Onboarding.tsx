"use client";

import { motion } from "framer-motion";
import {
  Aperture,
  ArrowRight,
  ImageDown,
  Layers3,
  Sparkles,
  Wand2,
  type LucideIcon,
} from "lucide-react";
import Image from "next/image";

import { cn } from "@/lib/utils";

type ComposerMode = "image" | "chat";

interface Preset {
  tag: string;
  badge: string;
  title: string;
  hint: string;
  promptExcerpt: string;
  text: string;
  mode: ComposerMode;
  previewSrc: string;
  previewAlt: string;
  previewMeta: string;
  Icon: LucideIcon;
}

const PRESETS: Preset[] = [
  {
    tag: "16:9 · 胶片光影",
    badge: "35mm 胶片",
    title: "电影级雨夜街角",
    hint: "文生图 · 16:9 · 胶片光影",
    promptExcerpt: "雨夜东京街角，霓虹倒影，35mm 胶片质感，浅景深，暖橙与青蓝色调",
    text: "雨夜东京街角，霓虹倒影，35mm 胶片质感，浅景深，暖橙与青蓝色调，画面留出呼吸感",
    mode: "image",
    previewSrc: "/inspiration/rainy-cinematic-street.webp",
    previewAlt: "雨幕中的霓虹街道、出租车与湿地倒影",
    previewMeta: "16:9",
    Icon: Aperture,
  },
  {
    tag: "暗调商拍",
    badge: "产品摄影",
    title: "极简数码静物海报",
    hint: "产品摄影 · 商业级光影",
    promptExcerpt: "黑色智能手机倚靠几何展台，暗调影棚背景，聚光与金属边缘高光",
    text: "黑色智能手机倚靠几何展台，暗调影棚背景，聚光形成克制的金属边缘高光，留出大面积高级画册排版空间",
    mode: "image",
    previewSrc: "/inspiration/minimal-product-still-life.webp",
    previewAlt: "暗调影棚中倚靠黑色几何展台的智能手机",
    previewMeta: "PRODUCT",
    Icon: Layers3,
  },
  {
    tag: "85mm 人像",
    badge: "肖像摄影",
    title: "高端时尚肖像特写",
    hint: "肖像摄影 · 85mm 人像",
    promptExcerpt: "高级时装模特特写，自然日光漫反射，柔和眼神光，细腻皮肤纹理",
    text: "高级时装模特特写，自然日光漫反射，柔和眼神光，细腻皮肤纹理，高级灰调背景",
    mode: "image",
    previewSrc: "/inspiration/editorial-fashion-portrait.webp",
    previewAlt: "青绿与陶土色摄影棚中的时尚模特肖像",
    previewMeta: "85 MM",
    Icon: Wand2,
  },
  {
    tag: "建筑摄影 · 几何",
    badge: "建筑摄影",
    title: "未来感建筑构图",
    hint: "建筑摄影 · 宽画幅",
    promptExcerpt: "现代玻璃与金属建筑，锐利悬挑几何体，低机位仰拍，大面积明亮天空留白",
    text: "现代玻璃与金属建筑，锐利悬挑几何体，低机位仰拍，大面积明亮天空留白，清晰结构线与冷静商业质感",
    mode: "image",
    previewSrc: "/inspiration/coastal-concept-architecture.webp",
    previewAlt: "明亮天空下锐利悬挑的玻璃金属现代建筑",
    previewMeta: "ARCHITECTURE",
    Icon: ImageDown,
  },
];

function PresetThumbnail({ preset }: { preset: Preset }) {
  const Icon = preset.Icon;

  return (
    <div className="relative mb-3 aspect-[8/5] w-full overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)]">
      <Image
        src={preset.previewSrc}
        alt={preset.previewAlt}
        fill
        priority={preset === PRESETS[0]}
        sizes="(max-width: 639px) calc(100vw - 48px), (max-width: 1023px) 46vw, 240px"
        className="object-cover transition-transform duration-300 group-hover:scale-[1.02] motion-reduce:transform-none"
      />
      <div className="absolute inset-x-2.5 bottom-2.5 flex items-end justify-between gap-2">
        <span className="inline-flex min-w-0 items-center gap-1 rounded-full bg-[var(--media-control-bg)] px-2 py-1 type-caption text-[var(--media-control-fg)] shadow-[var(--shadow-1)] backdrop-blur-md">
          <Icon className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
          <span className="truncate">{preset.badge}</span>
        </span>
        <span className="shrink-0 rounded-full bg-[var(--media-control-bg)] px-2 py-1 type-caption tabular-nums text-[var(--media-control-fg)] shadow-[var(--shadow-1)] backdrop-blur-md">
          {preset.previewMeta}
        </span>
      </div>
    </div>
  );
}

export function Onboarding({
  onPick,
  loading = false,
}: {
  onPick: (text: string, mode: ComposerMode) => void;
  loading?: boolean;
}) {
  return (
    <motion.div
      className="relative flex min-h-[calc(100dvh-13rem)] w-full flex-col items-center justify-start overflow-y-auto overscroll-contain px-3 pb-[calc(2rem+env(safe-area-inset-bottom,0px))] pt-6 text-center sm:px-4 md:justify-center md:pb-16 md:pt-10"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: "easeOut" }}
    >
      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, delay: 0.08 }}
        className="mb-3 inline-flex items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-3 py-1 type-caption font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)] backdrop-blur-md"
      >
        <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" strokeWidth={2.2} />
        Lumen Studio
      </motion.p>

      <motion.h1
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.12 }}
        className="type-display-lg mx-auto max-w-[22rem] break-words text-balance sm:max-w-2xl text-[var(--fg-0)]"
      >
        探索画面的无限可能
      </motion.h1>

      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.16 }}
        className="type-body mx-auto mt-3 max-w-lg text-pretty px-1 text-[var(--fg-2)]"
      >
        选一个起点注入灵感，或直接在下方描述你想要的画面。
      </motion.p>

      <div className="mt-6 grid w-full max-w-5xl grid-cols-1 gap-3.5 sm:mt-8 sm:grid-cols-2 lg:grid-cols-4">
        {PRESETS.map((preset, index) => (
          <motion.button
            key={preset.title}
            type="button"
            disabled={loading}
            aria-disabled={loading || undefined}
            aria-label={`应用预设：${preset.title}，${preset.hint}`}
            onClick={() => {
              if (loading) return;
              onPick(preset.text, preset.mode);
            }}
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.35, delay: 0.18 + index * 0.04 }}
            className={cn(
              "group surface-card-v2 relative flex min-w-0 flex-col overflow-hidden p-3.5 text-left",
              "cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60 active:scale-[0.99] disabled:cursor-wait disabled:opacity-60",
              loading && "pointer-events-none",
            )}
          >
            <PresetThumbnail preset={preset} />

            <div className="flex min-w-0 flex-1 flex-col justify-between">
              <div>
                <span className="inline-flex items-center rounded-full bg-[var(--bg-2)] px-2 py-0.5 type-caption text-[var(--fg-2)]">
                  {preset.tag}
                </span>
                <span className="mt-2 block type-card-title text-[var(--fg-0)] transition-colors group-hover:text-[var(--accent)]">
                  {loading ? "处理中…" : preset.title}
                </span>
                <p className="mt-1.5 line-clamp-2 type-caption leading-relaxed text-[var(--fg-2)]">
                  {preset.promptExcerpt}
                </p>
              </div>

              <div className="mt-4 flex items-center justify-between border-t border-[var(--border-subtle)] pt-2.5">
                <span className="inline-flex items-center gap-1.5 type-caption font-medium text-[var(--accent)] transition-transform duration-200 group-hover:translate-x-0.5 motion-reduce:transform-none">
                  一键应用提示词
                  <ArrowRight className="h-3 w-3" strokeWidth={2.2} />
                </span>
              </div>
            </div>
          </motion.button>
        ))}
      </div>

      {loading && (
        <div
          role="status"
          aria-live="polite"
          className="absolute inset-0 flex items-center justify-center rounded-[var(--radius-panel)] bg-[var(--bg-0)]/50 backdrop-blur-[2px]"
        >
          <span className="inline-flex items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-1)]/88 px-3 py-1.5 type-body-sm text-[var(--fg-1)] shadow-[var(--shadow-2)]">
            <Sparkles className="h-4 w-4 animate-spin text-[var(--accent)]" />
            处理中…
          </span>
        </div>
      )}
    </motion.div>
  );
}
