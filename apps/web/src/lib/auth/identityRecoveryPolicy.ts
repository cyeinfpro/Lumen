import { ApiError } from "@/lib/api/errors";

// A terminal authentication failure must never become a background retry.
export function isUnauthorizedIdentityError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

export function isRetryableIdentityError(error: unknown): boolean {
  if (isUnauthorizedIdentityError(error)) return false;
  if (error instanceof Error && error.name === "AbortError") return false;
  if (!(error instanceof ApiError)) return true;
  return (
    error.status === 0 ||
    error.status === 408 ||
    error.status === 429 ||
    (error.status >= 500 && error.status <= 599)
  );
}

// Transient errors may retain only the same previously confirmed identity.
export function canRetainConfirmedIdentity(
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
