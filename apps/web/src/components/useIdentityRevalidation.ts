"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";

import {
  AUTH_USER_QUERY_KEY,
  clearPreviousUserQueryCache,
  prepareUserIdentityRevalidation,
} from "@/lib/queries/userScope";
import { ApiError, type AuthUser } from "@/lib/apiClient";
import { resumeSessionClientState } from "@/lib/api/http";
import { invalidateSessionClientState } from "@/lib/auth/authFailureCoordinator";
import { isPublicPath } from "@/lib/auth/publicPaths";
import { subscribeToAuthSessionChanges } from "@/lib/auth/sessionChangeBus";
import {
  registerRuntimeRecovery,
  registerSessionInvalidation,
  setSessionRuntimeStatus,
  type SessionRuntimeStatus,
} from "@/lib/runtimeResilience";
import { invalidateRealtimeRuntimes } from "@/shared/realtime/runtimeRegistry";
import { useChatStore } from "@/store/useChatStore";

const IDENTITY_REVALIDATION_RETRY_DELAYS_MS = [1_000, 3_000, 10_000, 30_000] as const;

type IdentityRefetchResult = {
  data?: AuthUser;
  error?: unknown;
  status: string;
};

type IdentityRefetch = (options?: {
  cancelRefetch?: boolean;
}) => Promise<IdentityRefetchResult>;

type IdentityRevalidationState = {
  generation: number;
  request: Promise<IdentityRefetchResult> | null;
  retryTimer: number | null;
  retryAttempt: number;
  retryDue: boolean;
  retainedUserId: string | null;
  handledError: unknown;
  terminal: boolean;
  requiresFreshIdentity: boolean;
};

type IdentityQuery = {
  data?: AuthUser;
  error: unknown;
  isFetching: boolean;
  refetch: IdentityRefetch;
};

function getIdentityRevalidationRetryDelay(attempt: number): number {
  const index = Math.min(
    Math.max(0, Math.trunc(attempt)),
    IDENTITY_REVALIDATION_RETRY_DELAYS_MS.length - 1,
  );
  return IDENTITY_REVALIDATION_RETRY_DELAYS_MS[index] ?? 30_000;
}

function isUnauthorizedIdentityError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

function isRetryableIdentityError(error: unknown): boolean {
  if (isUnauthorizedIdentityError(error)) return false;
  if (error instanceof Error && error.name === "AbortError") return false;
  if (!(error instanceof ApiError)) return true;
  return error.status === 0 || (error.status >= 500 && error.status <= 599);
}

function canRetainConfirmedIdentity(
  error: unknown,
  currentUserId: string | null,
  retainedUserId: string | null,
): currentUserId is string {
  return (
    isRetryableIdentityError(error) &&
    currentUserId !== null &&
    retainedUserId === currentUserId
  );
}

function isAuthUser(value: unknown): value is AuthUser {
  return (
    Boolean(value) &&
    typeof value === "object" &&
    typeof (value as { id?: unknown }).id === "string" &&
    (value as { id: string }).id.length > 0
  );
}

function isCurrentPathPublic(isPublicAuthPath: boolean): boolean {
  return (
    isPublicAuthPath ||
    (typeof window !== "undefined" && isPublicPath(window.location.pathname))
  );
}

