"use client";

import { type RefObject, useLayoutEffect, useState } from "react";
import { getActiveModalRoot, subscribeActiveModalRoot } from "./mobile/useModalLayer";
import { calculateNotificationPlacement } from "./notificationPlacement";

const OBSTACLE_SELECTOR = "[data-lumen-toast-obstacle], .mobile-dialog-footer";

function createToastHost(): HTMLDivElement {
  const host = document.createElement("div");
  host.dataset.lumenToastHost = "";
  Object.assign(host.style, {
    position: "fixed", inset: "0", pointerEvents: "none",
    zIndex: "var(--z-toast)",
    paddingTop: "env(safe-area-inset-top, 0px)",
    paddingRight: "env(safe-area-inset-right, 0px)",
    paddingBottom: "env(safe-area-inset-bottom, 0px)",
    paddingLeft: "env(safe-area-inset-left, 0px)",
  });
  return host;
}

function isVisibleObstacle(element: HTMLElement, host: HTMLElement): boolean {
  if (host.contains(element) || element.closest("[inert], [aria-hidden='true'], [hidden]")) return false;
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" &&
    style.visibility !== "collapse" && rect.width > 0 && rect.height > 0;
}

function readNotificationViewport(host: HTMLElement) {
  const visual = window.visualViewport ?? {
    offsetLeft: 0, offsetTop: 0, width: window.innerWidth, height: window.innerHeight,
  };
  const safe = window.getComputedStyle(host);
  const inset = (value: string) => Number.parseFloat(value) || 0;
  return {
    left: visual.offsetLeft + inset(safe.paddingLeft),
    top: visual.offsetTop + inset(safe.paddingTop),
    right: visual.offsetLeft + visual.width - inset(safe.paddingRight),
    bottom: visual.offsetTop + visual.height - inset(safe.paddingBottom),
  };
}

function measurePlacement(host: HTMLElement, viewport: HTMLElement) {
  const visibleBounds = readNotificationViewport(host);
  const width = visibleBounds.right - visibleBounds.left;
  const owner = getActiveModalRoot();
  const scope = owner ?? document;
  const obstacles = Array.from(scope.querySelectorAll<HTMLElement>(OBSTACLE_SELECTOR))
    .filter((element) => isVisibleObstacle(element, host))
    .map((element) => element.getBoundingClientRect());
  const placement = calculateNotificationPlacement({
    viewport: visibleBounds,
    owner: owner?.getBoundingClientRect(), obstacles,
    preferredWidth: width < 640 ? width : 320,
    gap: width < 640 ? 12 : 16,
  });
  const hostRect = host.getBoundingClientRect();
  // Dialog entry scales are rendered pixels; positioned CSS lengths are local
  // pixels. The conversion also handles translated bottom sheets and scroll.
  const scaleX = host.offsetWidth > 0 ? hostRect.width / host.offsetWidth : 1;
  const scaleY = host.offsetHeight > 0 ? hostRect.height / host.offsetHeight : 1;
  if (scaleX <= 0 || scaleY <= 0) return;
  Object.assign(viewport.style, {
    left: `${(placement.left - hostRect.left) / scaleX}px`,
    bottom: `${(hostRect.bottom - placement.bottom) / scaleY}px`,
    width: `${placement.width / scaleX}px`,
    maxHeight: `${placement.maxHeight / scaleY}px`,
    visibility: placement.width > 0 && placement.maxHeight > 0 ? "visible" : "hidden",
  });
}

function observePlacement(host: HTMLElement, viewport: HTMLElement): () => void {
  let frame = 0;
  let observed = new Set<Element>();
  const schedule = () => {
    if (frame) return;
    frame = window.requestAnimationFrame(() => {
      frame = 0;
      measurePlacement(host, viewport);
    });
  };
  const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
  const refresh = () => {
    const scope = getActiveModalRoot() ?? document;
    const next = new Set<Element>([host, viewport, ...scope.querySelectorAll(OBSTACLE_SELECTOR)]);
    for (const element of observed) if (!next.has(element)) resize?.unobserve(element);
    // Footer padding and safe-area changes may leave the content box unchanged.
    for (const element of next) if (!observed.has(element)) resize?.observe(element, { box: "border-box" });
    observed = next;
    schedule();
  };
  const mutations = new MutationObserver((records) => {
    // Ignore our placement styles and toast animation frames. ResizeObserver
    // still tracks stack/row size, including wrapping after a width change.
    if (records.some((record) => !host.contains(record.target))) refresh();
  });
  mutations.observe(document.body, {
    subtree: true, childList: true, attributes: true,
    attributeFilter: ["class", "style", "hidden", "inert", "data-lumen-toast-obstacle"],
  });
  window.addEventListener("resize", refresh);
  document.addEventListener("scroll", schedule, true);
  window.visualViewport?.addEventListener("resize", refresh);
  window.visualViewport?.addEventListener("scroll", schedule);
  refresh();
  measurePlacement(host, viewport);
  return () => {
    window.cancelAnimationFrame(frame);
    resize?.disconnect();
    mutations.disconnect();
    window.removeEventListener("resize", refresh);
    document.removeEventListener("scroll", schedule, true);
    window.visualViewport?.removeEventListener("resize", refresh);
    window.visualViewport?.removeEventListener("scroll", schedule);
  };
}

// Keep the portal target stable while moving its host. React preserves toast
// identity, remaining dismissal time, and action state across nested modals.
export function useToastPortal(
  viewportRef: RefObject<HTMLDivElement | null>,
  active: boolean,
): HTMLDivElement {
  const [host] = useState(createToastHost);
  useLayoutEffect(() => {
    let stopObserving: (() => void) | undefined;
    const moveHost = () => {
      stopObserving?.();
      const owner = getActiveModalRoot();
      const parent = owner ?? document.body;
      host.style.setProperty("position", owner ? "absolute" : "fixed");
      if (host.parentElement !== parent) parent.append(host);
      const viewport = viewportRef.current;
      if (active && viewport) stopObserving = observePlacement(host, viewport);
    };
    moveHost();
    const unsubscribe = subscribeActiveModalRoot(moveHost);
    return () => {
      unsubscribe();
      stopObserving?.();
      host.remove();
    };
  }, [active, host, viewportRef]);
  return host;
}
