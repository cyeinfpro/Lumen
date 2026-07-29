"use client";

import {
  AtSign,
  RefreshCw,
  Trash2,
} from "lucide-react";

import {
  ActionSheet,
  BottomSheet,
} from "@/components/ui/primitives/mobile";
import type {
  AspectRatio,
  Quality,
  RenderQualityChoice,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import type { ReasoningEffort } from "@/store/useChatStore";

import { LazyMaskCanvas } from "../LazyMaskCanvas";
import {
  attachmentRoleLabel,
  type ComposerAttachmentRole,
} from "../shared/attachmentRoles";
import type { useMaskInpaint } from "../shared/useMaskInpaint";
import { AspectRatioPicker } from "../shared/AspectRatioPicker";
import {
  MOBILE_REASONING_OPTIONS,
  MobileAdvancedSettings,
} from "./MobileAdvancedSettings";
import type { MobileComposerMode } from "./MobileComposerButtons";

type Inpaint = ReturnType<typeof useMaskInpaint>;

export type MobileComposerPanel =
  | "none"
  | "advanced"
  | "aspect"
  | "reasoning";

interface MobileComposerOverlaysProps {
  panel: MobileComposerPanel;
  attachmentMenuIndex: number;
  attachmentMenuId: string | null;
  attachmentMenuRole: ComposerAttachmentRole | null;
  mode: MobileComposerMode;
  quality: Quality;
  renderQuality: RenderQualityChoice;
  aspect: AspectRatio;
  count: number;
  reasoningEffort: ReasoningEffort | undefined;
  webSearch: boolean;
  fileSearch: boolean;
  codeInterpreter: boolean;
  imageGeneration: boolean;
  fast: boolean;
  inpaint: Inpaint;
  onCloseAttachmentMenu: () => void;
  onInsertMention: (imageNumber: number) => void;
  onCycleRole: (id: string) => void;
  onRemoveAttachment: (id: string) => void;
  onClosePanel: () => void;
  onQualityChange: (value: Quality) => void;
  onRenderQualityChange: (value: RenderQualityChoice) => void;
  onAspectChange: (value: AspectRatio) => void;
  onOpenAspect: () => void;
  onCountChange: (value: number) => void;
  onOpenReasoning: () => void;
  onReasoningEffortChange: (value: ReasoningEffort) => void;
  onWebSearchChange: (value: boolean) => void;
  onFileSearchChange: (value: boolean) => void;
  onCodeInterpreterChange: (value: boolean) => void;
  onImageGenerationChange: (value: boolean) => void;
  onFastChange: (value: boolean) => void;
}

function attachmentMenuDescription(
  role: ComposerAttachmentRole | null,
): string | undefined {
  return role ? `当前用途：${attachmentRoleLabel(role)}` : undefined;
}

function buildAttachmentMenuActions(input: {
  index: number;
  id: string | null;
  insertMention: (imageNumber: number) => void;
  cycleRole: (id: string) => void;
  removeAttachment: (id: string) => void;
}): React.ComponentProps<typeof ActionSheet>["actions"] {
  if (input.index < 0 || !input.id) return [];
  const imageNumber = input.index + 1;
  const id = input.id;
  return [
    {
      key: "mention",
      label: `插入 @图${imageNumber}`,
      icon: <AtSign className="h-5 w-5" aria-hidden />,
      onSelect: () => input.insertMention(imageNumber),
    },
    {
      key: "role",
      label: "切换图片用途",
      icon: <RefreshCw className="h-5 w-5" aria-hidden />,
      onSelect: () => input.cycleRole(id),
    },
    {
      key: "remove",
      label: "移除参考图",
      icon: <Trash2 className="h-5 w-5" aria-hidden />,
      destructive: true,
      onSelect: () => input.removeAttachment(id),
    },
  ];
}

export function MobileComposerOverlays({
  panel,
  attachmentMenuIndex,
  attachmentMenuId,
  attachmentMenuRole,
  mode,
  quality,
  renderQuality,
  aspect,
  count,
  reasoningEffort,
  webSearch,
  fileSearch,
  codeInterpreter,
  imageGeneration,
  fast,
  inpaint,
  onCloseAttachmentMenu,
  onInsertMention,
  onCycleRole,
  onRemoveAttachment,
  onClosePanel,
  onQualityChange,
  onRenderQualityChange,
  onAspectChange,
  onOpenAspect,
  onCountChange,
  onOpenReasoning,
  onReasoningEffortChange,
  onWebSearchChange,
  onFileSearchChange,
  onCodeInterpreterChange,
  onImageGenerationChange,
  onFastChange,
}: MobileComposerOverlaysProps) {
  const attachmentTitle =
    attachmentMenuIndex >= 0
      ? `图 ${attachmentMenuIndex + 1}`
      : undefined;
  const attachmentActions = buildAttachmentMenuActions({
    index: attachmentMenuIndex,
    id: attachmentMenuId,
    insertMention: onInsertMention,
    cycleRole: onCycleRole,
    removeAttachment: onRemoveAttachment,
  });

  return (
    <>
      <ActionSheet
        open={attachmentMenuIndex >= 0}
        onClose={onCloseAttachmentMenu}
        title={attachmentTitle}
        description={attachmentMenuDescription(attachmentMenuRole)}
        actions={attachmentActions}
      />

      <BottomSheet
        open={panel === "advanced"}
        onClose={onClosePanel}
        ariaLabel="执行设置"
        snapPoints={["80%"]}
      >
        <MobileAdvancedSettings
          mode={mode}
          quality={quality}
          onQualityChange={onQualityChange}
          renderQuality={renderQuality}
          onRenderQualityChange={onRenderQualityChange}
          aspect={aspect}
          onOpenAspect={onOpenAspect}
          count={count}
          onCountChange={onCountChange}
          reasoningEffort={reasoningEffort ?? "medium"}
          onOpenReasoning={onOpenReasoning}
          webSearch={webSearch}
          onWebSearchChange={onWebSearchChange}
          fileSearch={fileSearch}
          onFileSearchChange={onFileSearchChange}
          codeInterpreter={codeInterpreter}
          onCodeInterpreterChange={onCodeInterpreterChange}
          imageGeneration={imageGeneration}
          onImageGenerationChange={onImageGenerationChange}
          fast={fast}
          onFastChange={onFastChange}
        />
      </BottomSheet>

      <BottomSheet
        open={panel === "aspect"}
        onClose={onClosePanel}
        ariaLabel="选择宽高比"
      >
        <AspectRatioPicker
          value={aspect}
          onChange={onAspectChange}
          onClose={onClosePanel}
          variant="sheet"
        />
      </BottomSheet>

      <BottomSheet
        open={panel === "reasoning"}
        onClose={onClosePanel}
        ariaLabel="选择推理强度"
      >
        <SheetList
          title="推理强度"
          items={MOBILE_REASONING_OPTIONS.map((option) => ({
            key: option.value,
            label: option.label,
            hint: option.hint,
            selected: option.value === reasoningEffort,
            onSelect: () => onReasoningEffortChange(option.value),
          }))}
        />
      </BottomSheet>

      {inpaint.open ? (
        <LazyMaskCanvas
          open={inpaint.open}
          imageSrc={inpaint.sourceImageSrc}
          onClose={inpaint.closeInpaint}
          onConfirm={inpaint.handleConfirm}
          submitting={inpaint.submitting}
        />
      ) : null}
    </>
  );
}

function SheetList({
  title,
  items,
}: {
  title: string;
  items: Array<{
    key: string;
    label: string;
    hint?: string;
    selected: boolean;
    onSelect: () => void;
  }>;
}) {
  return (
    <div className="px-4 pb-5">
      <div className="py-3.5 text-center text-[15px] font-semibold text-[var(--fg-0)] border-b border-[var(--border-subtle)]">
        {title}
      </div>
      <ul className="flex flex-col">
        {items.map((item) => (
          <li
            key={item.key}
            className="border-b border-[var(--border-subtle)] last:border-b-0"
          >
            <button
              type="button"
              onClick={item.onSelect}
              className={cn(
                "w-full min-h-[48px] flex items-center gap-3 px-3 py-2 text-left",
                "text-[15px] rounded-[var(--radius-card)] active:bg-[var(--bg-2)] transition-colors",
                item.selected
                  ? "text-[var(--amber-300)] font-medium"
                  : "text-[var(--fg-0)]",
              )}
            >
              <span className="flex-1">{item.label}</span>
              {item.hint ? (
                <span className="text-body-sm text-[var(--fg-2)]">
                  {item.hint}
                </span>
              ) : null}
              {item.selected ? (
                <span
                  aria-hidden
                  className="h-2.5 w-2.5 rounded-full bg-[var(--accent)]"
                />
              ) : null}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
