"use client";

import dynamic from "next/dynamic";

const InpaintModalImpl = dynamic(
  () => import("./InpaintModal").then((mod) => mod.InpaintModal),
  {
    ssr: false,
    loading: () => (
      <div className="fixed inset-0 z-[var(--z-dialog)] bg-black/60" />
    ),
  },
);

export function LazyInpaintModal() {
  return <InpaintModalImpl />;
}
