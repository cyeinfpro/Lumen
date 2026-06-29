"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";

gsap.registerPlugin(useGSAP, ScrollTrigger);
gsap.config({ nullTargetWarn: false });

const PAGE_ITEM_SELECTOR = [
  "[data-lumen-page-item]",
  ".surface-card",
  ".surface-panel",
  ".surface-dialog",
  ".stream-tile-shell",
  ".share-tile-shell",
  "article",
].join(",");

const REVEAL_SELECTOR = [
  "[data-lumen-reveal]",
  ".surface-card",
  ".surface-panel",
  ".surface-dialog",
  ".stream-tile-shell",
  ".share-tile-shell",
].join(",");

const INTERACTIVE_SELECTOR =
  "[data-lumen-interactive='true']:not(:disabled):not([aria-disabled='true'])";
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
  const el = target.closest<HTMLElement>(
    `${INTERACTIVE_SELECTOR}, ${CARD_SELECTOR}`,
  );
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
    (_context, contextSafe) => {
      const root = rootRef.current;
      if (!root) return;
      if (initialRouteRef.current) {
        initialRouteRef.current = false;
        return;
      }

      let rafOne = 0;
      let rafTwo = 0;
      let mm: ReturnType<typeof gsap.matchMedia> | null = null;
      const toContextSafe =
        contextSafe ?? ((fn: () => void) => fn);

      const runMotion = toContextSafe(() => {
        const page =
          root.querySelector<HTMLElement>("[data-lumen-motion-page]") ??
          root.querySelector<HTMLElement>("main") ??
          root;

        mm = gsap.matchMedia();
        mm.add(
          {
            reduceMotion: "(prefers-reduced-motion: reduce)",
            desktop: "(min-width: 768px)",
          },
          (context) => {
            const conditions = context.conditions as
              | { reduceMotion?: boolean; desktop?: boolean }
              | undefined;
            const reduceMotion = Boolean(conditions?.reduceMotion);
            if (reduceMotion) {
              gsap.set(page, {
                autoAlpha: 1,
                clearProps: "transform,opacity,visibility",
              });
              return;
            }

            const pageItems = collectMotionItems(page, PAGE_ITEM_SELECTOR, 18);
            const tl = gsap.timeline({
              defaults: { ease: "power2.out", overwrite: "auto" },
            });

            tl.fromTo(
              page,
              { autoAlpha: 0.985, y: 8, scale: 0.997 },
              {
                autoAlpha: 1,
                y: 0,
                scale: 1,
                duration: 0.28,
                clearProps: "transform,opacity,visibility",
              },
            );

            if (pageItems.length > 0) {
              tl.fromTo(
                pageItems,
                { autoAlpha: 0, y: 10 },
                {
                  autoAlpha: 1,
                  y: 0,
                  duration: 0.34,
                  stagger: { each: 0.025, from: "start" },
                  clearProps: "transform,opacity,visibility",
                },
                "<0.04",
              );
            }

            const revealItems = collectMotionItems(
              root,
              REVEAL_SELECTOR,
              80,
            ).filter((item) => !pageItems.includes(item));
            if (revealItems.length > 0) {
              ScrollTrigger.batch(revealItems, {
                start: "top 92%",
                once: true,
                interval: 0.08,
                batchMax: conditions?.desktop ? 10 : 6,
                onEnter: (batch) => {
                  gsap.fromTo(
                    batch,
                    { autoAlpha: 0, y: 16, scale: 0.99 },
                    {
                      autoAlpha: 1,
                      y: 0,
                      scale: 1,
                      duration: 0.42,
                      stagger: { each: 0.035, from: "start" },
                      ease: "power2.out",
                      overwrite: "auto",
                      clearProps: "transform,opacity,visibility",
                    },
                  );
                },
              });
              window.requestAnimationFrame(() => ScrollTrigger.refresh());
            }
          },
        );
      });

      rafOne = window.requestAnimationFrame(() => {
        rafTwo = window.requestAnimationFrame(runMotion);
      });

      return () => {
        window.cancelAnimationFrame(rafOne);
        window.cancelAnimationFrame(rafTwo);
        mm?.revert();
      };
    },
    { scope: rootRef, dependencies: [pathname], revertOnUpdate: true },
  );

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const pointerFine = window.matchMedia("(hover: hover) and (pointer: fine)");
    if (!pointerFine.matches) return;

    const onPointerOver = (event: PointerEvent) => {
      const el = closestMotionTarget(event, root);
      if (!el || !crossedTargetBoundary(el, event.relatedTarget)) return;
      const isCard = el.matches(CARD_SELECTOR);
      gsap.to(el, {
        y: isCard ? -3 : -1,
        scale: isCard ? 1.004 : 1.012,
        duration: 0.18,
        ease: "power2.out",
        overwrite: "auto",
      });
    };

    const onPointerOut = (event: PointerEvent) => {
      const el = closestMotionTarget(event, root);
      if (!el || !crossedTargetBoundary(el, event.relatedTarget)) return;
      gsap.to(el, {
        y: 0,
        scale: 1,
        duration: 0.22,
        ease: "power2.out",
        overwrite: "auto",
        clearProps: "transform",
      });
    };

    const onPointerDown = (event: PointerEvent) => {
      const el = closestMotionTarget(event, root);
      if (!el) return;
      gsap.to(el, {
        y: 0,
        scale: 0.965,
        duration: 0.08,
        ease: "power2.out",
        overwrite: "auto",
      });
    };

    const onPointerUp = (event: PointerEvent) => {
      const el = closestMotionTarget(event, root);
      if (!el) return;
      const isCard = el.matches(CARD_SELECTOR);
      gsap.to(el, {
        y: isCard ? -3 : -1,
        scale: isCard ? 1.004 : 1.012,
        duration: 0.16,
        ease: "power2.out",
        overwrite: "auto",
      });
    };

    root.addEventListener("pointerover", onPointerOver);
    root.addEventListener("pointerout", onPointerOut);
    root.addEventListener("pointerdown", onPointerDown);
    root.addEventListener("pointerup", onPointerUp);
    root.addEventListener("pointercancel", onPointerOut);

    return () => {
      root.removeEventListener("pointerover", onPointerOver);
      root.removeEventListener("pointerout", onPointerOut);
      root.removeEventListener("pointerdown", onPointerDown);
      root.removeEventListener("pointerup", onPointerUp);
      root.removeEventListener("pointercancel", onPointerOut);
    };
  }, []);

  return (
    <div ref={rootRef} data-lumen-motion-root className="contents">
      {children}
    </div>
  );
}
