"use client";

import type { ReactNode } from "react";
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
  return (
    <BottomSheet open={open} onClose={onClose} ariaLabel="操作面板">
      <div className="px-4 pt-1 pb-3">
        {(title || description) && (
          <div className="text-center py-3 border-b border-[var(--border-subtle)]">
            {title && (
              <div className="text-[15px] font-medium text-[var(--fg-0)]">
                {title}
              </div>
            )}
            {description && (
              <div className="mt-1 text-xs text-[var(--fg-2)]">
                {description}
              </div>
            )}
          </div>
        )}
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
                role="menuitem"
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
      <div className="px-4 pb-3 pt-3">
        <Pressable
          size="large"
          pressScale="soft"
          haptic="light"
          minHit={true}
          onPress={onClose}
          className="w-full h-14 rounded-2xl bg-[var(--bg-2)] border border-[var(--border-subtle)] text-[15px] font-medium text-[var(--fg-0)]"
        >
          {cancelLabel}
        </Pressable>
      </div>
    </BottomSheet>
  );
}
