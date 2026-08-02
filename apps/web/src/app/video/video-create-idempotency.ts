/**
 * 视频创建幂等键的按操作解析(纯函数,无 React/网络依赖)。
 *
 * 背景:歧义失败(网络中断/超时,服务端可能已建任务并预扣)后重提同一操作必须
 * 沿用同一 key,服务端按 (user, idempotency_key) + request fingerprint 回放已建
 * 任务,避免二次建任务与二次预扣/扣费。
 *
 * 旧实现把待定 key 存在 apiClient 模块级全局,导致不同请求、并发请求、或修改
 * 参数后的重提共享同一 key——后端指纹不同返回 409,跨账号/跨组件也会串扰。
 * 本模块把决策收敛为纯函数,由调用方(mutation 实例)以 useRef 持有「本次提交
 * 操作」的待定 key,并与 payload 指纹绑定:同参数重提复用,参数一变自动换新
 * key,成功或服务端明确拒绝后释放。
 */

export type PendingVideoCreateKey = {
  fingerprint: string;
  key: string;
} | null;

/** 与后端 request_fingerprint 同语义的规范化 payload(不含 idempotency_key)。 */
export function canonicalVideoCreatePayload(
  body: Record<string, unknown>,
): string {
  return JSON.stringify(sortKeys(body));
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortKeys);
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(record).sort()) {
      out[key] = sortKeys(record[key]);
    }
    return out;
  }
  return value;
}

/**
 * 解析本次提交使用的幂等键。
 *
 * - 待定 key 的指纹与本次 payload 一致(歧义失败后的同参数重提)→ 沿用,
 *   服务端回放已建任务,避免二次建任务与二次预扣/扣费;
 * - 否则(首次提交 / 参数已修改 / 上次已释放)→ 生成新 key。
 * 纯函数:并发与跨操作天然隔离,key 只随传入的 pending 状态流转。
 */
export function resolveVideoCreateIdempotencyKey(
  pending: PendingVideoCreateKey,
  body: Record<string, unknown>,
  freshKey: () => string,
): { key: string; pending: PendingVideoCreateKey } {
  const fingerprint = canonicalVideoCreatePayload(body);
  if (pending !== null && pending.fingerprint === fingerprint) {
    return { key: pending.key, pending };
  }
  const key = freshKey();
  return { key, pending: { fingerprint, key } };
}

/**
 * 提交结束后释放/保留待定 key:提交确认成功(error 为空)或服务端已返回明确
 * 错误(4xx/5xx,事务回滚或未建任务)时释放,下次提交视为新操作;仅网络/超时
 * 等歧义失败(无状态码)保留,供用户重提时回放。
 */
export function releaseVideoCreateIdempotencyKey(
  pending: PendingVideoCreateKey,
  error: unknown,
): PendingVideoCreateKey {
  if (pending === null) {
    return null;
  }
  if (error === null || error === undefined || isServerRejection(error)) {
    return null;
  }
  return pending;
}

export function isServerRejection(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    typeof (error as { status: unknown }).status === "number" &&
    (error as { status: number }).status >= 400
  );
}
