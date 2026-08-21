"use client";

import {
  CheckCircle2,
  CircleAlert,
} from "lucide-react";
import type { ComponentType } from "react";

import { CANVAS_NOTE_MAX_CHARS } from "@/lib/canvas/constants";
import { validateCanvasNodeExecution } from "@/lib/canvas/graph";
import {
  CANVAS_NODE_SPECS,
  isCanvasExecutableNodeType,
} from "@/lib/canvas/registry";
import type { CanvasNodeType } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";
import {
  normalizedCrop,
  UploadField,
} from "./CanvasNodeConfigControls";
import {
  CommitInput,
  CommitTextarea,
  ConfigSection,
  SliderField,
  ToggleField,
} from "./CanvasNodeConfigFields";
import type { CanvasNodeConfigEditorProps } from "./CanvasNodeConfigEditorContracts";
import {
  DeliveryConfig,
  FrameConfig,
  PromptConfig,
  PromptMergeConfig,
  VideoAssetConfig,
} from "./CanvasNodeConfigGeneralEditors";
import { CanvasNodeConfigImageEditor } from "./CanvasNodeConfigImageEditor";
import { VideoGenerateConfig } from "./CanvasNodeConfigVideoEditor";

export type { CanvasNodeConfigEditorProps } from "./CanvasNodeConfigEditorContracts";

// Source contracts track the delegated capability helpers by name:
// videoResolutionOptionsForModels, videoDurationOptionsForModels,
// selectVideoModelForParameters.

export function CanvasNodeConfigEditor(
  props: CanvasNodeConfigEditorProps,
) {
  const Editor = CONFIG_EDITORS[props.node.type];
  return (
    <>
      <InputStatusSection node={props.node} graph={props.graph} />
      <Editor {...props} />
    </>
  );
}

function InputStatusSection({
  node,
  graph,
}: Pick<CanvasNodeConfigEditorProps, "node" | "graph">) {
  const ports = CANVAS_NODE_SPECS[node.type].inputs;
  if (ports.length === 0) return null;
  const counts = new Map<string, number>();
  for (const edge of graph.edges) {
    if (edge.target_node_id !== node.id) continue;
    counts.set(edge.target_handle, (counts.get(edge.target_handle) ?? 0) + 1);
  }
  const executionValidation = isCanvasExecutableNodeType(node.type)
    ? validateCanvasNodeExecution(graph, node.id)
    : null;
  const executionIssue =
    executionValidation && !executionValidation.valid
      ? executionValidation.reason
      : null;
  return (
    <ConfigSection title="输入">
      <div className="grid gap-2">
        {ports.map((port) => {
          const count = counts.get(port.id) ?? 0;
          const missing = port.required === true && count === 0;
          const connected = count > 0;
          return (
            <div
              key={port.id}
              className="flex min-h-9 items-center justify-between gap-3"
            >
              <span className="min-w-0 truncate type-body-sm text-[var(--fg-1)]">
                {port.label}
              </span>
              <span
                className={cn(
                  "inline-flex shrink-0 items-center gap-1.5 type-caption tabular-nums",
                  missing
                    ? "text-[var(--danger-fg)]"
                    : connected
                      ? "text-[var(--success-fg)]"
                      : "text-[var(--fg-3)]",
                )}
              >
                {missing ? (
                  <CircleAlert className="h-3.5 w-3.5" aria-hidden />
                ) : connected ? (
                  <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                ) : null}
                {connected ? `${count} 个` : "未连接"}
                {port.required ? " · 必需" : ""}
              </span>
            </div>
          );
        })}
      </div>
      {executionIssue ? (
        <p role="alert" className="type-caption text-[var(--danger-fg)]">
          {executionIssue}
        </p>
      ) : null}
    </ConfigSection>
  );
}

