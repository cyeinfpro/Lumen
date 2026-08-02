import type { PrivateIdentitySnapshot } from "@/lib/auth/privateIdentityEpoch";

import type { LightboxItem } from "./types";

export interface MobileLightboxOpenState {
  ownerUserId: string;
  identityEpoch: number;
  items: LightboxItem[];
  currentId: string;
}

const EMPTY_PRIVATE_IDENTITY: PrivateIdentitySnapshot = {
  userId: null,
  epoch: 0,
};

export function mobileLightboxOpenIdentity(
  state: MobileLightboxOpenState,
): PrivateIdentitySnapshot {
  return {
    userId: state.ownerUserId,
    epoch: state.identityEpoch,
  };
}

export function mobileLightboxOpenStateMatchesIdentity(
  state: MobileLightboxOpenState,
  identity: PrivateIdentitySnapshot,
): boolean {
  return (
    state.ownerUserId === identity.userId &&
    state.identityEpoch === identity.epoch
  );
}

export function currentMobileLightboxIdentity(
  state: MobileLightboxOpenState | null,
): PrivateIdentitySnapshot {
  return state
    ? mobileLightboxOpenIdentity(state)
    : EMPTY_PRIVATE_IDENTITY;
}
