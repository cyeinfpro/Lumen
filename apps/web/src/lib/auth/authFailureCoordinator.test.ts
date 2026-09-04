import { equal } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../../test-support/load-ts-module.mjs";

test("unauthorized coordination exposes recovery without forcing navigation", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  let sessionStatus = "authenticated";
  let invalidationReason = "";
  let cleanupCompleted = false;
  let notified = 0;
  let resolveCleanup: (() => void) | null = null;
  const cleanup = new Promise<void>((resolve) => {
    resolveCleanup = () => {
      cleanupCompleted = true;
      resolve();
    };
  });

  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { pathname: "/video" },
    },
  });

  try {
    const coordinator = loadTsModule(
      new URL("./authFailureCoordinator.ts", import.meta.url),
      {
        "../runtimeResilience": {
          requestSessionInvalidation(reason: string) {
            invalidationReason = reason;
            sessionStatus = "unauthorized";
          },
        },
        "./publicPaths": { isPublicPath: () => false },
        "./privateStateCleanup": {
          clearPrivateClientState() {
            return cleanup;
          },
        },
        "./sessionChangeBus": {
          notifyAuthSessionChanged() {
            notified += 1;
          },
        },
      },
    ) as { coordinateUnauthorized(): void };

    coordinator.coordinateUnauthorized();

    equal(sessionStatus, "unauthorized");
    equal(invalidationReason, "http_unauthorized");
    equal(notified, 1);
    equal(cleanupCompleted, false);

    const finishCleanup = resolveCleanup as unknown as (() => void) | null;
    if (!finishCleanup) throw new Error("cleanup resolver was not initialized");
    finishCleanup();
    await cleanup;

    equal(cleanupCompleted, true);
  } finally {
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", originalWindow);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
  }
});
