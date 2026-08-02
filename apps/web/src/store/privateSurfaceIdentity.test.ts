import assert from "node:assert/strict";
import test from "node:test";
import * as React from "react";
import { QueryClient } from "@tanstack/react-query";
import "./chat/moduleResolution.test-helper.mjs";

const { ApiError } = await import(
  new URL("../lib/api/errors.ts", import.meta.url).href
);
const { useIdentityRevalidation } = await import(
  new URL("../components/useIdentityRevalidation.ts", import.meta.url).href
);
const {
  clearPrivateClientState,
  activatePrivateClientState,
} = await import(
  new URL("../lib/auth/privateStateCleanup.ts", import.meta.url).href
);
const { getPrivateIdentitySnapshot } = await import(
  new URL("../lib/auth/privateIdentityEpoch.ts", import.meta.url).href
);
const { CLOSE_EVENT } = await import(
  new URL("../lib/lightbox/types.ts", import.meta.url).href
);
const { useInpaintStore } = await import(
  new URL("./useInpaintStore.ts", import.meta.url).href
);
const { useUiStore } = await import(
  new URL("./useUiStore.ts", import.meta.url).href
);
const { useChatStore } = await import(
  new URL("./useChatStore.ts", import.meta.url).href
);
const { requestSessionInvalidation } = await import(
  new URL("../lib/runtimeResilience.ts", import.meta.url).href
);

type HookDispatcher = {
  useContext: () => QueryClient;
  useState: <T>(initial: T) => [T, (next: T | ((value: T) => T)) => void];
  useRef: <T>(initial: T) => { current: T };
  useCallback: <T extends (...args: never[]) => unknown>(callback: T) => T;
  useEffect: (effect: () => void | (() => void)) => void;
  useLayoutEffect: (effect: () => void | (() => void)) => void;
};

type IdentityQueryForTest = {
  data?: { id: string };
  error: unknown;
  isFetching: boolean;
  refetch: (options?: { cancelRefetch?: boolean }) => Promise<{
    data?: { id: string };
    error?: unknown;
    status: string;
  }>;
};

function useIdentityRevalidationHarness(
  queryClient: QueryClient,
  query: IdentityQueryForTest,
): void {
  const internals = (
    React as unknown as {
      __CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE: {
        H: unknown;
      };
    }
  ).__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
  const previousDispatcher = internals.H;
  const stateValues: unknown[] = [];
  const refs: Array<{ current: unknown }> = [];
  const dispatcher: HookDispatcher = {
    useContext: () => queryClient,
    useState: <T>(initial: T) => {
      const index = stateValues.length;
      stateValues.push(initial);
      return [
        stateValues[index] as T,
        (next: T | ((value: T) => T)) => {
          stateValues[index] =
            typeof next === "function"
              ? (next as (value: T) => T)(stateValues[index] as T)
              : next;
        },
      ];
    },
    useRef: <T>(initial: T) => {
      const ref = { current: initial };
      refs.push(ref);
      return ref;
    },
    useCallback: <T extends (...args: never[]) => unknown>(callback: T) =>
      callback,
    useEffect: () => undefined,
    useLayoutEffect: (effect) => {
      effect();
    },
  };

  // eslint-disable-next-line react-hooks/immutability
  internals.H = dispatcher;
  try {
    useIdentityRevalidation({
      isPublicAuthPath: false,
      query,
    });
  } finally {
    internals.H = previousDispatcher;
  }
}

