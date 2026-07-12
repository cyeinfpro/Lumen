"use client";

// 海报风格库独立生成器（蓝本：ModelLibraryGenerator）。
// 表单：title / category / prompt / style_tags / palette / mood /
//      recommended_aspects / count / aspect_ratio / auto_tag
// 提交后由调用方（PosterStylePage）切到"任务中心" tab。

import { Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import {
  POSTER_STYLE_ASPECT_OPTIONS,
  POSTER_STYLE_CATEGORY_LABEL,
  POSTER_STYLE_CATEGORY_OPTIONS,
  type PosterStyleCategory,
  type PosterStyleGenerateCount,
  type PosterStyleGenerateIn,
} from "@/lib/apiClient";
import { cn } from "@/lib/utils";

const STYLE_PRESETS = [
  "极简",
  "复古",
  "中式",
  "孟菲斯",
  "杂志",
  "扁平插画",
  "厚涂",
  "霓虹",
  "极致排版",
];

const COUNT_OPTIONS: PosterStyleGenerateCount[] = [1, 2, 3, 4];

const PROMPT_MAX = 2000;

export interface PosterStyleGeneratorProps {
  onSubmit: (body: PosterStyleGenerateIn) => Promise<void> | void;
  generating: boolean;
  defaultCategory?: PosterStyleCategory;
}

export function PosterStyleGenerator({
  onSubmit,
  generating,
  defaultCategory = "illustration",
}: PosterStyleGeneratorProps) {
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<PosterStyleCategory>(defaultCategory);
  const [prompt, setPrompt] = useState("");
  const [mood, setMood] = useState("");
  const [styleTags, setStyleTags] = useState<string[]>([]);
  const [palette, setPalette] = useState("");
  const [recommendedAspects, setRecommendedAspects] = useState<string[]>([
    "1:1",
  ]);
  const [aspectRatio, setAspectRatio] = useState("1:1");
  const [count, setCount] = useState<PosterStyleGenerateCount>(2);
  const [autoTag, setAutoTag] = useState(true);

  const submit = async () => {
    if (!title.trim()) {
      toast.warning("请填写风格名称");
      return;
    }
    if (!prompt.trim()) {
      toast.warning("请填写生成 prompt");
      return;
    }
    const body: PosterStyleGenerateIn = {
      title: title.trim().slice(0, 120),
      category,
      prompt: prompt.trim(),
      mood: mood.trim() || null,
      style_tags: styleTags,
      palette: parseHexList(palette),
      recommended_aspects: recommendedAspects,
      aspect_ratio: aspectRatio,
      count,
      auto_tag: autoTag,
    };
    try {
      await onSubmit(body);
    } catch (err) {
      toast.error("提交失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    }
  };

  const toggleStylePreset = (preset: string) => {
    setStyleTags((prev) => {
      if (prev.includes(preset)) {
        return prev.filter((token) => token !== preset);
      }
      return [...prev, preset].slice(0, 8);
    });
  };

  const toggleAspect = (value: string) => {
    setRecommendedAspects((prev) =>
      prev.includes(value)
        ? prev.filter((v) => v !== value)
        : [...prev, value].slice(0, 8),
    );
  };

  return (
    <section className="grid gap-4 md:gap-5">
      <header className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2 border-b border-[var(--border)] pb-3 max-[360px]:sr-only">
        <div className="min-w-0">
          <p className="type-page-kicker">生成器</p>
          <h2 className="type-page-title mt-1 md:text-[28px]">新建风格</h2>
        </div>
        <p className="type-page-subtitle max-w-2xl md:max-w-xl md:text-right">
          {`用 prompt 生成 N 张风格样图入库，"自动识别"会在出图后跑一次 vision 反推填充色板和标签。`}
        </p>
      </header>

      <div className="grid gap-4 xl:grid-cols-2">
        {/* 1. 基础信息 */}
        <Section eyebrow="N°01" title="基础信息">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
            <Field label="风格名称">
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                maxLength={120}
                placeholder="例如：低饱和极简海报"
                className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
              />
            </Field>
            <Field label="类目">
              <select
                value={category}
                onChange={(event) =>
                  setCategory(event.target.value as PosterStyleCategory)
                }
                className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none focus:border-[var(--amber-400)] md:h-10 md:text-sm"
              >
                {POSTER_STYLE_CATEGORY_OPTIONS.map((value) => (
                  <option
                    key={value}
                    value={value}
                    className="bg-[var(--bg-0)]"
                  >
                    {POSTER_STYLE_CATEGORY_LABEL[value]}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </Section>

        {/* 2. Prompt */}
        <Section eyebrow="N°02" title="Prompt" className="xl:row-span-2">
          <Field
            label="生成 prompt"
            hint={`${prompt.length}/${PROMPT_MAX}`}
          >
            <UnderlineTextarea
              value={prompt}
              maxLength={PROMPT_MAX}
              onChange={(value) => setPrompt(value.slice(0, PROMPT_MAX))}
              rows={6}
              placeholder="一张极简风格的海报，平面构图，主体居中，留白克制"
            />
          </Field>
          <Field label="情绪 / mood" hint="一个短词即可">
            <input
              value={mood}
              onChange={(event) => setMood(event.target.value)}
              maxLength={120}
              placeholder="冷静、温暖、奇幻"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
          </Field>
          <Field label="色板（逗号分隔 hex）" hint="可留空，自动识别会补">
            <input
              value={palette}
              onChange={(event) => setPalette(event.target.value)}
              placeholder="#F2A93A, #2A2A2A"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
            <PaletteSwatchRow hexes={parseHexList(palette)} />
          </Field>
        </Section>

        {/* 3. 风格标签 */}
        <Section eyebrow="N°03" title="风格标签">
          <Field hint="最多选 8 个；自动识别也会追加">
            <ChipRow>
              {STYLE_PRESETS.map((preset) => (
                <Chip
                  key={preset}
                  active={styleTags.includes(preset)}
                  onClick={() => toggleStylePreset(preset)}
                >
                  {preset}
                </Chip>
              ))}
            </ChipRow>
          </Field>
        </Section>

        {/* 4. 输出 & 推荐尺寸 */}
        <Section eyebrow="N°04" title="输出" className="xl:col-span-2">
          <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(220px,0.55fr)] xl:grid-cols-[minmax(0,0.7fr)_minmax(220px,0.42fr)_minmax(220px,0.42fr)_auto] xl:items-end">
            <Field label="生成尺寸">
              <ChipRow>
                {POSTER_STYLE_ASPECT_OPTIONS.map((value) => (
                  <Chip
                    key={value}
                    active={aspectRatio === value}
                    onClick={() => setAspectRatio(value)}
                  >
                    {value}
                  </Chip>
                ))}
              </ChipRow>
            </Field>

            <Field label="推荐尺寸（多选）" hint="海报应用时的默认尺寸">
              <ChipRow>
                {POSTER_STYLE_ASPECT_OPTIONS.map((value) => (
                  <Chip
                    key={value}
                    active={recommendedAspects.includes(value)}
                    onClick={() => toggleAspect(value)}
                  >
                    {value}
                  </Chip>
                ))}
              </ChipRow>
            </Field>

            <Field label="生成张数">
              <ChipRow>
                {COUNT_OPTIONS.map((option) => (
                  <Chip
                    key={option}
                    active={count === option}
                    onClick={() => setCount(option)}
                  >
                    <span className="tabular-nums">
                      {String(option).padStart(2, "0")}
                    </span>
                  </Chip>
                ))}
              </ChipRow>
            </Field>

            <Field label="自动识别">
              <button
                type="button"
                onClick={() => setAutoTag((prev) => !prev)}
                className="group flex min-h-11 w-full items-center gap-3 border-b border-[var(--border)] pb-2 pt-0.5 text-left transition-colors hover:border-[var(--border-strong)] md:min-h-9"
                aria-pressed={autoTag}
              >
                <span
                  aria-hidden
                  className={cn(
                    "inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors",
                    autoTag
                      ? "border-[var(--border-amber)] bg-[var(--accent)]"
                      : "border-[var(--border-strong)] bg-transparent",
                  )}
                >
                  <span
                    className={cn(
                      "ml-0.5 h-3 w-3 rounded-full bg-white transition-transform",
                      autoTag ? "translate-x-3" : "",
                    )}
                  />
                </span>
                <span className="font-mono text-[10.5px] uppercase tracking-[0.15em] text-[var(--fg-1)]">
                  {autoTag ? "自动识别 · 开" : "自动识别 · 关"}
                </span>
              </button>
            </Field>
          </div>

          <div className="mt-2 hidden flex-col gap-2 md:flex md:flex-row md:items-center md:justify-between">
            <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
              {`将生成 ${count} 张样图`}
            </p>
            <Button
              variant="primary"
              loading={generating}
              onClick={submit}
              leftIcon={<Sparkles className="h-4 w-4" />}
            >
              开始生成
            </Button>
          </div>
        </Section>
      </div>

      {/* 提交条（mobile） */}
      <div className="-mx-3 sticky bottom-0 z-20 mt-1 flex flex-col gap-2 border-t border-[var(--border)] bg-[var(--bg-0)]/95 px-3 py-3 shadow-[var(--shadow-1)] backdrop-blur-xl md:hidden">
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
          {`将生成 ${count} 张样图`}
        </p>
        <Button
          variant="primary"
          loading={generating}
          onClick={submit}
          leftIcon={<Sparkles className="h-4 w-4" />}
          className="w-full md:w-auto"
        >
          开始生成
        </Button>
      </div>
    </section>
  );
}

function Section({
  eyebrow,
  title,
  className,
  children,
}: {
  eyebrow: string;
  title: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "grid gap-3 border-t border-[var(--border)] pt-3 md:pt-4",
        className,
      )}
    >
      <div className="flex items-baseline gap-2.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          {eyebrow}
        </span>
        <h3 className="text-[16px] font-semibold leading-none tracking-tight text-[var(--fg-0)] md:text-[17px]">
          {title}
        </h3>
      </div>
      {children}
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-1.5">
      {label ? (
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {label}
        </span>
      ) : null}
      {children}
      {hint ? (
        <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-3)]">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

function ChipRow({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap gap-x-3.5 gap-y-1">{children}</div>;
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

function UnderlineTextarea({
  value,
  onChange,
  placeholder,
  rows,
  maxLength,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
  maxLength?: number;
}) {
  return (
    <textarea
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      rows={rows}
      maxLength={maxLength}
      className="w-full resize-none border-b border-[var(--border)] bg-transparent px-1 py-1.5 text-[15px] leading-[1.45] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:text-sm"
    />
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

function parseHexList(value: string): string[] {
  return value
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter((item) => /^#?[0-9a-fA-F]{3,8}$/.test(item))
    .map((item) => (item.startsWith("#") ? item : `#${item}`))
    .slice(0, 12);
}
