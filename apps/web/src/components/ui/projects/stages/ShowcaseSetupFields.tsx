import {
  ASPECT_RATIO_LABELS,
  CONTINUITY_ANCHOR_LABELS,
  OUTPUT_COUNT_LABELS,
  SCENE_ENVIRONMENT_LABELS,
  SCENE_STRATEGY_LABELS,
  SCENE_VARIETY_LABELS,
  TEMPLATE_LABELS,
  coerceOutputCount,
  type CreateAspectRatio,
  type CreateContinuityAnchor,
  type CreateSceneEnvironment,
  type CreateSceneStrategy,
  type CreateSceneVariety,
  type CreateTemplate,
} from "../types";
import type { ShowcaseFormController } from "./showcaseStageForm";

const OUTPUT_COUNT_SELECT_OPTIONS = OUTPUT_COUNT_LABELS.map(
  ([value, label]) => [String(value), label] as const,
);

interface ShowcaseSetupFieldsProps {
  form: ShowcaseFormController;
  disabled: boolean;
  showSceneEnvironment?: boolean;
  templateLabel?: string;
  qualityLabel?: string;
}

export function ShowcaseSetupFields({
  form,
  disabled,
  showSceneEnvironment = false,
  templateLabel = "输出模板",
  qualityLabel = "质量模式",
}: ShowcaseSetupFieldsProps) {
  return (
    <>
      <div
        className={`mt-3 grid gap-x-6 gap-y-4 ${
          showSceneEnvironment ? "md:grid-cols-5" : "md:grid-cols-4"
        }`}
      >
        <SelectField
          label={templateLabel}
          value={form.template}
          onChange={(value) => form.setTemplate(value as CreateTemplate)}
          disabled={disabled}
          options={TEMPLATE_LABELS}
        />
        {showSceneEnvironment ? (
          <SelectField
            label="室内 / 室外"
            value={form.sceneEnvironment}
            onChange={(value) =>
              form.setSceneEnvironment(value as CreateSceneEnvironment)
            }
            disabled={disabled}
            options={SCENE_ENVIRONMENT_LABELS}
          />
        ) : null}
        <SelectField
          label="画幅比例"
          value={form.aspectRatio}
          onChange={(value) => form.setAspectRatio(value as CreateAspectRatio)}
          disabled={disabled}
          options={ASPECT_RATIO_LABELS}
        />
        <SelectField
          label={qualityLabel}
          value={form.quality}
          onChange={(value) => form.setQuality(value as "high" | "4k")}
          disabled={disabled}
          options={[
            ["high", "2K 高质量"],
            ["4k", "4K 终稿"],
          ]}
        />
        <SelectField
          label="张数"
          value={String(form.outputCount)}
          onChange={(value) => form.setOutputCount(coerceOutputCount(value))}
          disabled={disabled}
          options={OUTPUT_COUNT_SELECT_OPTIONS}
        />
      </div>
      <div className="mt-4 grid gap-x-6 gap-y-4 md:grid-cols-3">
        <SelectField
          label="场景风格"
          value={form.sceneStrategy}
          onChange={(value) =>
            form.setSceneStrategy(value as CreateSceneStrategy)
          }
          disabled={disabled}
          options={SCENE_STRATEGY_LABELS}
        />
        <SelectField
          label="丰富度"
          value={form.sceneVariety}
          onChange={(value) => form.setSceneVariety(value as CreateSceneVariety)}
          disabled={disabled}
          options={SCENE_VARIETY_LABELS}
        />
        <SelectField
          label="连续元素"
          value={form.continuityAnchor}
          onChange={(value) =>
            form.setContinuityAnchor(value as CreateContinuityAnchor)
          }
          disabled={disabled}
          options={CONTINUITY_ANCHOR_LABELS}
        />
      </div>
      <div className="mt-4 grid gap-x-6 gap-y-3 md:grid-cols-2">
        <CheckboxField
          label="允许宠物"
          checked={form.allowPet}
          onChange={form.setAllowPet}
          disabled={disabled}
        />
        <CheckboxField
          label="允许远处路人"
          checked={form.allowBackgroundPeople}
          onChange={form.setAllowBackgroundPeople}
          disabled={disabled}
        />
      </div>
    </>
  );
}

function SelectField({
  label,
  value,
  onChange,
  disabled,
  options,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  disabled: boolean;
  options: ReadonlyArray<readonly [string, string]>;
}) {
  return (
    <label className="block min-w-0">
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        className="mt-2 h-10 w-full min-w-0 border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--amber-400)] disabled:opacity-40"
      >
        {options.map(([optionValue, optionLabel]) => (
          <option
            key={optionValue}
            value={optionValue}
            className="bg-[var(--bg-1)]"
          >
            {optionLabel}
          </option>
        ))}
      </select>
    </label>
  );
}

function CheckboxField({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled: boolean;
}) {
  return (
    <label className="inline-flex min-h-10 items-center gap-2 text-[13px] text-[var(--fg-1)]">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        disabled={disabled}
        className="h-4 w-4 accent-[var(--amber-400)] disabled:opacity-40"
      />
      <span className="min-w-0 break-words">{label}</span>
    </label>
  );
}
