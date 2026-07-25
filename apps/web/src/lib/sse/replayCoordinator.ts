import type { RecoveryReason } from "./contracts";
import {
  scopesForRecovery,
  type SnapshotScope,
} from "./snapshotScopes";

export type SnapshotResult = {
  cursor?: string;
  syncedAt?: number;
};

export type SnapshotAdapter = (
  scopes: readonly SnapshotScope[],
  reason: RecoveryReason,
) => Promise<SnapshotResult>;

export class ReplayCoordinator {
  private flight: Promise<SnapshotResult> | null = null;
  private lastSuccessfulSync = 0;
  private readonly snapshot: SnapshotAdapter;

  constructor(snapshot: SnapshotAdapter) {
    this.snapshot = snapshot;
  }

  recover(reason: RecoveryReason): Promise<SnapshotResult> {
    if (this.flight) return this.flight;
    const flight = this.snapshot(scopesForRecovery(reason), reason)
      .then((result) => {
        this.lastSuccessfulSync = result.syncedAt ?? Date.now();
        return result;
      })
      .finally(() => {
        if (this.flight === flight) this.flight = null;
      });
    this.flight = flight;
    return flight;
  }

  lastSuccessfulSyncAt(): number {
    return this.lastSuccessfulSync;
  }
}
