"use client";

import dynamic from "next/dynamic";

import { Spinner } from "@/components/ui/primitives";
import type { MaskCanvasProps } from "./MaskCanvas";

const MaskCanvasImpl = dynamic(
  () => import("./MaskCanvas").then((mod) => mod.MaskCanvas),
  {
    ssr: false,
    loading: () => (
      <div
        className="fixed inset-0 z-[var(--z-dialog)] grid place-items-center bg-[var(--surface-scrim)] backdrop-blur-md"
        aria-busy="true"
        aria-label="加载局部修改画布"
      >
        <Spinner size={24} />
      </div>
    ),
  },
);

export function LazyMaskCanvas(props: MaskCanvasProps) {
  if (!props.open) return null;
  return <MaskCanvasImpl {...props} />;
}
