"use client";

/* eslint-disable @next/next/no-img-element */

import {
  forwardRef,
  type MutableRefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { cn } from "@/lib/utils";

interface ViewportImageProps extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, "src" | "alt"> {
  src: string;
  alt: string;
  unloadWhenHidden?: boolean;
  persistAfterVisible?: boolean;
  /** 显式覆盖 rootMargin；不传则移动端 500px / 桌面端 1000px 自适应。 */
  rootMargin?: string;
}

export const ViewportImage = forwardRef<HTMLImageElement, ViewportImageProps>(
  function ViewportImage(
    {
      src,
      alt,
      unloadWhenHidden = true,
      persistAfterVisible = false,
      rootMargin,
      className,
      ...props
    },
    forwardedRef,
  ) {
    const imgRef = useRef<HTMLImageElement | null>(null);
    const [visible, setVisible] = useState(false);
    const [activated, setActivated] = useState(false);
    const setRefs = useCallback(
      (node: HTMLImageElement | null) => {
        imgRef.current = node;
        if (typeof forwardedRef === "function") {
          forwardedRef(node);
        } else if (forwardedRef) {
          (forwardedRef as MutableRefObject<HTMLImageElement | null>).current =
            node;
        }
      },
      [forwardedRef],
    );

    useEffect(() => {
      const node = imgRef.current;
      if (!node || typeof IntersectionObserver === "undefined") {
        setVisible(true);
        return;
      }
      // 响应式 rootMargin：移动端（窄屏）用较小值节省流量，桌面端提前预加载
      let margin = rootMargin;
      if (!margin) {
        const isMobile =
          typeof window !== "undefined" &&
          window.matchMedia?.("(max-width: 767px)").matches;
        // 窄屏节省流量：只预载视口下方约 1 屏；桌面端可以更激进
        margin = isMobile ? "250px 0px" : "1000px 0px";
      }
      const observer = new IntersectionObserver(
        ([entry]) => {
          const intersecting = entry.isIntersecting;
          setVisible(intersecting);
          if (intersecting) setActivated(true);
        },
        { root: null, rootMargin: margin, threshold: 0.01 },
      );
      observer.observe(node);
      return () => observer.disconnect();
    }, [rootMargin]);

    const activeSrc =
      visible || !unloadWhenHidden || (persistAfterVisible && activated)
        ? src
        : undefined;

    return (
      <img
        ref={setRefs}
        src={activeSrc}
        alt={alt}
        data-src={src}
        loading="lazy"
        decoding="async"
        className={cn(className, !activeSrc && "opacity-0")}
        {...props}
      />
    );
  },
);