function useIdentityRevalidationHarnessWithEffects(
  queryClient: QueryClient,
  query: IdentityQueryForTest,
): () => void {
  const internals = (
    React as unknown as {
      __CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE: {
        H: unknown;
      };
    }
  ).__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
  const previousDispatcher = internals.H;
  const stateValues: unknown[] = [];
  const effects: Array<() => void | (() => void)> = [];
  const cleanups: Array<() => void> = [];
  const dispatcher: HookDispatcher = {
    useContext: () => queryClient,
    useState: <T>(initial: T) => {
      const index = stateValues.length;
      stateValues.push(initial);
      return [
        stateValues[index] as T,
        (next: T | ((value: T) => T)) => {
          stateValues[index] =
            typeof next === "function"
              ? (next as (value: T) => T)(stateValues[index] as T)
              : next;
        },
      ];
    },
    useRef: <T>(initial: T) => ({ current: initial }),
    useCallback: <T extends (...args: never[]) => unknown>(callback: T) =>
      callback,
    useEffect: (effect) => {
      effects.push(effect);
    },
    useLayoutEffect: (effect) => {
      effect();
    },
  };

  // eslint-disable-next-line react-hooks/immutability
  internals.H = dispatcher;
  try {
    useIdentityRevalidation({
      isPublicAuthPath: false,
      query,
    });
  } finally {
    internals.H = previousDispatcher;
  }
  for (const effect of effects) {
    const cleanup = effect();
    if (typeof cleanup === "function") cleanups.push(cleanup);
  }
  return () => {
    for (const cleanup of cleanups.reverse()) cleanup();
  };
}

test("private surfaces reset synchronously and reject stale lightbox epochs", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const eventTarget = new EventTarget();
  let closeEvents = 0;
  eventTarget.addEventListener(CLOSE_EVENT, () => {
    closeEvents += 1;
  });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: eventTarget,
  });

  try {
    await activatePrivateClientState("user-a");
    const userAIdentity = getPrivateIdentitySnapshot();
    assert.equal(userAIdentity.userId, "user-a");

    useInpaintStore.getState().openInpaint({
      imageId: "image-a",
      src: "data:image/png;base64,a",
    });
    useInpaintStore.getState().setDraft("image-a", "private prompt");
    useInpaintStore.getState().setMaskDraft("image-a", [
      {
        tool: "brush",
        points: [0, 0, 10, 10],
        radius: 12,
      },
    ]);
    useInpaintStore.getState().setSubmitting(true);

    useUiStore
      .getState()
      .openLightbox("image-a", "/images/a", "private image");
    useUiStore.setState({
      lightbox: {
        ...useUiStore.getState().lightbox,
        action: {
          label: "Select",
          pending: false,
          onClick() {},
        },
      },
    });

    const activation = activatePrivateClientState("user-b");
    const inpaintAfterSwitch = useInpaintStore.getState();
    assert.equal(inpaintAfterSwitch.ownerUserId, "user-b");
    assert.equal(inpaintAfterSwitch.open, false);
    assert.equal(inpaintAfterSwitch.source, null);
    assert.equal(inpaintAfterSwitch.submitting, false);
    assert.deepEqual(inpaintAfterSwitch.drafts, {});
    assert.deepEqual(inpaintAfterSwitch.maskDrafts, {});

    const uiAfterSwitch = useUiStore.getState();
    assert.equal(uiAfterSwitch.lightbox.ownerUserId, "user-b");
    assert.equal(uiAfterSwitch.lightbox.open, false);
    assert.equal(uiAfterSwitch.lightbox.action, null);
    await activation;

    useUiStore
      .getState()
      .openLightbox("image-b", "/images/b", "new user image");
    useUiStore.setState({
      lightbox: {
        ...useUiStore.getState().lightbox,
        action: {
          label: "New action",
          pending: false,
          onClick() {},
        },
      },
    });
    useUiStore.getState().setLightboxActionPending(true, userAIdentity);
    useUiStore.getState().closeLightbox(userAIdentity);

    const userBLightbox = useUiStore.getState().lightbox;
    assert.equal(userBLightbox.open, true);
    assert.equal(userBLightbox.imageId, "image-b");
    assert.equal(userBLightbox.action?.pending, false);

    await clearPrivateClientState();
    assert.equal(useInpaintStore.getState().ownerUserId, null);
    assert.equal(useUiStore.getState().lightbox.ownerUserId, null);
    assert.equal(useUiStore.getState().lightbox.open, false);

    useInpaintStore.getState().openInpaint({
      imageId: "public-image",
      src: "/images/public",
    });
    useUiStore
      .getState()
      .openLightbox("public-image", "/images/public", "public");
    assert.equal(useInpaintStore.getState().open, false);
    assert.equal(useUiStore.getState().lightbox.open, false);
    assert.equal(closeEvents, 3);
  } finally {
    await clearPrivateClientState();
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", originalWindow);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
  }
});

