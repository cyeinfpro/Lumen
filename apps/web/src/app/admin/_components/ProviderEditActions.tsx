"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Plus, RotateCcw, Save } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";

export function ProviderEditActions({
  open,
  draftCount,
  saving,
  onAdd,
  onCancel,
  onSave,
}: {
  open: boolean;
  draftCount: number;
  saving: boolean;
  onAdd: () => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 30 }}
          transition={{ duration: 0.2 }}
          className="fixed bottom-0 left-0 right-0 z-40 max-w-full px-4 pb-[env(safe-area-inset-bottom)] sm:bottom-4 sm:left-1/2 sm:right-auto sm:w-auto sm:max-w-[calc(100vw-2rem)] sm:-translate-x-1/2 sm:px-0 sm:pb-4"
        >
          <div className="grid grid-cols-2 items-stretch gap-2 rounded-[var(--radius-dialog)] border border-[var(--color-lumen-amber)]/40 bg-[var(--bg-1)]/95 px-3 py-2.5 shadow-[var(--shadow-3)] backdrop-blur-xl sm:flex sm:items-center sm:gap-3 sm:px-4">
            <span className="col-span-2 min-w-0 type-caption text-[var(--fg-1)] sm:col-span-1 sm:whitespace-nowrap">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-lumen-amber)] shadow-[var(--shadow-amber)]" />
                编辑中
                <span className="text-[var(--fg-2)]">·</span>
                <span className="font-mono tabular-nums">{draftCount}</span>
                <span>个供应商</span>
              </span>
            </span>
            <div className="hidden flex-1 sm:block sm:flex-none" />
            <Button
              variant="secondary"
              size="sm"
              onClick={onAdd}
              disabled={saving}
              leftIcon={<Plus className="h-3 w-3" />}
            >
              <span className="hidden sm:inline">添加</span>
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={onCancel}
              disabled={saving}
              leftIcon={<RotateCcw className="h-3 w-3" />}
            >
              放弃
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={onSave}
              disabled={saving}
              loading={saving}
              leftIcon={saving ? undefined : <Save className="h-3 w-3" />}
            >
              {saving ? copy.state.saving : copy.action.save}
            </Button>
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
