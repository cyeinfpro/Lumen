"use client";

// 移动端只保留抽屉层级与动效，内容统一复用 Sidebar。

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { type ReactNode, useRef } from "react";
import { createPortal } from "react-dom";

import { Sidebar } from "@/components/ui/Sidebar";
import {
  useModalLayer,
  usePortalReady,
} from "@/components/ui/primitives/mobile/useModalLayer";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { DURATION, resolveDrawerMotion } from "@/lib/motion";

export interface MobileConversationDrawerProps {
  open: boolean;
  onClose: () => void;
  children?: ReactNode;
  ariaLabel?: string;
}

export function MobileConversationDrawer({
  open,
  onClose,
  children,
  ariaLabel = "会话列表",
}: MobileConversationDrawerProps) {
  const portalReady = usePortalReady();
  const reduceMotion = useReducedMotion();
  const drawerMotion = resolveDrawerMotion(reduceMotion, DURATION.normal);
  const panelRef = useRef<HTMLElement | null>(null);
  const onPanelKeyDown = useModalLayer({
    open,
    rootRef: panelRef,
    onClose,
  });

  useBodyScrollLock(open);

  if (!portalReady) return null;

  return createPortal(
    <AnimatePresence initial={false}>
      {open ? (
        <motion.div
          key="mobile-conversation-drawer"
          data-lumen-modal-layer
          className="fixed inset-0 z-[var(--z-dialog)]"
        >
          <motion.button
            type="button"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={drawerMotion.scrimTransition}
            onClick={onClose}
            aria-label="关闭会话列表"
            className="absolute inset-0 bg-[var(--surface-scrim)]"
          />
          <motion.aside
            ref={panelRef}
            tabIndex={-1}
            role="dialog"
            aria-modal="true"
            aria-label={ariaLabel}
            initial={drawerMotion.panelInitial}
            animate={drawerMotion.panelAnimate}
            exit={drawerMotion.panelExit}
            transition={drawerMotion.panelTransition}
            onKeyDown={onPanelKeyDown}
            className={[
              "absolute inset-y-0 left-0 flex min-h-0 max-h-[100dvh] flex-col overflow-hidden",
              "w-[var(--sidebar-panel-w)] max-w-[calc(100vw-var(--space-12))]",
              "border-r border-[var(--border-subtle)] bg-[var(--bg-1)] shadow-[var(--shadow-2)]",
              "pl-[env(safe-area-inset-left,0px)] pt-[env(safe-area-inset-top,0px)]",
              "pb-[env(safe-area-inset-bottom,0px)] focus-visible:outline-none",
            ].join(" ")}
          >
            {children ?? <Sidebar embedded showBrand onNavigate={onClose} />}
          </motion.aside>
        </motion.div>
      ) : null}
    </AnimatePresence>,
    document.body,
  );
}
