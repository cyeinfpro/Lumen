"use client";

import dynamic from "next/dynamic";

import { Spinner } from "@/components/ui/primitives/Spinner";

const InpaintModalImpl = dynamic(
  () => import("./InpaintModal").then((mod) => mod.InpaintModal),
  {
    ssr: false,
    loading: () => (
      <div
        className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] grid place-items-center bg-[var(--surface-scrim)]"
        aria-busy="true"
        aria-label="加载中"
      >
        <Spinner size={24} className="text-[var(--media-control-fg)]" />
      </div>
    ),
  },
);

export function LazyInpaintModal() {
  return <InpaintModalImpl />;
}
