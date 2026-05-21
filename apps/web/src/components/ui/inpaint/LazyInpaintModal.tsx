"use client";

import dynamic from "next/dynamic";

const InpaintModalImpl = dynamic(
  () => import("./InpaintModal").then((mod) => mod.InpaintModal),
  {
    ssr: false,
    loading: () => null,
  },
);

export function LazyInpaintModal() {
  return <InpaintModalImpl />;
}
