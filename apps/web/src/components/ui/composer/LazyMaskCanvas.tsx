"use client";

import dynamic from "next/dynamic";

import type { MaskCanvasProps } from "./MaskCanvas";

const MaskCanvasImpl = dynamic(
  () => import("./MaskCanvas").then((mod) => mod.MaskCanvas),
  {
    ssr: false,
    loading: () => (
      <div
        className="fixed inset-0 z-[var(--z-dialog)] grid place-items-center bg-black/72 backdrop-blur-md"
        aria-busy="true"
        aria-label="加载局部修改画布"
      >
        <div
          className="h-8 w-8 animate-spin rounded-full border-2 border-white/30 border-t-white"
          aria-hidden
        />
      </div>
    ),
  },
);

export function LazyMaskCanvas(props: MaskCanvasProps) {
  if (!props.open) return null;
  return <MaskCanvasImpl {...props} />;
}