function ImageAssetConfig({
  node,
  patch,
  uploading,
  onUploadImage,
}: CanvasNodeConfigEditorProps) {
  const isMask = node.type === "mask_asset";
  const crop = normalizedCrop(node.config.crop);
  return (
    <>
      <ConfigSection title={isMask ? "遮罩素材" : "图片素材"}>
        <CommitInput
          label="显示名称"
          value={String(node.config.display_name ?? "")}
          maxLength={255}
          onCommit={(displayName) =>
            patch({ display_name: displayName || null })
          }
        />
        <CommitInput
          label="图片 ID"
          value={String(node.config.image_id ?? "")}
          maxLength={36}
          onCommit={(imageId) => patch({ image_id: imageId })}
        />
        <UploadField
          accept={isMask ? "image/png" : "image/png,image/jpeg,image/webp"}
          busy={uploading}
          label={isMask ? "上传遮罩" : "上传图片"}
          onSelect={onUploadImage}
        />
      </ConfigSection>
      <ConfigSection title="预览裁切">
        <ToggleField
          label="启用裁切"
          checked={crop !== null}
          onChange={(enabled) =>
            patch({
              crop: enabled
                ? { x: 0, y: 0, width: 1, height: 1 }
                : null,
            })
          }
        />
        {crop ? (
          <div className="grid gap-3">
            <SliderField
              label="水平起点"
              value={Math.round(crop.x * 100)}
              min={0}
              max={Math.round((1 - crop.width) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, x: value / 100 },
                })
              }
            />
            <SliderField
              label="垂直起点"
              value={Math.round(crop.y * 100)}
              min={0}
              max={Math.round((1 - crop.height) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, y: value / 100 },
                })
              }
            />
            <SliderField
              label="裁切宽度"
              value={Math.round(crop.width * 100)}
              min={5}
              max={Math.round((1 - crop.x) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, width: value / 100 },
                })
              }
            />
            <SliderField
              label="裁切高度"
              value={Math.round(crop.height * 100)}
              min={5}
              max={Math.round((1 - crop.y) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, height: value / 100 },
                })
              }
            />
          </div>
        ) : null}
      </ConfigSection>
    </>
  );
}

function ImageGenerateConfig(props: CanvasNodeConfigEditorProps) {
  return (
    <CanvasNodeConfigImageEditor
      {...props}
      imageFormatPatch={imageFormatPatch}
    />
  );
}

function imageFormatPatch(value: string): Record<string, unknown> {
  if (value === "png") {
    return { output_format: value, output_compression: null };
  }
  if (value === "jpeg") {
    return { output_format: value, background: "opaque" };
  }
  return { output_format: value };
}

function NoteConfig({
  node,
  patch,
}: CanvasNodeConfigEditorProps) {
  const tags = Array.isArray(node.config.tags)
    ? node.config.tags.filter((tag): tag is string => typeof tag === "string")
    : [];
  return (
    <ConfigSection title="备注">
      <CommitTextarea
        label="内容"
        value={String(node.config.text ?? "")}
        maxLength={CANVAS_NOTE_MAX_CHARS}
        rows={8}
        placeholder="记录创作说明、审核意见或交付要求"
        onCommit={(text) => patch({ text })}
      />
      <CommitInput
        label="标签"
        value={tags.join("，")}
        maxLength={395}
        placeholder="用逗号分隔，最多 12 个"
        onCommit={(raw) =>
          patch({
            tags: Array.from(
              new Set(
                raw
                  .split(/[,，]/)
                  .map((tag) => tag.trim())
                  .filter(Boolean)
                  .slice(0, 12),
              ),
            ),
          })
        }
      />
    </ConfigSection>
  );
}

const CONFIG_EDITORS: Record<
  CanvasNodeType,
  ComponentType<CanvasNodeConfigEditorProps>
> = {
  prompt: PromptConfig,
  prompt_merge: PromptMergeConfig,
  image_asset: ImageAssetConfig,
  mask_asset: ImageAssetConfig,
  video_asset: VideoAssetConfig,
  image_generate: ImageGenerateConfig,
  image_edit: ImageGenerateConfig,
  image_inpaint: ImageGenerateConfig,
  image_upscale: ImageGenerateConfig,
  video_generate: VideoGenerateConfig,
  video_text_generate: VideoGenerateConfig,
  video_image_generate: VideoGenerateConfig,
  video_reference_generate: VideoGenerateConfig,
  note: NoteConfig,
  frame: FrameConfig,
  delivery: DeliveryConfig,
};
