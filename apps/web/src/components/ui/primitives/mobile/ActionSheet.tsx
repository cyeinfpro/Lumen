"use client";

import { useId, type ReactNode } from "react";
import { BottomSheet } from "./BottomSheet";
import { Pressable } from "./Pressable";

export interface ActionItem {
  key: string;
  label: ReactNode;
  icon?: ReactNode;
  destructive?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

export interface ActionSheetProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  actions: ActionItem[];
  cancelLabel?: string;
}

export function ActionSheet({
  open,
  onClose,
  title,
  description,
  actions,
  cancelLabel = "取消",
}: ActionSheetProps) {
  const titleId = useId();
  const descriptionId = useId();
  const accessibleLabel =
    typeof title === "string" && title.trim() ? title : "操作面板";

  return (
    <BottomSheet open={open} onClose={onClose} ariaLabel={accessibleLabel}>
      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto px-4 pt-1 pb-3">
        <div className="border-b border-[var(--border-subtle)] py-3 text-center">
          <h2 id={titleId} className="type-card-title">
            {title ?? "操作面板"}
          </h2>
          {description && (
            <p id={descriptionId} className="type-body-sm mt-1">
              {description}
            </p>
          )}
        </div>
        <ul className="flex flex-col">
          {actions.map((a) => (
            <li key={a.key} className="border-b border-[var(--border-subtle)] last:border-b-0">
              <Pressable
                size="large"
                pressScale="soft"
                haptic={a.destructive ? "warning" : "light"}
                minHit={true}
                disabled={a.disabled}
                onPress={() => {
                  a.onSelect();
                  onClose();
                }}
                className={[
                  "w-full h-14 px-3 justify-start text-left gap-3",
                  "text-[15px]",
                  a.destructive ? "text-[var(--danger)]" : "text-[var(--fg-0)]",
                ].join(" ")}
              >
                {a.icon && (
                  <span className="inline-flex w-5 h-5 items-center justify-center">
                    {a.icon}
                  </span>
                )}
                <span className="flex-1">{a.label}</span>
              </Pressable>
            </li>
          ))}
        </ul>
      </div>
      {/* spec §9.4：取消按钮单独一格，与 actions 之间 12px 间隙（iOS 标配） */}
      <div className="mobile-dialog-footer shrink-0 border-t border-[var(--border-subtle)] bg-[var(--bg-1)] px-4 pt-3">
        <Pressable
          size="large"
          pressScale="soft"
          haptic="light"
          minHit={true}
          onPress={onClose}
          className="h-14 w-full rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] text-[15px] font-medium text-[var(--fg-0)]"
        >
          {cancelLabel}
        </Pressable>
      </div>
    </BottomSheet>
  );
}