function canAttemptIdentityRevalidation(): boolean {
  if (
    typeof document === "undefined" ||
    document.visibilityState !== "visible"
  ) {
    return false;
  }
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

function canStartIdentityRevalidation(input: {
  state: IdentityRevalidationState;
  force: boolean;
  isFetching: boolean;
  isPublicAuthPath: boolean;
}): boolean {
  const { state, force, isFetching, isPublicAuthPath } = input;
  if (state.terminal || state.request || (!force && isFetching)) return false;
  if (
    isCurrentPathPublic(isPublicAuthPath) ||
    !canAttemptIdentityRevalidation()
  ) {
    return false;
  }
  return force || state.retryTimer === null;
}

function retainedIdentityUserId(
  state: IdentityRevalidationState,
  currentUserId: string | null,
  queryUserId: string | undefined,
): string | null {
  return currentUserId ?? state.retainedUserId ?? queryUserId ?? null;
}

function shouldIgnoreIdentityFailure(input: {
  state: IdentityRevalidationState;
  error: unknown;
  generation?: number;
  request?: Promise<IdentityRefetchResult>;
}): boolean {
  const { state, error, generation, request } = input;
  if (generation !== undefined && state.generation !== generation) return true;
  if (request && state.request !== request) return true;
  return Boolean(
    !request &&
      (state.handledError === error || state.requiresFreshIdentity),
  );
}

function removeAuthUserQuery(queryClient: QueryClient): void {
  queryClient
    .getQueryCache()
    .find({ queryKey: AUTH_USER_QUERY_KEY, exact: true })
    ?.reset();
  queryClient.removeQueries({
    queryKey: AUTH_USER_QUERY_KEY,
    exact: true,
  });
}

export function useIdentityRevalidation({
  isPublicAuthPath,
  query,
}: {
  isPublicAuthPath: boolean;
  query: IdentityQuery;
}) {
  const queryClient = useQueryClient();
  const {
    data: queryData,
    error: queryError,
    isFetching,
    refetch,
  } = query;
  const [isolated, setIsolated] = useState(isPublicAuthPath);
  const [identityStatus, setIdentityStatus] =
    useState<SessionRuntimeStatus>(
      isPublicAuthPath ? "public" : queryData ? "authenticated" : "revalidating",
    );
  const stateRef = useRef<IdentityRevalidationState>({
    generation: 0,
    request: null,
    retryTimer: null,
    retryAttempt: 0,
    retryDue: false,
    retainedUserId: null,
    handledError: null,
    terminal: false,
    requiresFreshIdentity: false,
  });
  const runRef = useRef<(force?: boolean) => void>(() => undefined);

  const clearRetryTimer = useCallback(() => {
    const state = stateRef.current;
    if (state.retryTimer !== null) {
      window.clearTimeout(state.retryTimer);
      state.retryTimer = null;
    }
    state.retryDue = false;
  }, []);

  const resetRecovery = useCallback(
    (terminal: boolean, clearRetainedUser: boolean) => {
      const state = stateRef.current;
      state.generation += 1;
      state.request = null;
      state.retryAttempt = 0;
      state.handledError = null;
      state.terminal = terminal;
      state.requiresFreshIdentity = false;
      if (clearRetainedUser) state.retainedUserId = null;
      clearRetryTimer();
    },
    [clearRetryTimer],
  );

  const enterFailClosed = useCallback(
    (userId: string | null, error: unknown) => {
      const state = stateRef.current;
      state.retainedUserId ??= userId;
      useChatStore.getState().setCurrentUser(null);
      prepareUserIdentityRevalidation(queryClient, state.retainedUserId);
      if (!isRetryableIdentityError(error)) {
        void invalidateSessionClientState();
      }
      setIsolated(true);
    },
    [queryClient],
  );

  const invalidateIdentity = useCallback(() => {
    const currentUserId =
      useChatStore.getState().currentUserId ??
      stateRef.current.retainedUserId ??
      queryData?.id ??
      null;
    resetRecovery(true, true);
    useChatStore.getState().setCurrentUser(null);
    if (currentUserId) {
      clearPreviousUserQueryCache(queryClient, currentUserId);
    }
    removeAuthUserQuery(queryClient);
    void invalidateSessionClientState();
    setIsolated(true);
    setIdentityStatus("unauthorized");
  }, [queryClient, queryData?.id, resetRecovery]);

  const beginInvalidatedSessionRevalidation = useCallback(() => {
    if (isCurrentPathPublic(isPublicAuthPath)) return;
    const state = stateRef.current;
    const currentUserId =
      useChatStore.getState().currentUserId ??
      state.retainedUserId ??
      queryData?.id ??
      null;
    state.retainedUserId ??= currentUserId;
    resetRecovery(false, false);
    state.requiresFreshIdentity = true;
    invalidateRealtimeRuntimes();
    useChatStore.getState().setCurrentUser(null);
    prepareUserIdentityRevalidation(queryClient, state.retainedUserId);
    void invalidateSessionClientState();
    setSessionRuntimeStatus("revalidating");
    setIsolated(true);
    setIdentityStatus("revalidating");
    runRef.current(true);
  }, [isPublicAuthPath, queryClient, queryData?.id, resetRecovery]);

  const acceptIdentity = useCallback(
    (
      user: AuthUser,
      generation?: number,
      request?: Promise<IdentityRefetchResult>,
    ) => {
      const state = stateRef.current;
      if (generation !== undefined && state.generation !== generation) return;
      if (request && state.request !== request) return;
      if (isCurrentPathPublic(isPublicAuthPath)) return;
      if (state.requiresFreshIdentity && generation === undefined) return;

      const currentUserId = useChatStore.getState().currentUserId;
      if (currentUserId && currentUserId !== user.id) {
        // Another tab may have changed the shared session cookie. Clear the
        // old user's private cache before accepting the successful /auth/me
        // response, but retain that response as the new identity bootstrap.
        useChatStore.getState().setCurrentUser(null);
        clearPreviousUserQueryCache(queryClient, currentUserId);
      }

      state.request = null;
      state.retryAttempt = 0;
      state.retainedUserId = null;
      state.handledError = null;
      state.terminal = false;
      state.requiresFreshIdentity = false;
      clearRetryTimer();
      void resumeSessionClientState(user.id);
      useChatStore.getState().setCurrentUser(user.id);
      setIsolated(false);
      setIdentityStatus("authenticated");
    },
    [
      clearRetryTimer,
      isPublicAuthPath,
      queryClient,
    ],
  );

  const scheduleRetry = useCallback(() => {
    const state = stateRef.current;
    if (
      state.terminal ||
      state.request ||
      state.retryTimer !== null ||
      typeof window === "undefined"
    ) {
      return;
    }

    const delay = getIdentityRevalidationRetryDelay(state.retryAttempt);
    state.retryAttempt = Math.min(
      state.retryAttempt + 1,
      IDENTITY_REVALIDATION_RETRY_DELAYS_MS.length,
    );
    const generation = state.generation;
    state.retryTimer = window.setTimeout(() => {
      const current = stateRef.current;
      current.retryTimer = null;
      if (
        current.generation !== generation ||
        current.terminal ||
        isCurrentPathPublic(isPublicAuthPath)
      ) {
        return;
      }
      if (!canAttemptIdentityRevalidation()) {
        current.retryDue = true;
        return;
      }
      current.retryDue = false;
      runRef.current(true);
    }, delay);
  }, [isPublicAuthPath]);

  const handleFailure = useCallback(
    (
      error: unknown,
      generation?: number,
      request?: Promise<IdentityRefetchResult>,
    ) => {
      const state = stateRef.current;
      if (
        shouldIgnoreIdentityFailure({
          state,
          error,
          generation,
          request,
        })
      ) {
        return;
      }

      state.request = null;
      state.handledError = error;
      const currentUserId = useChatStore.getState().currentUserId;
      state.retainedUserId ??= retainedIdentityUserId(
        state,
        currentUserId,
        queryData?.id,
      );

      if (isCurrentPathPublic(isPublicAuthPath)) {
        resetRecovery(true, true);
        useChatStore.getState().setCurrentUser(null);
        removeAuthUserQuery(queryClient);
        setIsolated(true);
        setIdentityStatus("public");
        return;
      }
      if (isUnauthorizedIdentityError(error)) {
        invalidateIdentity();
        return;
      }

      if (
        canRetainConfirmedIdentity(
          error,
          currentUserId,
          state.retainedUserId,
        )
      ) {
        // A transient focus/visibility revalidation failure does not invalidate
        // the last confirmed identity. Keep the current UI mounted and retry in
        // the background; a later 401 or changed user still clears old data.
        setIsolated(false);
        setIdentityStatus("degraded");
        scheduleRetry();
        return;
      }

      enterFailClosed(state.retainedUserId, error);
      setIdentityStatus("degraded");
      if (isRetryableIdentityError(error)) scheduleRetry();
    },
    [
      enterFailClosed,
      invalidateIdentity,
      isPublicAuthPath,
      queryData?.id,
      queryClient,
      resetRecovery,
      scheduleRetry,
    ],
  );

  const revalidateIdentity = useCallback(
    (force = false) => {
      const state = stateRef.current;
      if (
        !canStartIdentityRevalidation({
          state,
          force,
          isFetching,
          isPublicAuthPath,
        })
      ) {
        return;
      }
      if (force) clearRetryTimer();

      const currentUserId = useChatStore.getState().currentUserId;
      const retainedUserId = retainedIdentityUserId(
        state,
        currentUserId,
        queryData?.id,
      );
      if (!retainedUserId && !queryData && !queryError) return;

      state.generation += 1;
      const generation = state.generation;
      state.retainedUserId = retainedUserId;
      state.handledError = null;
      setIdentityStatus("revalidating");

      let request: Promise<IdentityRefetchResult>;
      try {
        request = refetch({ cancelRefetch: true });
      } catch (error) {
        handleFailure(error, generation);
        return;
      }
      state.request = request;
      void request.then(
        (result) => {
          if (result.status === "success" && isAuthUser(result.data)) {
            acceptIdentity(result.data, generation, request);
            return;
          }
          handleFailure(
            result.error ?? new Error("identity revalidation failed"),
            generation,
            request,
          );
        },
        (error) => handleFailure(error, generation, request),
      );
    },
    [
      acceptIdentity,
      clearRetryTimer,
      handleFailure,
      isPublicAuthPath,
      isFetching,
      queryData,
      queryError,
      refetch,
    ],
  );

  useEffect(() => {
    runRef.current = revalidateIdentity;
    return () => {
      if (runRef.current === revalidateIdentity) {
        runRef.current = () => undefined;
      }
    };
  }, [revalidateIdentity]);

  useEffect(
    () =>
      registerRuntimeRecovery("session", () => {
        revalidateIdentity(true);
      }),
    [revalidateIdentity],
  );

  useEffect(
    () =>
      registerSessionInvalidation((reason) => {
        if (reason === "http_unauthorized") {
          invalidateIdentity();
          return;
        }
        beginInvalidatedSessionRevalidation();
      }),
    [beginInvalidatedSessionRevalidation, invalidateIdentity],
  );

  useEffect(() => {
    if (isPublicAuthPath) return;
    return subscribeToAuthSessionChanges(beginInvalidatedSessionRevalidation);
  }, [beginInvalidatedSessionRevalidation, isPublicAuthPath]);

  useEffect(() => {
    if (isPublicAuthPath) return;
    const resume = () => {
      const state = stateRef.current;
      revalidateIdentity(state.retryDue || state.retryTimer !== null);
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") resume();
    };

    window.addEventListener("focus", resume);
    window.addEventListener("online", resume);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.removeEventListener("focus", resume);
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [isPublicAuthPath, revalidateIdentity]);

  useLayoutEffect(() => {
    if (!isPublicAuthPath) return;
    resetRecovery(true, true);
    useChatStore.getState().setCurrentUser(null);
    removeAuthUserQuery(queryClient);
    void invalidateSessionClientState();
  }, [isPublicAuthPath, queryClient, resetRecovery]);

  useLayoutEffect(() => {
    if (isPublicAuthPath || stateRef.current.request) return;
    stateRef.current.terminal = false;
  }, [isPublicAuthPath, queryData, queryError]);

  useLayoutEffect(() => {
    const state = stateRef.current;
    if (
      isPublicAuthPath ||
      !queryData ||
      queryError ||
      state.request ||
      state.terminal ||
      state.requiresFreshIdentity
    ) {
      return;
    }
    acceptIdentity(queryData);
  }, [acceptIdentity, isPublicAuthPath, queryData, queryError]);

  useLayoutEffect(() => {
    if (
      isPublicAuthPath ||
      !queryError ||
      stateRef.current.request
    ) {
      return;
    }
    handleFailure(queryError);
  }, [handleFailure, isPublicAuthPath, queryError]);

  useEffect(
    () => () => {
      const state = stateRef.current;
      state.generation += 1;
      state.request = null;
      if (state.retryTimer !== null) {
        window.clearTimeout(state.retryTimer);
        state.retryTimer = null;
      }
    },
    [],
  );

  const effectiveIdentityStatus: SessionRuntimeStatus = isPublicAuthPath
    ? "public"
    : identityStatus === "public"
      ? "revalidating"
      : identityStatus;

  return {
    identityUnavailable: isPublicAuthPath || isolated,
    identityStatus: effectiveIdentityStatus,
  };
}
