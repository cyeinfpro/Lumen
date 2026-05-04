"use client";

// 模特库独立生成表单：放在"新建模特"tab 里。
// 提交 → onSubmit(body)，由调用方决定后续（通常是切到"任务中心"tab）。
//
// 字段分组：
//   1. 基础信息：年龄段 / 性别
//   2. 外貌方向：地域枚举单选（appearance_direction）
//   3. 风格 & 细节：气质风格 chip + 自由文本 / 风格标签 / 其他要求
//   4. 输出 & 提交：张数 / 自动识别 / 提交按钮（移动端 sticky）

import { motion } from "framer-motion";
import { Sparkles, WandSparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Input } from "@/components/ui/primitives/Input";
import { Textarea } from "@/components/ui/primitives/Textarea";
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

// 气质风格预设：原 8 个 + "高级感 / 街头" 让风格更全
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
      className="grid gap-5 rounded-2xl border border-[var(--border)] bg-[var(--bg-1)] p-4 shadow-[var(--shadow-1)] md:p-5"
    >
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="flex items-center gap-2 text-[11px] font-medium tracking-[0.16em] text-[var(--fg-2)]">
            <WandSparkles className="h-3.5 w-3.5" />
            LIBRARY GENERATOR
          </p>
          <h3 className="mt-2 font-display italic text-[22px] leading-[1.1] text-[var(--fg-0)] md:text-[26px]">
            新建模特
          </h3>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
            {`在不开项目的情况下批量生成模特图，提交后会自动进入"任务中心"。`}
            {`选了"自动识别"会在生成完跑一次风格识别打标签。`}
          </p>
        </div>
      </header>

      {/* 1. 基础信息 */}
      <Section caption="基础信息">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="年龄段">
            <div className="flex flex-wrap gap-2">
              {AGE_OPTIONS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={ageSegment === value}
                  onClick={() => setAgeSegment(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </Field>

          <Field label="性别">
            <div className="flex flex-wrap gap-2">
              {GENDER_OPTIONS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={gender === value}
                  onClick={() => setGender(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </Field>
        </div>
      </Section>

      {/* 2. 外貌方向（地域枚举单选） */}
      <Section caption="外貌方向">
        <Field hint="地域/外貌偏向，留空表示不指定，由模型自由发挥">
          <div className="flex flex-wrap gap-2">
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
          </div>
        </Field>
      </Section>

      {/* 3. 风格 & 细节 */}
      <Section caption="风格 & 细节">
        <div className="grid gap-4">
          <Field
            label="气质风格"
            hint={`留空也能生成；点 chip 可拼接成"温柔、极简"这样的多关键词`}
          >
            <Input
              value={styleHint}
              onChange={(event) => setStyleHint(event.target.value)}
              placeholder={`温柔、极简，或者具体到"短发知性"`}
            />
            <div className="mt-2 flex flex-wrap gap-2">
              {STYLE_PRESETS.map((preset) => (
                <Chip
                  key={preset}
                  active={styleHint.includes(preset)}
                  onClick={() => toggleStylePreset(preset)}
                >
                  {preset}
                </Chip>
              ))}
            </div>
          </Field>

          <Field
            label="风格标签"
            hint="逗号 / 顿号分隔，写在生成时附在 prompt 上"
          >
            <Input
              value={styleTags}
              onChange={(event) => setStyleTags(event.target.value)}
              placeholder="高级简洁、棚拍"
            />
          </Field>

          <Field label={`其他要求（${extra.length}/${EXTRA_MAX}）`}>
            <Textarea
              value={extra}
              maxLength={EXTRA_MAX}
              onChange={(event) => setExtra(event.target.value.slice(0, EXTRA_MAX))}
              rows={3}
              placeholder="例如：偏向自然光棚拍，纯白底，半身正面"
            />
          </Field>
        </div>
      </Section>

      {/* 4. 输出 & 提交 */}
      <Section caption="输出">
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] md:items-end">
          <Field label="生成张数">
            <div className="flex flex-wrap gap-2">
              {COUNT_OPTIONS.map((option) => (
                <Chip
                  key={option}
                  active={count === option}
                  onClick={() => setCount(option)}
                  className="min-w-[3.25rem] justify-center"
                >
                  {option}
                </Chip>
              ))}
            </div>
          </Field>

          <Field label="自动识别">
            <button
              type="button"
              onClick={() => setAutoTag((prev) => !prev)}
              className={cn(
                "h-10 w-full rounded-md border px-3 text-left text-sm transition-colors",
                autoTag
                  ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                  : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)]",
              )}
            >
              <div className="flex items-center gap-2">
                <span
                  aria-hidden
                  className={cn(
                    "inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors",
                    autoTag
                      ? "border-[var(--border-amber)] bg-[var(--accent)]"
                      : "border-[var(--border)] bg-white/8",
                  )}
                >
                  <span
                    className={cn(
                      "ml-0.5 h-3 w-3 rounded-full bg-white transition-transform",
                      autoTag ? "translate-x-3" : "",
                    )}
                  />
                </span>
                <span className="truncate">
                  {autoTag ? "生成完会自动打标签" : "不自动识别"}
                </span>
              </div>
            </button>
          </Field>
        </div>
      </Section>

      {/* 提交条：移动端 sticky 底部 + 全宽，桌面端靠右 */}
      <div
        className={cn(
          "sticky bottom-0 z-10 -mx-4 -mb-4 flex flex-col gap-2 border-t border-[var(--border)] bg-[var(--bg-1)] px-4 py-3",
          "pb-[calc(12px+env(safe-area-inset-bottom,0px))]",
          "md:static md:z-auto md:m-0 md:flex-row md:flex-wrap md:items-center md:justify-end md:border-0 md:bg-transparent md:p-0 md:pb-0",
        )}
      >
        <p className="text-xs leading-5 text-[var(--fg-2)] md:mr-auto">
          {`张数越多耗时越久，16 张约几分钟，请耐心等待并到"任务中心"查看结果。`}
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

// 视觉分组：caption + 子内容，靠间距而非边框
function Section({
  caption,
  children,
}: {
  caption: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-3">
      <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--fg-2)]">
        {caption}
      </p>
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
        <span className="text-xs font-medium text-[var(--fg-1)]">{label}</span>
      ) : null}
      {children}
      {hint ? (
        <p className="text-xs leading-5 text-[var(--fg-2)]">{hint}</p>
      ) : null}
    </div>
  );
}

function Chip({
  children,
  active,
  onClick,
  className,
}: {
  children: React.ReactNode;
  active?: boolean;
  onClick?: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex min-h-10 cursor-pointer items-center rounded-md border px-3.5 text-[13px] transition-colors",
        "md:min-h-9 md:px-3 md:text-xs",
        active
          ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
          : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/[0.04] hover:text-[var(--fg-0)]",
        className,
      )}
    >
      {children}
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
