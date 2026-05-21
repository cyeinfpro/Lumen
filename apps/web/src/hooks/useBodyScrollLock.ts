"use client";

import { useEffect } from "react";

type OverscrollBehaviorValue = "auto" | "contain" | "none";

interface BodyScrollLockOptions {
  bodyOverscrollBehavior?: OverscrollBehaviorValue;
  documentOverscrollBehavior?: OverscrollBehaviorValue;
}

type ScrollLockSnapshot = {
  bodyOverflow: string;
  bodyOverscrollBehavior: string;
  documentOverscrollBehavior: string;
};

const activeLocks = new Map<number, BodyScrollLockOptions>();

let lockIdSeed = 0;
let snapshot: ScrollLockSnapshot | null = null;

function readDocumentParts():
  | { body: HTMLElement; documentElement: HTMLElement }
  | null {
  if (typeof document === "undefined") return null;
  return {
    body: document.body,
    documentElement: document.documentElement,
  };
}

function applyActiveLockStyles(): void {
  const parts = readDocumentParts();
  if (!parts) return;
  const { body, documentElement } = parts;

  body.style.overflow = "hidden";

  let bodyOverscrollBehavior: OverscrollBehaviorValue | undefined;
  let documentOverscrollBehavior: OverscrollBehaviorValue | undefined;
  for (const options of activeLocks.values()) {
    bodyOverscrollBehavior =
      options.bodyOverscrollBehavior ?? bodyOverscrollBehavior;
    documentOverscrollBehavior =
      options.documentOverscrollBehavior ?? documentOverscrollBehavior;
  }

  body.style.overscrollBehavior =
    bodyOverscrollBehavior ?? snapshot?.bodyOverscrollBehavior ?? "";
  documentElement.style.overscrollBehavior =
    documentOverscrollBehavior ?? snapshot?.documentOverscrollBehavior ?? "";
}

function restoreSnapshot(): void {
  const parts = readDocumentParts();
  if (!parts) return;
  const { body, documentElement } = parts;

  body.style.overflow = snapshot?.bodyOverflow ?? "";
  body.style.overscrollBehavior = snapshot?.bodyOverscrollBehavior ?? "";
  documentElement.style.overscrollBehavior =
    snapshot?.documentOverscrollBehavior ?? "";
  snapshot = null;
}

export function acquireBodyScrollLock(
  options: BodyScrollLockOptions = {},
): () => void {
  const parts = readDocumentParts();
  if (!parts) return () => {};

  if (activeLocks.size === 0) {
    snapshot = {
      bodyOverflow: parts.body.style.overflow,
      bodyOverscrollBehavior: parts.body.style.overscrollBehavior,
      documentOverscrollBehavior:
        parts.documentElement.style.overscrollBehavior,
    };
  }

  const lockId = lockIdSeed + 1;
  lockIdSeed = lockId;
  activeLocks.set(lockId, options);
  applyActiveLockStyles();

  let released = false;
  return () => {
    if (released) return;
    released = true;
    activeLocks.delete(lockId);
    if (activeLocks.size === 0) {
      restoreSnapshot();
    } else {
      applyActiveLockStyles();
    }
  };
}

export function useBodyScrollLock(
  locked: boolean,
  options: BodyScrollLockOptions = {},
): void {
  const {
    bodyOverscrollBehavior,
    documentOverscrollBehavior,
  } = options;

  useEffect(() => {
    if (!locked) return;
    return acquireBodyScrollLock({
      bodyOverscrollBehavior,
      documentOverscrollBehavior,
    });
  }, [bodyOverscrollBehavior, documentOverscrollBehavior, locked]);
}
