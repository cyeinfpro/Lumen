"use client";

// Editorial 重构：杂志大标题 + hairline section + underline-on-active chip。
// 模特库独立生成表单：放在"新建模特"tab 里。
// 提交 → onSubmit(body)，由调用方决定后续（通常是切到"任务中心"tab）。

import { motion } from "framer-motion";
import { Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { cn } from "@/lib/utils";
import {
  type ApparelModelLibraryGenerateCount,
  type ApparelModelLibraryGenerateIn,
  type ModelLibraryAppearance,
  type ModelLibraryItemAgeSegment,
  MODEL_LIBRARY_APPEARANCE_LABEL,
} from "@/lib/apiClient";

const AGE_OPTIONS: Array<[ModelLibraryItemAgeSegment, string]> = [
  ["toddler", "幼儿"],
  ["child", "儿童"],
  ["teen", "青少年"],
  ["young_adult", "青年"],
  ["adult", "成年"],
  ["middle_aged", "中老年"],
  ["senior", "老年"],
];

const GENDER_OPTIONS: Array<["female" | "male", string]> = [
  ["female", "女"],
  ["male", "男"],
];

// 外貌方向枚举顺序：和 MODEL_LIBRARY_APPEARANCE_LABEL 对齐
const APPEARANCE_OPTIONS: Array<Exclude<ModelLibraryAppearance, "all">> = [
  "asian",
  "east_asian",
  "southeast_asian",
  "south_asian",
  "european",
  "latin",
  "middle_eastern",
  "african",
  "mixed",
  "other",
];

// 气质风格预设
const STYLE_PRESETS = [
  "温柔",
  "酷感",
  "甜美",
  "复古",
  "极简",
  "高冷",
  "都市",
  "运动",
  "高级感",
  "街头",
];

const COUNT_OPTIONS: ApparelModelLibraryGenerateCount[] = [1, 2, 4, 16];

const EXTRA_MAX = 400;

export interface ModelLibraryGeneratorProps {
  onSubmit: (body: ApparelModelLibraryGenerateIn) => Promise<void> | void;
  generating: boolean;
  defaultAgeSegment?: ModelLibraryItemAgeSegment;
}

export function ModelLibraryGenerator({
  onSubmit,
  generating,
  defaultAgeSegment = "young_adult",
}: ModelLibraryGeneratorProps) {
  const [ageSegment, setAgeSegment] = useState<ModelLibraryItemAgeSegment>(defaultAgeSegment);
  const [gender, setGender] = useState<"female" | "male">("female");
  // 外貌方向：枚举单选，"" 表示不指定
  const [appearance, setAppearance] = useState<ModelLibraryAppearance | "">("");
  // 气质风格：自由文本（chip 点击会拼接到这里）
  const [styleHint, setStyleHint] = useState("");
  const [extra, setExtra] = useState("");
  const [styleTags, setStyleTags] = useState("");
  const [count, setCount] = useState<ApparelModelLibraryGenerateCount>(4);
  const [autoTag, setAutoTag] = useState(true);

  const submit = async () => {
    // styleHint 直接拼进 extra_requirements，前端字段只是输入辅助
    const composedExtra = [extra.trim(), styleHint.trim()]
      .filter(Boolean)
      .join("；");
    const body: ApparelModelLibraryGenerateIn = {
      age_segment: ageSegment,
      gender,
      appearance_direction: appearance || null,
      extra_requirements: composedExtra || null,
      style_tags: splitTags(styleTags),
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

  // 气质风格 chip：点击切换 token 拼接（保留多关键词的拼接体验）
  const toggleStylePreset = (preset: string) => {
    setStyleHint((prev) => {
      const trimmed = prev.trim();
      if (!trimmed) return preset;
      const tokens = trimmed.split(/[、,，\s]+/).filter(Boolean);
      if (tokens.includes(preset)) {
        return tokens.filter((token) => token !== preset).join("、");
      }
      return [...tokens, preset].join("、");
    });
  };

  return (
    <motion.section
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="grid gap-8"
    >
      <header className="border-y border-[var(--border)] py-6 md:py-8">
        <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          N°03 — Generator
        </p>
        <h2 className="mt-3 font-display text-[36px] italic leading-[1] text-[var(--fg-0)] md:text-[52px]">
          新建模特
        </h2>
        <p className="mt-3 max-w-2xl text-[14px] leading-[1.7] text-[var(--fg-1)]">
          {`在不开项目的情况下批量生成模特图，提交后会自动进入"任务中心"。`}
          {`选了"自动识别"会在生成完跑一次风格识别打标签。`}
        </p>
      </header>

      {/* 1. 基础信息 */}
      <Section eyebrow="N°01" title="基础信息">
        <div className="grid gap-6 md:grid-cols-2">
          <Field label="年龄段">
            <ChipRow>
              {AGE_OPTIONS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={ageSegment === value}
                  onClick={() => setAgeSegment(value)}
                >
                  {label}
                </Chip>
              ))}
            </ChipRow>
          </Field>

          <Field label="性别">
            <ChipRow>
              {GENDER_OPTIONS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={gender === value}
                  onClick={() => setGender(value)}
                >
                  {label}
                </Chip>
              ))}
            </ChipRow>
          </Field>
        </div>
      </Section>

      {/* 2. 外貌方向（地域枚举单选） */}
      <Section eyebrow="N°02" title="外貌方向">
        <Field hint="留空由模型自由发挥">
          <ChipRow>
            <Chip active={appearance === ""} onClick={() => setAppearance("")}>
              不指定
            </Chip>
            {APPEARANCE_OPTIONS.map((value) => (
              <Chip
                key={value}
                active={appearance === value}
                onClick={() => setAppearance(value)}
              >
                {MODEL_LIBRARY_APPEARANCE_LABEL[value]}
              </Chip>
            ))}
          </ChipRow>
        </Field>
      </Section>

      {/* 3. 风格 & 细节 */}
      <Section eyebrow="N°03" title="风格 & 细节">
        <div className="grid gap-6">
          <Field
            label="气质风格"
            hint={`点 chip 拼接成"温柔、极简"`}
          >
            <UnderlineInput
              value={styleHint}
              onChange={setStyleHint}
              placeholder={`温柔、极简，或具体到"短发知性"`}
            />
            <div className="mt-3">
              <ChipRow>
                {STYLE_PRESETS.map((preset) => (
                  <Chip
                    key={preset}
                    active={styleHint.includes(preset)}
                    onClick={() => toggleStylePreset(preset)}
                  >
                    {preset}
                  </Chip>
                ))}
              </ChipRow>
            </div>
          </Field>

          <Field label="风格标签" hint="逗号 / 顿号分隔">
            <UnderlineInput
              value={styleTags}
              onChange={setStyleTags}
              placeholder="高级简洁、棚拍"
            />
          </Field>

          <Field label={`其他要求`} hint={`${extra.length}/${EXTRA_MAX}`}>
            <UnderlineTextarea
              value={extra}
              maxLength={EXTRA_MAX}
              onChange={(value) => setExtra(value.slice(0, EXTRA_MAX))}
              rows={3}
              placeholder="例如：自然光棚拍，纯白底，半身正面"
            />
          </Field>
        </div>
      </Section>

      {/* 4. 输出 & 提交 */}
      <Section eyebrow="N°04" title="输出">
        <div className="grid gap-6 md:grid-cols-2 md:items-start">
          <Field label="生成张数">
            <ChipRow>
              {COUNT_OPTIONS.map((option) => (
                <Chip
                  key={option}
                  active={count === option}
                  onClick={() => setCount(option)}
                >
                  <span className="tabular-nums">{String(option).padStart(2, "0")}</span>
                </Chip>
              ))}
            </ChipRow>
          </Field>

          <Field label="自动识别">
            <button
              type="button"
              onClick={() => setAutoTag((prev) => !prev)}
              className="group flex w-full items-center gap-3 border-b border-[var(--border)] pb-3 pt-1 text-left transition-colors hover:border-[var(--border-strong)]"
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
              <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--fg-1)]">
                {autoTag ? "Auto Tag · ON" : "Auto Tag · OFF"}
              </span>
            </button>
          </Field>
        </div>
      </Section>

      {/* 提交条 */}
      <div
        className={cn(
          "sticky bottom-0 z-10 -mx-4 flex flex-col gap-3 border-t border-[var(--border)] bg-[var(--bg-0)]/95 px-4 py-4 backdrop-blur",
          "pb-[calc(16px+env(safe-area-inset-bottom,0px))]",
          "md:static md:z-auto md:m-0 md:flex-row md:flex-wrap md:items-center md:justify-end md:bg-transparent md:px-0 md:py-6 md:backdrop-blur-none md:pb-6",
        )}
      >
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] md:mr-auto">
          {`${count} 张约几分钟，到任务中心查看`}
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
    </motion.section>
  );
}

// 视觉分组：mono eyebrow + 大标题 + 子内容，hairline 分隔
function Section({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-5 border-t border-[var(--border)] pt-6">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          {eyebrow}
        </span>
        <h3 className="font-display text-[20px] italic leading-none text-[var(--fg-0)] md:text-[24px]">
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
    <div className="grid gap-2">
      {label ? (
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {label}
        </span>
      ) : null}
      {children}
      {hint ? (
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-3)]">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

function ChipRow({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap gap-x-5 gap-y-2">{children}</div>;
}

// Filter chip：去 border / bg；mono uppercase + underline-on-active
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
        "group relative inline-flex min-h-10 cursor-pointer items-center px-1 py-1.5 font-mono text-[11px] uppercase tracking-[0.16em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-9",
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

// underline 输入框
function UnderlineInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <input
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
    />
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
      className="w-full resize-none border-b border-[var(--border)] bg-transparent px-1 py-2 text-[15px] leading-[1.6] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:text-sm"
    />
  );
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}
