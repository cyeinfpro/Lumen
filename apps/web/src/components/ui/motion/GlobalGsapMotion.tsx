"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";

gsap.registerPlugin(useGSAP);
gsap.config({ nullTargetWarn: false });

const PAGE_ITEM_SELECTOR = [
  "[data-lumen-page-item]",
].join(",");

const CARD_SELECTOR = "[data-lumen-card='true']";

function isUsableElement(el: HTMLElement): boolean {
  if (
    el.closest(
      "[data-lumen-motion-skip='true'], [aria-hidden='true'], [hidden]",
    )
  ) {
    return false;
  }
  const style = window.getComputedStyle(el);
  return style.display !== "none" && style.visibility !== "hidden";
}

function collectMotionItems(
  root: HTMLElement,
  selector: string,
  maxItems: number,
): HTMLElement[] {
  const selected: HTMLElement[] = [];
  const candidates = Array.from(root.querySelectorAll<HTMLElement>(selector));
  for (const candidate of candidates) {
    if (!isUsableElement(candidate)) continue;
    if (selected.some((parent) => parent.contains(candidate))) continue;
    selected.push(candidate);
    if (selected.length >= maxItems) break;
  }
  return selected;
}

function closestMotionTarget(event: Event, root: HTMLElement): HTMLElement | null {
  const target = event.target;
  if (!(target instanceof Element)) return null;
  const el = target.closest<HTMLElement>(CARD_SELECTOR);
  if (!el || !root.contains(el)) return null;
  return el;
}

function crossedTargetBoundary(
  el: HTMLElement,
  relatedTarget: EventTarget | null,
): boolean {
  return !(relatedTarget instanceof Node) || !el.contains(relatedTarget);
}

export function GlobalGsapMotion({ children }: { children: ReactNode }) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const initialRouteRef = useRef(true);
  const pathname = usePathname();

  useGSAP(
    () => {
      const root = rootRef.current;
      if (!root) return;
      if (initialRouteRef.current) {
        initialRouteRef.current = false;
        return;
      }

      let mm: ReturnType<typeof gsap.matchMedia> | null = null;

      const page =
        root.querySelector<HTMLElement>("[data-lumen-motion-page]") ??
        root.querySelector<HTMLElement>("main") ??
        root;

      mm = gsap.matchMedia();
      mm.add(
        {
          reduceMotion: "(prefers-reduced-motion: reduce)",
        },
        (context) => {
          const conditions = context.conditions as
            | { reduceMotion?: boolean }
            | undefined;
          const reduceMotion = Boolean(conditions?.reduceMotion);
          if (reduceMotion) {
            gsap.set(page, {
              autoAlpha: 1,
              clearProps: "opacity,visibility",
            });
            return;
          }

          const pageItems = collectMotionItems(page, PAGE_ITEM_SELECTOR, 18);
          const tl = gsap.timeline({
            defaults: { ease: "power2.out", overwrite: "auto" },
          });

          tl.fromTo(
            page,
            { autoAlpha: 0.985 },
            {
              autoAlpha: 1,
              duration: 0.18,
              clearProps: "opacity,visibility",
            },
          );

          if (pageItems.length > 0) {
            tl.fromTo(
              pageItems,
              { autoAlpha: 0, y: 8 },
              {
                autoAlpha: 1,
                y: 0,
                duration: 0.28,
                stagger: { each: 0.02, from: "start" },
                clearProps: "transform,opacity,visibility",
              },
              "<0.03",
            );
          }
        },
      );

      return () => {
        mm?.revert();
        gsap.killTweensOf(page);
      };
    },
    { scope: rootRef, dependencies: [pathname], revertOnUpdate: true },
  );

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    let mm: ReturnType<typeof gsap.matchMedia> | null = gsap.matchMedia();
    mm.add(
      {
        pointerFine: "(hover: hover) and (pointer: fine)",
        reduceMotion: "(prefers-reduced-motion: reduce)",
      },
      (context) => {
        const conditions = context.conditions as
          | { pointerFine?: boolean; reduceMotion?: boolean }
          | undefined;
        if (!conditions?.pointerFine || conditions.reduceMotion) return;

        const onPointerOver = (event: PointerEvent) => {
          const el = closestMotionTarget(event, root);
          if (!el || !crossedTargetBoundary(el, event.relatedTarget)) return;
          gsap.to(el, {
            y: -2,
            duration: 0.16,
            ease: "power2.out",
            overwrite: "auto",
          });
        };

        const onPointerOut = (event: PointerEvent) => {
          const el = closestMotionTarget(event, root);
          if (!el || !crossedTargetBoundary(el, event.relatedTarget)) return;
          gsap.to(el, {
            y: 0,
            duration: 0.18,
            ease: "power2.out",
            overwrite: "auto",
            clearProps: "transform",
          });
        };

        root.addEventListener("pointerover", onPointerOver);
        root.addEventListener("pointerout", onPointerOut);
        root.addEventListener("pointercancel", onPointerOut);

        return () => {
          root.removeEventListener("pointerover", onPointerOver);
          root.removeEventListener("pointerout", onPointerOut);
          root.removeEventListener("pointercancel", onPointerOut);
          gsap.killTweensOf(root.querySelectorAll(CARD_SELECTOR));
        };
      },
    );

    return () => {
      mm?.revert();
      mm = null;
    };
  }, []);

  return (
    <div ref={rootRef} data-lumen-motion-root className="contents">
      {children}
    </div>
  );
}