test("non-retryable revalidation failure clears private state through fail-closed handling", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const eventTarget = new EventTarget();
  Object.assign(eventTarget, { location: { pathname: "/studio" } });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: eventTarget,
  });

  try {
    await activatePrivateClientState("user-a");
    useChatStore.getState().setCurrentUser("user-a");
    useInpaintStore.getState().openInpaint({
      imageId: "image-a",
      src: "data:image/png;base64,a",
    });
    useInpaintStore.getState().setDraft("image-a", "private prompt");
    useUiStore.getState().openLightbox("image-a", "/images/a", "private");
    const before = getPrivateIdentitySnapshot();

    const queryClient = new QueryClient();
    useIdentityRevalidationHarness(queryClient, {
      data: { id: "user-a" },
      error: new ApiError({
        code: "forbidden",
        message: "identity rejected",
        status: 403,
      }),
      isFetching: false,
      refetch: async () => ({
        status: "error",
        error: new Error("unexpected refetch"),
      }),
    });

    const after = getPrivateIdentitySnapshot();
    assert.equal(after.userId, null);
    assert.equal(after.epoch, before.epoch + 1);
    assert.equal(useChatStore.getState().currentUserId, null);
    assert.equal(useInpaintStore.getState().ownerUserId, null);
    assert.equal(useInpaintStore.getState().open, false);
    assert.deepEqual(useInpaintStore.getState().drafts, {});
    assert.equal(useUiStore.getState().lightbox.ownerUserId, null);
    assert.equal(useUiStore.getState().lightbox.open, false);
  } finally {
    await clearPrivateClientState();
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", originalWindow);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
  }
});

test("session invalidation hides user A until a fresh user B identity resolves", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const originalBroadcastChannel = Object.getOwnPropertyDescriptor(
    globalThis,
    "BroadcastChannel",
  );
  const windowTarget = new EventTarget();
  const documentTarget = new EventTarget();
  Object.assign(windowTarget, { location: { pathname: "/studio" } });
  Object.assign(documentTarget, { visibilityState: "visible" });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: windowTarget,
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: documentTarget,
  });
  Object.defineProperty(globalThis, "BroadcastChannel", {
    configurable: true,
    value: undefined,
  });

  let resolveIdentity!: (result: {
    data?: { id: string };
    error?: unknown;
    status: string;
  }) => void;
  const pendingIdentity = new Promise<{
    data?: { id: string };
    error?: unknown;
    status: string;
  }>((resolve) => {
    resolveIdentity = resolve;
  });

  try {
    await activatePrivateClientState("user-a");
    useChatStore.getState().setCurrentUser("user-a");
    useInpaintStore.getState().openInpaint({
      imageId: "image-a",
      src: "data:image/png;base64,a",
    });
    const queryClient = new QueryClient();
    queryClient.setQueryData(["user", "user-a", "tasks"], ["task-a"]);
    let refetches = 0;
    const unmount = useIdentityRevalidationHarnessWithEffects(queryClient, {
      data: { id: "user-a" },
      error: null,
      isFetching: false,
      refetch: () => {
        refetches += 1;
        return pendingIdentity;
      },
    });

    requestSessionInvalidation("realtime_auth_invalidated");

    assert.equal(refetches, 1);
    assert.equal(useChatStore.getState().currentUserId, null);
    assert.equal(queryClient.getQueryData(["user", "user-a", "tasks"]), undefined);
    assert.equal(getPrivateIdentitySnapshot().userId, null);
    assert.equal(useInpaintStore.getState().open, false);

    resolveIdentity({ data: { id: "user-b" }, status: "success" });
    await Promise.resolve();
    await Promise.resolve();

    assert.equal(useChatStore.getState().currentUserId, "user-b");
    assert.equal(getPrivateIdentitySnapshot().userId, "user-b");
    unmount();
  } finally {
    await clearPrivateClientState();
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", originalWindow);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
    if (originalDocument) {
      Object.defineProperty(globalThis, "document", originalDocument);
    } else {
      Reflect.deleteProperty(globalThis, "document");
    }
    if (originalBroadcastChannel) {
      Object.defineProperty(
        globalThis,
        "BroadcastChannel",
        originalBroadcastChannel,
      );
    } else {
      Reflect.deleteProperty(globalThis, "BroadcastChannel");
    }
  }
});
