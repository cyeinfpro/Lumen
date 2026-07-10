"use client";

import { useLayoutEffect, useRef, useState } from "react";

export function useElementBlockSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [blockSize, setBlockSize] = useState(0);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;

    const update = () => {
      const next = Math.ceil(node.getBoundingClientRect().height);
      setBlockSize((current) => (current === next ? current : next));
    };
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(node);
    window.addEventListener("resize", update);
    update();

    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", update);
    };
  }, []);

  return [ref, blockSize] as const;
}
