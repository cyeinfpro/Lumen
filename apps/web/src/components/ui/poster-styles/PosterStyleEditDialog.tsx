"use client";

// 编辑用户海报风格条目（user:* 才能编辑；preset 走删除＝隐藏）。

import { motion } from "framer-motion";
import { X } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import { toast } from "@/components/ui/primitives/Toast";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import {
  POSTER_STYLE_ASPECT_OPTIONS,
  POSTER_STYLE_CATEGORY_LABEL,
  POSTER_STYLE_CATEGORY_OPTIONS,
  type PosterStyleCategory,
  type PosterStyleItem,
  type PosterStylePatchIn,
} from "@/lib/apiClient";
import { usePatchPosterStyleMutation } from "@/lib/queries";
import { cn } from "@/lib/utils";

export interface PosterStyleEditDialogProps {
  item: PosterStyleItem;
  onClose: () => void;
}

export function PosterStyleEditDialog({
  item,
  onClose,
}: PosterStyleEditDialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  const [title, setTitle] = useState(item.title);
  const [category, setCategory] = useState<PosterStyleCategory>(item.category);
  const [mood, setMood] = useState(item.mood ?? "");
  const [promptTemplate, setPromptTemplate] = useState(
    item.prompt_template ?? "",
  );
  const [palette, setPalette] = useState(item.palette.join(", "));
  const [styleTags, setStyleTags] = useState(item.style_tags.join("、"));
  const [recommendedAspects, setRecommendedAspects] = useState<string[]>(
    item.recommended_aspects,
  );

  const patch = usePatchPosterStyleMutation({
    onSuccess: () => {
      toast.success("已更新风格");
      onClose();
    },
    onError: (err) =>
      toast.error("更新失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  // ESC + body lock
  useBodyScrollLock(true);
  const onDialogKeyDown = useModalLayer({
    open: true,
    rootRef: dialogRef,
    onClose,
  });

  const trimmedTitle = title.trim();
  const canSubmit = useMemo(() => {
    if (!trimmedTitle) return false;
    return true;
  }, [trimmedTitle]);

  const submit = () => {
    if (!canSubmit) {
      toast.warning("请填写名称");
      return;
    }
    const body: PosterStylePatchIn = {
      title: trimmedTitle,
      category,
      mood: mood.trim() || null,
      prompt_template: promptTemplate.trim() || null,
      palette: parseHexList(palette),
      style_tags: splitTags(styleTags),
      recommended_aspects: recommendedAspects,
    };
    patch.mutate({ id: item.id, body });
  };

  const toggleAspect = (value: string) => {
    setRecommendedAspects((prev) =>
      prev.includes(value)
        ? prev.filter((v) => v !== value)
        : [...prev, value].slice(0, 8),
    );
  };

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-md md:items-center md:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 16, scale: 0.98 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-panel flex w-full flex-col overflow-hidden border border-[var(--border)] bg-[var(--bg-0)] md:max-h-[92dvh] md:max-w-2xl"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="type-page-kicker">编辑风格</p>
            <h2 id={titleId} className="type-page-title mt-2 md:text-[26px]">
              编辑风格
            </h2>
            <p id={descriptionId} className="type-page-subtitle mt-2 max-w-md">
              调整名称、分类和生成提示，保存后会更新当前风格。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-11 w-11 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-9 md:w-9"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="mobile-dialog-scroll grid min-h-0 flex-1 gap-5 overflow-y-auto overscroll-contain px-5 py-5 md:grid-cols-2">
          <UnderlineLabeled label="名称" wrapperClass="md:col-span-2">
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              maxLength={120}
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
          </UnderlineLabeled>
          <UnderlineLabeled label="类目">
            <select
              value={category}
              onChange={(event) =>
                setCategory(event.target.value as PosterStyleCategory)
              }
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            >
              {POSTER_STYLE_CATEGORY_OPTIONS.map((value) => (
                <option key={value} value={value} className="bg-[var(--bg-0)]">
                  {POSTER_STYLE_CATEGORY_LABEL[value]}
                </option>
              ))}
            </select>
          </UnderlineLabeled>
          <UnderlineLabeled label="情绪 / mood">
            <input
              value={mood}
              onChange={(event) => setMood(event.target.value)}
              maxLength={120}
              placeholder="温暖、冷峻、奇幻"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
          </UnderlineLabeled>
          <UnderlineLabeled label="Prompt 模板" wrapperClass="md:col-span-2">
            <textarea
              value={promptTemplate}
              onChange={(event) => setPromptTemplate(event.target.value)}
              maxLength={2000}
              rows={5}
              placeholder="海报构图、用色、字体方向"
              className="w-full resize-none border-b border-[var(--border)] bg-transparent px-1 py-1.5 text-[14px] leading-[1.5] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:text-[13px]"
            />
          </UnderlineLabeled>
          <UnderlineLabeled label="色板（逗号分隔 hex）" wrapperClass="md:col-span-2">
            <input
              value={palette}
              onChange={(event) => setPalette(event.target.value)}
              placeholder="#F2A93A, #2A2A2A"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
            <PaletteSwatchRow hexes={parseHexList(palette)} />
          </UnderlineLabeled>
          <UnderlineLabeled label="风格标签" wrapperClass="md:col-span-2">
            <input
              value={styleTags}
              onChange={(event) => setStyleTags(event.target.value)}
              placeholder="极简、复古、低饱和"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
          </UnderlineLabeled>
          <UnderlineLabeled label="推荐尺寸" wrapperClass="md:col-span-2">
            <div className="flex flex-wrap gap-x-3 gap-y-1 pt-1">
              {POSTER_STYLE_ASPECT_OPTIONS.map((value) => (
                <Chip
                  key={value}
                  active={recommendedAspects.includes(value)}
                  onClick={() => toggleAspect(value)}
                >
                  {value}
                </Chip>
              ))}
            </div>
          </UnderlineLabeled>
        </div>

        <footer className="mobile-dialog-footer grid shrink-0 grid-cols-2 gap-2 border-t border-[var(--border)] px-5 py-4 md:flex md:items-center md:justify-end">
          <Button
            variant="outline"
            onClick={onClose}
            disabled={patch.isPending}
            className="w-full md:w-auto"
          >
            取消
          </Button>
          <Button
            variant="primary"
            loading={patch.isPending}
            onClick={submit}
            className="w-full md:w-auto"
          >
            保存
          </Button>
        </footer>
      </motion.div>
    </div>
  );
}

function UnderlineLabeled({
  label,
  children,
  wrapperClass,
}: {
  label: string;
  children: React.ReactNode;
  wrapperClass?: string;
}) {
  return (
    <label className={cn("grid gap-2", wrapperClass)}>
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </span>
      {children}
    </label>
  );
}

function PaletteSwatchRow({ hexes }: { hexes: string[] }) {
  if (hexes.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {hexes.map((hex, idx) => (
        <span
          key={`${hex}-${idx}`}
          aria-hidden
          title={hex}
          className="h-5 w-5 rounded-[var(--radius-card)] border border-[var(--border)]"
          style={{ backgroundColor: hex }}
        />
      ))}
    </div>
  );
}

function Chip({
  children,
  active,
  onClick,
}: {
  children: React.ReactNode;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group relative inline-flex min-h-11 cursor-pointer items-center px-1 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-8",
        active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
      )}
    >
      <span>{children}</span>
      <span
        aria-hidden
        className={cn(
          "absolute inset-x-1 -bottom-px h-px transition-colors duration-[var(--dur-base)]",
          active
            ? "bg-[var(--amber-400)]"
            : "bg-transparent group-hover:bg-[var(--border-strong)]",
        )}
      />
    </button>
  );
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}

function parseHexList(value: string): string[] {
  return value
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter((item) => /^#?[0-9a-fA-F]{3,8}$/.test(item))
    .map((item) => (item.startsWith("#") ? item : `#${item}`))
    .slice(0, 12);
}
