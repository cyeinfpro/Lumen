"use client";

import type { CanvasNodeType } from "@/lib/canvas/types";
import {
  FixedSizeInput,
  selectOptionsWithCurrent,
} from "./CanvasNodeConfigControls";
import {
  ConfigSection,
  SliderField,
  SelectField,
  ToggleField,
} from "./CanvasNodeConfigFields";
import type { SelectOption } from "./CanvasNodeConfigFields";
import type { CanvasNodeConfigEditorProps } from "./CanvasNodeConfigEditorContracts";

const IMAGE_ASPECT_OPTIONS: readonly SelectOption[] = [
  { value: "1:1", label: "方形 1:1" },
  { value: "4:5", label: "竖版 4:5" },
  { value: "3:4", label: "竖版 3:4" },
  { value: "2:3", label: "竖版 2:3" },
  { value: "7:10", label: "竖版 7:10" },
  { value: "9:16", label: "竖屏 9:16" },
  { value: "3:2", label: "横版 3:2" },
  { value: "4:3", label: "横版 4:3" },
  { value: "10:7", label: "横版 10:7" },
  { value: "16:9", label: "宽屏 16:9" },
  { value: "21:9", label: "超宽 21:9" },
  { value: "9:21", label: "超长竖屏 9:21" },
];

const IMAGE_QUALITY_OPTIONS: readonly SelectOption[] = [
  { value: "1k", label: "1K" },
  { value: "2k", label: "2K" },
  { value: "4k", label: "4K" },
  { value: "standard", label: "标准（旧配置）" },
  { value: "high", label: "高质量（旧配置）" },
];

const RENDER_QUALITY_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const SIZE_MODE_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "按比例自动计算" },
  { value: "fixed", label: "固定像素尺寸" },
];

const IMAGE_FORMAT_OPTIONS: readonly SelectOption[] = [
  { value: "webp", label: "WebP" },
  { value: "jpeg", label: "JPEG" },
  { value: "png", label: "PNG" },
];

const IMAGE_BACKGROUND_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动" },
  { value: "opaque", label: "不透明" },
  { value: "transparent", label: "透明" },
];

const IMAGE_MODERATION_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动审核" },
  { value: "low", label: "低强度审核" },
];

interface CanvasNodeConfigImageEditorProps
  extends CanvasNodeConfigEditorProps {
  imageFormatPatch: (value: string) => Record<string, unknown>;
}

export function CanvasNodeConfigImageEditor(
  props: CanvasNodeConfigImageEditorProps,
) {
  return (
    <>
      <ImageGenerationParameters {...props} />
      <ImageOutputParameters {...props} />
    </>
  );
}

function ImageGenerationParameters({
  node,
  patch,
}: CanvasNodeConfigEditorProps) {
  const sizeMode = String(node.config.size_mode ?? "auto");
  const aspectRatio = String(node.config.aspect_ratio ?? "1:1");
  return (
    <ConfigSection title={imageParameterSectionTitle(node.type)}>
      <SelectField
        label="比例"
        value={aspectRatio}
        options={selectOptionsWithCurrent(
          IMAGE_ASPECT_OPTIONS.map((option) => option.value),
          aspectRatio,
          IMAGE_ASPECT_OPTIONS,
          true,
        )}
        onChange={(value) => patch({ aspect_ratio: value })}
      />
      <SelectField
        label="输出尺寸"
        value={String(node.config.quality ?? "2k").toLowerCase()}
        options={IMAGE_QUALITY_OPTIONS}
        onChange={(quality) =>
          patch({
            quality,
            size: imageSizeForQuality(quality, node.config.size),
          })
        }
      />
      <SelectField
        label="尺寸模式"
        value={sizeMode}
        options={SIZE_MODE_OPTIONS}
        onChange={(value) =>
          patch({
            size_mode: value,
            fixed_size: value === "fixed" ? node.config.fixed_size ?? "" : null,
          })
        }
      />
      {sizeMode === "fixed" ? (
        <FixedSizeInput
          value={String(node.config.fixed_size ?? "")}
          onCommit={(fixedSize) => patch({ fixed_size: fixedSize || null })}
        />
      ) : null}
      <SelectField
        label="渲染质量"
        value={String(node.config.render_quality ?? "high")}
        options={RENDER_QUALITY_OPTIONS}
        onChange={(value) => patch({ render_quality: value })}
      />
      <SliderField
        label="候选数量"
        value={Number(node.config.count ?? 1)}
        min={1}
        max={10}
        onChange={(value) => patch({ count: value })}
      />
    </ConfigSection>
  );
}

function ImageOutputParameters({
  node,
  patch,
  imageFormatPatch,
}: CanvasNodeConfigImageEditorProps) {
  const outputFormat = String(node.config.output_format ?? "webp");
  const compression = numericConfigValue(node.config.output_compression);
  return (
    <ConfigSection title="输出">
      <SelectField
        label="图片格式"
        value={outputFormat}
        options={IMAGE_FORMAT_OPTIONS}
        onChange={(value) => patch(imageFormatPatch(value))}
      />
      <SelectField
        label="背景"
        value={String(node.config.background ?? "auto")}
        options={IMAGE_BACKGROUND_OPTIONS}
        onChange={(value) => patch(imageBackgroundPatch(value, outputFormat))}
      />
      {outputFormat === "png" ? null : (
        <ImageCompressionControls compression={compression} patch={patch} />
      )}
      <SelectField
        label="内容审核"
        value={String(node.config.moderation ?? "low")}
        options={IMAGE_MODERATION_OPTIONS}
        onChange={(value) => patch({ moderation: value })}
      />
    </ConfigSection>
  );
}

function ImageCompressionControls({
  compression,
  patch,
}: {
  compression: number | null;
  patch: CanvasNodeConfigEditorProps["patch"];
}) {
  return (
    <>
      <ToggleField
        label="自定义压缩"
        checked={compression !== null}
        onChange={(enabled) =>
          patch({ output_compression: enabled ? 90 : null })
        }
      />
      {compression !== null ? (
        <SliderField
          label="压缩质量"
          value={compression}
          min={0}
          max={100}
          suffix="%"
          onChange={(value) => patch({ output_compression: value })}
        />
      ) : null}
    </>
  );
}

function imageParameterSectionTitle(type: CanvasNodeType): string {
  if (type === "image_edit") return "编辑参数";
  if (type === "image_inpaint") return "重绘参数";
  if (type === "image_upscale") return "高清参数";
  return "生成参数";
}

function imageSizeForQuality(quality: string, current: unknown): string {
  const normalizedQuality = quality.toLowerCase();
  if (["1k", "2k", "4k"].includes(normalizedQuality)) {
    return normalizedQuality.toUpperCase();
  }
  const normalizedCurrent = String(current ?? "1K").toUpperCase();
  return ["1K", "2K", "4K"].includes(normalizedCurrent)
    ? normalizedCurrent
    : "1K";
}

function numericConfigValue(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function imageBackgroundPatch(
  value: string,
  outputFormat: string,
): Record<string, unknown> {
  if (value === "transparent") {
    if (outputFormat !== "jpeg") {
      return { background: value };
    }
    return {
      background: value,
      output_format: "png",
      output_compression: null,
    };
  }
  return { background: value };
}
