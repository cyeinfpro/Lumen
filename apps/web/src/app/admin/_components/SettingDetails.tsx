"use client";

import { AnimatePresence, motion } from "framer-motion";

export function SettingDetails({
  open,
  detail,
  settingKey,
  description,
  summary,
}: {
  open: boolean;
  detail?: string;
  settingKey: string;
  description?: string | null;
  summary: string;
}) {
  return (
    <AnimatePresence initial={false}>
      {open ? (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          exit={{ opacity: 0, height: 0 }}
          className="overflow-hidden"
        >
          <div className="mt-3 space-y-2 border-t border-[var(--border-subtle)] pt-3 type-caption text-[var(--fg-2)]">
            {detail ? <p>{detail}</p> : null}
            <p>
              技术名{" "}
              <code className="font-mono text-[var(--fg-1)]">
                {settingKey}
              </code>
            </p>
            {description && description !== summary ? (
              <p>{description}</p>
            ) : null}
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
