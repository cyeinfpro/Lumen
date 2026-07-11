"use client";

import { useCallback, useMemo, useState } from "react";

export type ComposerAttachmentRole =
  | "subject"
  | "product"
  | "style"
  | "reference"
  | "edit_target"
  | "ask_target";

interface ComposerAttachmentRoleOption {
  value: ComposerAttachmentRole;
  label: string;
  hint: string;
}

const COMPOSER_ATTACHMENT_ROLE_OPTIONS: ComposerAttachmentRoleOption[] = [
  { value: "reference", label: "参考", hint: "作为整体参考参与生成" },
  { value: "subject", label: "主体", hint: "保持人物、物体或构图主体" },
  { value: "product", label: "产品", hint: "保持商品款式、文字和细节" },
  { value: "style", label: "风格", hint: "借鉴摄影、配色或画风" },
  { value: "edit_target", label: "编辑目标", hint: "直接修改这张图" },
  { value: "ask_target", label: "询问对象", hint: "识别、分析并回答问题" },
];

const ROLE_ORDER = COMPOSER_ATTACHMENT_ROLE_OPTIONS.map((item) => item.value);

const ROLE_LABELS = Object.fromEntries(
  COMPOSER_ATTACHMENT_ROLE_OPTIONS.map((item) => [item.value, item.label]),
) as Record<ComposerAttachmentRole, string>;

interface AttachmentLike {
  id: string;
}

type ComposerMode = "chat" | "image";

type ManualRoleState = Record<
  string,
  {
    role: ComposerAttachmentRole;
  }
>;

function inferAttachmentRole(input: {
  mode: ComposerMode;
  attachmentId: string;
  maskTargetAttachmentId?: string | null;
}): ComposerAttachmentRole {
  if (input.mode === "chat") return "ask_target";
  if (input.maskTargetAttachmentId === input.attachmentId) return "edit_target";
  return "reference";
}

function nextRole(role: ComposerAttachmentRole): ComposerAttachmentRole {
  const index = ROLE_ORDER.indexOf(role);
  return ROLE_ORDER[(index + 1) % ROLE_ORDER.length] ?? "reference";
}

function pruneManualRoles(
  roles: ManualRoleState,
  activeIds: Set<string>,
): ManualRoleState {
  let changed = false;
  const next: ManualRoleState = {};
  for (const [id, value] of Object.entries(roles)) {
    if (!activeIds.has(id)) {
      changed = true;
      continue;
    }
    next[id] = value;
  }
  return changed ? next : roles;
}

export function attachmentRoleLabel(role: ComposerAttachmentRole): string {
  return ROLE_LABELS[role] ?? "参考";
}

export function attachmentRoleHint(role: ComposerAttachmentRole): string {
  return (
    COMPOSER_ATTACHMENT_ROLE_OPTIONS.find((item) => item.value === role)?.hint ??
    "作为参考参与当前请求"
  );
}

export function useComposerAttachmentRoles(input: {
  attachments: AttachmentLike[];
  mode: ComposerMode;
  maskTargetAttachmentId?: string | null;
}) {
  const { attachments, mode, maskTargetAttachmentId } = input;
  const [manualRoles, setManualRoles] = useState<ManualRoleState>({});
  const activeAttachmentIds = useMemo(() => {
    return new Set(attachments.map((attachment) => attachment.id));
  }, [attachments]);

  const activeManualRoles = useMemo(
    () => pruneManualRoles(manualRoles, activeAttachmentIds),
    [activeAttachmentIds, manualRoles],
  );

  const inferRole = useCallback(
    (attachmentId: string) =>
      inferAttachmentRole({ mode, attachmentId, maskTargetAttachmentId }),
    [maskTargetAttachmentId, mode],
  );

  const getRole = useCallback(
    (attachmentId: string) =>
      activeManualRoles[attachmentId]?.role ?? inferRole(attachmentId),
    [activeManualRoles, inferRole],
  );

  const setRole = useCallback(
    (attachmentId: string, role: ComposerAttachmentRole) => {
      setManualRoles((prev) => {
        if (!activeAttachmentIds.has(attachmentId)) return prev;
        return {
          ...pruneManualRoles(prev, activeAttachmentIds),
          [attachmentId]: { role },
        };
      });
    },
    [activeAttachmentIds],
  );

  const cycleRole = useCallback((attachmentId: string) => {
    setManualRoles((prev) => {
      if (!activeAttachmentIds.has(attachmentId)) return prev;
      const pruned = pruneManualRoles(prev, activeAttachmentIds);
      const current = pruned[attachmentId]?.role ?? inferRole(attachmentId);
      return {
        ...pruned,
        [attachmentId]: { role: nextRole(current) },
      };
    });
  }, [activeAttachmentIds, inferRole]);

  const entries = useMemo(
    () =>
      attachments.map((attachment, index) => ({
        id: attachment.id,
        index,
        role: getRole(attachment.id),
      })),
    [attachments, getRole],
  );

  const hasManualRoles = useMemo(
    () => entries.some((entry) => activeManualRoles[entry.id]),
    [activeManualRoles, entries],
  );

  const hint = useMemo(() => {
    if (attachments.length === 0) return null;
    if (hasManualRoles) return "附件角色已标注，可继续点标签切换用途";
    if (mode === "chat") return "默认识别为询问对象，点标签可切换用途";
    if (maskTargetAttachmentId) return "默认识别为编辑目标，点标签可切换用途";
    return "默认识别为参考图，点标签可切换主体/产品/风格";
  }, [attachments.length, hasManualRoles, maskTargetAttachmentId, mode]);

  const compactHint = useMemo(() => {
    if (attachments.length === 0) return null;
    if (hasManualRoles) return "点标签可继续切换用途";
    if (mode === "chat") return "默认询问对象，可切换";
    if (maskTargetAttachmentId) return "默认编辑目标，可切换";
    return "默认参考图，可切换";
  }, [attachments.length, hasManualRoles, maskTargetAttachmentId, mode]);

  return {
    entries,
    getRole,
    setRole,
    cycleRole,
    hint,
    compactHint,
    hasManualRoles,
  };
}
