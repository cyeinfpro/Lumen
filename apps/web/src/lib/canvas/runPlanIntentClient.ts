import type { SemanticIdempotencyStore, SemanticIdempotencyLease } from "../api/semanticIdempotency";
import type { CanvasPendingPlanIntent, CanvasPlanIntent, CanvasPlanReceipt, CanvasPlanRunDetail } from "./runPlanTypes";
import { assertCanvasPlanIntent } from "./runPlanValidation";

type Journal = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type Leases = Pick<SemanticIdempotencyStore, "acquire" | "pendingKey" | "markSubmitted" | "confirm" | "confirmPendingKey" | "recordFailure" | "discard">;
interface Dependencies {
  leases: Leases;
  journal: () => Journal;
  currentIdentity: () => string;
  identityEpoch: () => number;
  query: (pending: CanvasPendingPlanIntent) => Promise<CanvasPlanReceipt>;
  post: (pending: CanvasPendingPlanIntent) => Promise<CanvasPlanRunDetail>;
  ambiguous: (error: unknown) => boolean;
}
const scope = (canvasId: string) => ({ operation: "canvas.batch.admission", canvasId });
const storageKey = (key: string) => `lumen.canvas.batch.intent.v1:${key}`;
export class CanvasPlanPendingError extends Error {
  readonly pending: CanvasPendingPlanIntent;
  constructor(pending: CanvasPendingPlanIntent, message = "提交状态待确认，请查询原任务，勿创建新任务") { super(message); this.pending = pending; }
}
export class CanvasPlanIntentClient {
  private readonly deps: Dependencies;
  constructor(deps: Dependencies) { this.deps = deps; }
  private identityToken() { return JSON.stringify([this.deps.currentIdentity(), this.deps.identityEpoch()]); }
  private read(canvasId: string, key: string): CanvasPendingPlanIntent {
    const raw = this.deps.journal().getItem(storageKey(key));
    if (!raw) throw new Error("原提交意图日志暂不可用，禁止创建新的付费请求");
    const pending = JSON.parse(raw) as CanvasPendingPlanIntent;
    if (pending.key !== key || pending.canvasId !== canvasId) throw new Error("提交意图日志不匹配");
    assertCanvasPlanIntent(pending.intent);
    return pending;
  }
  async pending(canvasId: string): Promise<CanvasPendingPlanIntent | null> {
    this.deps.currentIdentity();
    const key = await this.deps.leases.pendingKey(scope(canvasId), {});
    return key ? this.read(canvasId, key) : null;
  }
  async resolve(pending: CanvasPendingPlanIntent): Promise<CanvasPlanRunDetail | null> {
    const identity = this.identityToken();
    const receipt = await this.deps.query(pending);
    if (this.identityToken() !== identity) throw new Error("登录身份已变化");
    if (!receipt.admitted || !receipt.run) return null;
    await this.deps.leases.confirmPendingKey(this.deps.currentIdentity(), pending.key);
    // Keep the immutable receipt body for in-flight borrowers of this key.
    return receipt.run;
  }
  async submit(canvasId: string, intent: CanvasPlanIntent): Promise<CanvasPlanRunDetail> {
    assertCanvasPlanIntent(intent);
    const identity = this.identityToken();
    const lease = await this.deps.leases.acquire(scope(canvasId), {});
    if (this.identityToken() !== identity) throw new Error("登录身份已变化");
    if (lease.ownership === "borrowed") {
      const pending = this.read(canvasId, lease.key);
      const run = await this.resolve(pending);
      if (run) return run;
      throw new CanvasPlanPendingError(pending);
    }
    const pending = JSON.parse(JSON.stringify({ canvasId, key: lease.key, intent })) as CanvasPendingPlanIntent;
    try {
      const journal = this.deps.journal(), serialized = JSON.stringify(pending);
      journal.setItem(storageKey(lease.key), serialized);
      if (journal.getItem(storageKey(lease.key)) !== serialized) throw new Error("无法持久保存原提交意图");
    } catch (error) {
      await this.deps.leases.discard(lease);
      throw error;
    }
    return this.send(pending, lease, identity);
  }
  // Explicit recovery only: query first, then replay the exact journal body/key.
  async replay(pending: CanvasPendingPlanIntent): Promise<CanvasPlanRunDetail> {
    const identity = this.identityToken();
    const found = await this.resolve(pending);
    if (found) return found;
    const current = await this.pending(pending.canvasId);
    if (!current || current.key !== pending.key) throw new Error("原提交已变化，请查询最新状态");
    const lease = await this.deps.leases.acquire(scope(pending.canvasId), {});
    if (lease.key !== pending.key) {
      await this.deps.leases.discard(lease);
      throw new Error("原提交已结束，不能重放");
    }
    return this.send(current, lease, identity);
  }
  private async send(pending: CanvasPendingPlanIntent, lease: SemanticIdempotencyLease, identity: string): Promise<CanvasPlanRunDetail> {
    try {
      await this.deps.leases.markSubmitted(lease);
      if (this.identityToken() !== identity) throw new Error("登录身份已变化");
      const run = await this.deps.post(pending);
      if (this.identityToken() !== identity) throw new Error("登录身份已变化");
      await this.deps.leases.confirm(lease);
      return run;
    } catch (error) {
      await this.deps.leases.recordFailure(lease, error);
      if (this.identityToken() !== identity) throw error;
      if (!this.deps.ambiguous(error)) throw error;
      try {
        const found = await this.resolve(pending);
        if (found) return found;
      } catch { /* A failed GET never authorizes another POST. */ }
      throw new CanvasPlanPendingError(pending, error instanceof Error ? error.message : undefined);
    }
  }
}
