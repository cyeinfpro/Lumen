"use client";

import { motion, useReducedMotion } from "framer-motion";
import { ArrowRight, ChevronDown } from "lucide-react";
import Image from "next/image";

import { Button } from "@/components/ui/primitives/Button";
import { DURATION, EASE } from "@/lib/motion";

type ComposerMode = "image" | "chat";

const PRESETS: readonly {
  title: string;
  label: string;
  text: string;
  mode: "image";
  previewSrc: string;
  previewAlt: string;
}[] = [
  {
    title: "电影级雨夜街角",
    label: "胶片街景",
    text: "雨夜东京街角，霓虹倒影，35mm 胶片质感，浅景深，暖橙与青蓝色调，画面留出呼吸感",
    mode: "image",
    previewSrc: "/inspiration/rainy-cinematic-street.webp",
    previewAlt: "雨幕中的霓虹街道、出租车与湿地倒影",
  },
  {
    title: "极简数码静物海报",
    label: "产品摄影",
    text: "黑色智能手机倚靠几何展台，暗调影棚背景，聚光形成克制的金属边缘高光，留出大面积高级画册排版空间",
    mode: "image",
    previewSrc: "/inspiration/minimal-product-still-life.webp",
    previewAlt: "暗调影棚中倚靠黑色几何展台的智能手机",
  },
  {
    title: "高端时尚肖像特写",
    label: "时尚人像",
    text: "高级时装模特特写，自然日光漫反射，柔和眼神光，细腻皮肤纹理，高级灰调背景",
    mode: "image",
    previewSrc: "/inspiration/editorial-fashion-portrait.webp",
    previewAlt: "青绿与陶土色摄影棚中的时尚模特肖像",
  },
  {
    title: "未来感建筑构图",
    label: "建筑构图",
    text: "现代玻璃与金属建筑，锐利悬挑几何体，低机位仰拍，大面积明亮天空留白，清晰结构线与冷静商业质感",
    mode: "image",
    previewSrc: "/inspiration/coastal-concept-architecture.webp",
    previewAlt: "明亮天空下锐利悬挑的玻璃金属现代建筑",
  },
];

const CHAT_STARTERS = [
  "帮我把这张照片调成胶片感",
  "分析这张图的构图和光影",
  "用克制一点的语言描述这张照片",
] as const;

/** Shared desktop/mobile starting point. A preset fills a draft; it never submits. */
export function Onboarding({
  onPick,
  loading = false,
}: {
  onPick: (text: string, mode: ComposerMode) => void;
  loading?: boolean;
}) {
  const reduceMotion = useReducedMotion();

  return (
    <motion.section
      data-studio-welcome
      aria-labelledby="studio-welcome-title"
      aria-busy={loading || undefined}
      className="mx-auto w-full max-w-[var(--content-composer)] py-6 md:py-10"
      initial={reduceMotion ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduceMotion ? 0 : DURATION.normal, ease: EASE.develop }}
    >
      <header className="mb-6 space-y-3 md:mb-8">
        <h1 id="studio-welcome-title" className="type-display-lg text-balance text-[var(--fg-0)]">
          今天想创作什么？
        </h1>
        <p className="max-w-lg text-pretty type-body text-[var(--fg-muted-aa)]">
          描述你的画面，或从一个灵感开始。
        </p>
        <Button
          variant="secondary"
          disabled={loading}
          rightIcon={<ArrowRight className="h-4 w-4" aria-hidden />}
          onClick={() => window.dispatchEvent(new CustomEvent("lumen:composer-expand"))}
        >
          开始创作
        </Button>
      </header>

      <section aria-labelledby="studio-inspiration-title">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h2 id="studio-inspiration-title" className="type-body-sm font-medium text-[var(--fg-0)]">试试这些灵感</h2>
          <p className="type-caption text-[var(--fg-muted-aa)]">填入提示词后可继续修改</p>
        </div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {PRESETS.map((preset, index) => (
            <button
              key={preset.title}
              type="button"
              data-studio-preset
              aria-label={`应用预设：${preset.title}`}
              disabled={loading}
              onClick={() => {
                if (loading) return;
                onPick(preset.text, preset.mode);
                window.dispatchEvent(new CustomEvent("lumen:composer-expand"));
              }}
              className="group flex min-h-11 min-w-0 flex-col overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] text-left transition-colors duration-[var(--dur-quick)] hover:border-[var(--border-strong)] focus-visible:outline-offset-4 disabled:cursor-wait disabled:opacity-50"
            >
              <span className="relative block aspect-[8/5] w-full overflow-hidden bg-[var(--bg-2)]">
                <Image
                  src={preset.previewSrc}
                  alt={preset.previewAlt}
                  fill
                  priority={index === 0}
                  sizes="(max-width: 639px) 44vw, (max-width: 1023px) 38vw, 210px"
                  className="object-cover"
                />
              </span>
              <span className="flex w-full min-w-0 items-center justify-between gap-2 px-3 py-2.5">
                <span className="type-body-sm font-medium text-[var(--fg-0)]">{preset.label}</span>
                <ArrowRight className="h-3.5 w-3.5 shrink-0 text-[var(--fg-muted-aa)]" aria-hidden />
              </span>
            </button>
          ))}
        </div>
      </section>

      <details className="group mt-4 border-t border-[var(--border-subtle)]">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 py-3 type-body-sm text-[var(--fg-1)] [&::-webkit-details-marker]:hidden">
          <span>也可以从对话开始</span>
          <ChevronDown className="h-4 w-4 transition-transform duration-[var(--dur-quick)] group-open:rotate-180 motion-reduce:transition-none" aria-hidden />
        </summary>
        <div className="grid gap-1 pb-2" role="group" aria-label="对话灵感">
          {CHAT_STARTERS.map((text) => (
            <Button key={text} variant="ghost" disabled={loading} onClick={() => {
              if (loading) return;
              onPick(text, "chat");
              window.dispatchEvent(new CustomEvent("lumen:composer-expand"));
            }} className="h-auto min-h-11 justify-start px-2 py-2 text-left">
              <span className="min-w-0 break-words type-body-sm">{text}</span>
            </Button>
          ))}
        </div>
      </details>
      {loading && <p role="status" className="mt-3 type-caption text-[var(--fg-muted-aa)]">加载中，完成后可继续创作。</p>}
    </motion.section>
  );
}
