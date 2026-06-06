"use client";

import dynamic from "next/dynamic";

const InpaintModalImpl = dynamic(
  () => import("./InpaintModal").then((mod) => mod.InpaintModal),
  {
    ssr: false,
    loading: () => (
      <div
        className="fixed inset-0 z-[var(--z-dialog)] bg-black/60 grid place-items-center"
        aria-busy="true"
        aria-label="加载中"
      >
        <div
          className="h-8 w-8 animate-spin rounded-full border-2 border-white/30 border-t-white"
          aria-hidden
        />
      </div>
    ),
  },
);

export function LazyInpaintModal() {
  return <InpaintModalImpl />;
}
