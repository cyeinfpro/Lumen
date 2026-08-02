export interface PrivateIdentitySnapshot {
  userId: string | null;
  epoch: number;
}

export interface PrivateIdentityTransition extends PrivateIdentitySnapshot {
  changed: boolean;
}

let currentUserId: string | null = null;
let currentEpoch = 0;

function normalizeUserId(userId: string | null): string | null {
  const normalized = userId?.trim() ?? "";
  return normalized || null;
}

export function getPrivateIdentitySnapshot(): PrivateIdentitySnapshot {
  return {
    userId: currentUserId,
    epoch: currentEpoch,
  };
}

export function transitionPrivateIdentity(
  userId: string | null,
): PrivateIdentityTransition {
  const nextUserId = normalizeUserId(userId);
  const changed = currentUserId !== nextUserId;
  if (changed) {
    currentUserId = nextUserId;
    currentEpoch += 1;
  }
  return {
    userId: currentUserId,
    epoch: currentEpoch,
    changed,
  };
}

export function isPrivateIdentitySnapshotCurrent(
  snapshot: PrivateIdentitySnapshot,
): boolean {
  return (
    snapshot.userId !== null &&
    snapshot.userId === currentUserId &&
    snapshot.epoch === currentEpoch
  );
}
