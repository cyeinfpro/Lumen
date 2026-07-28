import type { RecoveryReason } from "./contracts";

export type SnapshotScope =
  | "identity"
  | "conversations"
  | "activeTasks"
  | "wallet"
  | "runtimeDefaults"
  | "adminStatus";

export function scopesForRecovery(
  reason: RecoveryReason,
): readonly SnapshotScope[] {
  void reason;
  return [
    "identity",
    "conversations",
    "activeTasks",
    "wallet",
    "runtimeDefaults",
  ];
}
