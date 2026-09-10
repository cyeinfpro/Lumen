# Lumen 深度代码审计与前端产品化重构方案

> 仓库：`cyeinfpro/Lumen`\
> 审计基线：`3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026`，`main` 的本轮取样提交\
> 提交时间：2026-09-05 02:13:08 UTC（北京时间 / 台北时间 10:13:08）\
> 报告日期：2026-09-05，Asia/Taipei\
> 交付范围：代码问题、修复参考代码、回归方案、前端信息架构与视觉 / 交互重构方案。**未修改远程仓库。**

导航：[结论](#overview) · [问题总表](#findings) · [修复详情](#bugs) · [整体设计](#design) · [逐页方案](#pages) · [组件代码](#implementation) · [结构重构](#architecture) · [测试矩阵](#testing) · [迁移计划](#migration) · [审计范围](#coverage) · [复核脚本](#reproductions)

---

<a id="overview"></a>

## 0. 先说结论

Lumen 不需要再套一层“AI 产品模板”，也不适合直接推倒重写。当前代码已经具备语义设计令牌、共享按钮和弹窗、请求身份校验、持久化幂等机制、Agent 原生执行策略、画布草稿恢复等基础。更值得投入的是：**把这些基础真正接到所有业务入口上，减少交互中的不确定性，再统一工作台的信息层级。**

本轮最值得先处理的事情是：

1. **Agent 的“同一次操作”没有贯穿手动重试。** 自动重试使用原键，但重新点击发送会生成新键；在服务端已经接收而客户端没有收到确认的场景，存在重复创建任务的条件。与此同时，Agent 对“交付结果不确定”的分类比通用请求层更窄。
2. **Runtime 停机期间的请求准入存在异步边界漏洞。** 正文读取前通过的请求，可能在排空开始后继续启动任务。
3. **画布投影排序、命令面板、焦点管理和认证异常输入有可定位的边界缺陷。** 这些不是“感觉不高级”，而是可以用代码路径和最小例子说明的问题。
4. **UI 的主要改造方向不是增加装饰，而是减轻持续暴露的复杂度。** 保留品牌识别，把参数从主操作区分流，把执行状态和可恢复动作放到明确位置，让内容而不是容器成为视觉中心。

### 0.1 本报告的证据边界

本轮通过 GitHub 连接读取固定提交下的代码、部分目录树与测试入口，并沿关键调用链交叉核对。随后在隔离环境执行了 **8 个 JavaScript 算法 / 控制流检查和 4 组 Python / CSS 选择器 / 对比度检查**。

这些检查验证的是抽取出的逻辑、标准库行为和颜色计算，**不是 Lumen 全仓测试，不是已启动产品后的端到端复现**。当前环境没有取得可执行的完整仓库副本，没有运行完整构建、数据库迁移、真实供应商调用或浏览器截图测试。

因此，报告不声称“扫描了全部文件的每一行”“发现了所有 Bug”“修复后绝无问题”或“当前线上已经发生重复扣费”。未深入到的 Worker、完整账务链、部署脚本等范围列在附录中，不能把未发现问题理解为这些模块已通过审计。

### 0.2 证据等级与代码标记

| 标记 | 含义 |
| --- | --- |
| S | 已读取相关实现，代码直接支持这一结论 |
| R | 隔离最小检查验证了相应逻辑；不等于真实应用复现 |
| I | 后果还需要浏览器、数据库、进程或真实网络环境联调确认 |
| D | 产品设计建议，属于方案选择，不混算为程序 Bug |

文中的代码分两类：**局部替换代码**尽量贴近当前函数；**接入参考代码**说明模块边界与实现方式，需要与现有类型、调用点、测试整合。除附录的最小检查脚本外，没有把建议代码伪装成已在完整项目编译通过的补丁。

---

<a id="findings"></a>

## 1. 问题总览与处理优先级

P1 表示应优先处理的重复执行 / 生命周期风险；P2 表示功能、数据投影、认证健壮性或可访问性问题；P3 表示潜在契约和构建维护问题。**本轮未确认 P0 级漏洞。**

| 编号 | 优先级 | 问题 | 证据 | 应如何理解影响 |
| --- | --- | --- | --- | --- |
| [B01](#b01) | P1 | Agent 手动重试不延续同一逻辑操作键，交付不确定分类不统一 | S / R / I | 特定丢响应条件下可重复提交；未证明线上重复扣费 |
| [B02](#b02) | P1 | Runtime 读取正文后未重新检查 draining | S / R / I | 排空阶段可接受此前在读正文的新任务 |
| [B03](#b03) | P2 | 可选全局组件异常后被永久隐藏，缺少局部恢复入口 | S / I | 该边界存续期间功能消失，通常需重载或重新挂载 |
| [B04](#b04) | P2 | 兼容 API 封装把 HEAD 转成 GET | S / R | 接口契约错误；未证明当前有业务调用触发 |
| [B05](#b05) | P2 | 非 ASCII 认证字符串可使 compare_digest 抛 TypeError | S / R / I | 应拒绝的异常凭证可能走服务器错误；不是认证绕过 |
| [B06](#b06) | P2 | 弹窗焦点候选集错误包含负 tabindex 或 disabled 元素 | S / R / I | 与命令面板的复合控件焦点模型冲突 |
| [B07](#b07) | P2 | 命令面板在中文输入法组合期间处理 Escape | S / R / I | 取消候选词可能变成关闭整个面板 |
| [B08](#b08) | P2 | 命令面板方向键选择没有同步可见区域 | S / I | 焦点仍在输入框时，选中结果可移出滚动视口 |
| [B09](#b09) | P2 | 画布投影用负无穷相减排序，产生 NaN | S / R / I | 同版本数据合并可能丢失本应保留的客户端投影项 |
| [B10](#b10) | P3 | 语义指纹规范化丢弃自有 __proto__ 键 | S / R | 一般 JSON 契约缺陷；业务入口可达性需再核查 |
| [B11](#b11) | P3 | Tailwind 显式扫描路径错误，且没有关闭自动扫描 | S / R | 配置意图与实际不同；不等于全部样式缺失 |
| [B12](#b12) | P2 | 浅色链接和部分元信息使用不适合文字的颜色令牌 | S / R / I | 已计算的令牌组合低于常规正文对比度要求 |

建议先交付 B01、B02、B09，再处理 B03～B08 与 B12；B10、B11 可进入同一轮小型契约修正。UI 改造可以并行建立样板页，但不应与请求、账务、事件版本协议一次性混在同一个大提交中。

---

<a id="bugs"></a>

## 2. Bug 详情、建议代码与回归方法

<a id="b01"></a>

### B01 · Agent 的手动重试没有复用逻辑操作身份

**位置与证据**

- `apps/web/src/features/agent/containers/AgentWorkspaceController.tsx`：`submit`、`continueFrom`。
- `apps/web/src/features/agent/containers/agentSubmission.ts`：`postAgentMessageWithTransportRetry`、`agentSubmissionDeliveryIsUncertain`、`reconcileFailedAgentSubmission`。
- `apps/web/src/features/agent/api/agentApi.ts`：`postAgentMessage`、`continueAgentRun`。
- `apps/web/src/lib/api/semanticIdempotencyRequest.ts`：`idempotentPostRequest`。
- `apps/web/src/lib/api/semanticIdempotency.ts`：已有 `withSemanticPostIdempotency`。
- `apps/api/app/services/agent/message_submission.py`：`_idempotent_agent_message`、`_stage_submission`。

[前端控制器](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/features/agent/containers/AgentWorkspaceController.tsx) · [提交辅助函数](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/features/agent/containers/agentSubmission.ts) · [后端去重实现](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/api/app/services/agent/message_submission.py)

**根因**

`submit` 每次执行都会创建新的 `uniqueAgentId("agent-message")`。单次函数内部的自动网络重试确实复用同一个 body，不能说“完全没有幂等”。但是函数失败返回后，用户再次发送同一份草稿，会拿到一个新键。

`idempotentPostRequest` 只负责把键写到请求头和 body，不负责持久化或找回以前的操作。后端先按用户、会话、幂等键查找任务，再检查请求指纹；因此，新键不会被当成旧操作的重放。

另一个相关问题是：Agent 的不确定分类只覆盖 status 0、`network_error` 和 `request_timeout`，没有复用通用层对 408、425、429、5xx 等保守分类。例如代理在服务端提交后返回 504，不足以证明任务没有发生，但当前 Agent 逻辑可将其当作确定失败，移除乐观消息。

**触发条件**

服务端提交成功 → 返回确认丢失 → 自动重试仍未拿到确认 → 客户端恢复失败提示 → 原任务已结束，或当前快照暂未反映它 → 用户再点发送。此时新键允许第二个逻辑上重复的任务被创建。

“继续”入口也会生成新键，建议统一治理；但本轮没有完整核对 continuation 的服务端唯一性约束，不把重复 continuation 当作已证明的后果。

后端已有 active-run 检查，能够阻止部分并发场景；它不能替代跨完成状态、跨刷新和跨丢响应的逻辑幂等。费用影响是额外执行可能带来的成本 / 预留，不应未经账务联调就写成“已确认重复扣费”。

**建议代码：复用现有持久化机制，不新造一套重试系统**

下面作为 Agent API 服务层的接入参考，保持现有通用幂等存储的身份隔离和跨标签页语义。`payload` 不能包含每次重新生成的随机键。

```ts
// 建议新增：features/agent/api/logicalAgentRequests.ts
import { withSemanticPostIdempotency } from "@/lib/api/semanticIdempotency";
import { postAgentMessage, continueAgentRun } from "./agentApi";
import type {
  AgentMessageCreateInput,
  AgentMessageCreateResult,
  AgentRun,
} from "../model/contracts";

type MessagePayload = Omit<AgentMessageCreateInput, "idempotency_key">;

export function submitLogicalAgentMessage(input: {
  userId: string;
  sessionId: string;
  payload: MessagePayload;
  signal?: AbortSignal;
}): Promise<AgentMessageCreateResult> {
  return withSemanticPostIdempotency(
    {
      operation: "agent.message.create",
      userId: input.userId,
      sessionId: input.sessionId,
    },
    input.payload,
    (key) => postAgentMessage(
      input.sessionId,
      { ...input.payload, idempotency_key: key },
      input.signal,
    ),
  );
}

export function continueLogicalAgentRun(input: {
  userId: string;
  runId: string;
}): Promise<AgentRun> {
  return withSemanticPostIdempotency(
    {
      operation: "agent.run.continue",
      userId: input.userId,
      sourceRunId: input.runId,
    },
    { sourceRunId: input.runId },
    (key) => continueAgentRun(input.runId, key),
  );
}
```

```ts
// agentSubmission.ts：局部替换交付判断。
// “是否立即自动重试”和“能否断言服务端未接受”不是同一个概念。
import { isAmbiguousRequestFailure } from "@/lib/api/semanticIdempotency";

export function agentSubmissionDeliveryIsUncertain(error: unknown): boolean {
  return isAmbiguousRequestFailure(error);
}
```

**控制器接入注意事项**

当前乐观占位也需要幂等键。实际接入时，应在 `withSemanticPostIdempotency` 获得 key 的回调内，按 `sessionId + key` 复用或创建乐观占位，再提交；不能在外层继续随机生成占位身份，让同一个重试在界面里出现两套消息。

建议给 store 增加一个原子动作，而不是让控制器串联若干松散 setState：

```ts
// 接入接口示例：由现有 Agent store 实现并测试该动作。
export interface OptimisticAttemptHandle {
  userMessageId: string;
  assistantMessageId: string;
  runId: string;
}

export interface AgentAttemptStore {
  // 相同会话、相同 key：复用占位并恢复 submitting 状态。
  // 不同 key：创建新的占位；不得把已确认任务改回乐观任务。
  ensureAttempt(input: {
    sessionId: string;
    idempotencyKey: string;
    text: string;
  }): OptimisticAttemptHandle;
}
```

这段接口是新增接入契约，不是当前仓库已经存在的方法。实现时继续使用现有 `stageOptimisticSubmission`、`reconcileSubmission` 和带版本的快照归并，不复制一套消息模型。

**回归验收**

| 场景 | 预期 |
| --- | --- |
| 服务端接收，第一次响应丢失 | 重试沿用原 key |
| 两次响应丢失，用户手动重试 | 仍返回同一个 run，不新增费用预留 |
| 504 后刷新页面 | 能恢复待确认操作，而不是直接变成新请求 |
| 两个标签页重试相同操作 | 同一身份下共享待确认 key |
| 旧账号请求在切换账号后返回 | 不写入新账号消息 / 草稿 |
| 请求已经确认，用户明确再次生成 | 创建新操作，不永久抑制相同文案 |
| 点击继续后丢失响应 | 对同一源 run 的重试复用待确认身份 |

本轮隔离检查验证了“同键可重放、新键会绕过按键去重”的逻辑，以及 504 分类差异。真实数据库事务、账务预留和多标签页恢复仍需联调。

---

<a id="b02"></a>

### B02 · Runtime 在正文 await 之后缺少第二次准入检查

**位置**：`apps/agent-runtime/src/server.ts`，`createRuntimeServer` 的请求处理器及 `shutdown`。

[固定提交源码](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/agent-runtime/src/server.ts)

**根因与时序**

请求在读取正文前检查 `draining` 和 readiness，然后等待 `readBody`。等待期间，SIGTERM 可以把服务切换到 draining。正文完成后，代码只检查容量，没有重新检查生命周期状态，随后增加 activeRuns 并启动任务。

同时，shutdown 的等待集合是对 `activeExecutions` 的一个快照。因此“正在读取正文但尚未注册执行”的请求，不一定包含在最初的等待集合中。

```text
请求 A：准入通过 ── await readBody ─────────────── 继续启动任务
服务端：                 开始 draining ── 等待既有 activeExecutions
```

**建议局部补丁**

放在正文读取、认证及 parse 完成之后，容量预留之前。这里不要再插入 await，以保持“复查 + 预留”在同一事件循环片段中完成。

```ts
// server.ts：在 pendingBodyReads 的 try/finally 结束后插入。
if (response.destroyed || response.writableEnded) {
  return;
}

if (draining || !readiness.state.ready) {
  writeError(
    response,
    503,
    draining ? "agent_runtime_draining" : "agent_runtime_not_ready",
  );
  return;
}

// 下方继续现有容量判断、activeRuns / activeRunBytes 预留。
// 从本次检查到 activeExecutions.set 之间不应引入异步等待。
```

若需要精细化排空，再把 pending body reader 纳入关闭协调；但修这个缺口不需要重写整个 HTTP 服务，也不需要取消现有 HMAC、限流、容量保护。

**测试设计**

在测试环境给正文读取阶段一个可控制的 Promise 屏障：通过首次准入后暂停；调用 shutdown；再放行正文。断言 `executeAgentRun` 没有被调用、活动计数不增加、连接仍可写时返回 503。另测原本已经注册的任务能在 grace 内完成。

本轮屏障式隔离检查：旧控制流启动 1 次，增加复查后启动 0 次。未执行真实 Node HTTP / SIGTERM 集成测试。

---

<a id="b03"></a>

### B03 · ErrorBoundary 的空 fallback 让功能静默消失

**位置**：`apps/web/src/components/LumenAppShell.tsx` 的 `OptionalIsland`；`apps/web/src/components/ErrorBoundary.tsx`。

[Shell](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/components/LumenAppShell.tsx) · [错误边界](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/components/ErrorBoundary.tsx)

当前 OptionalIsland 对 Lightbox、InpaintModal、GlobalTaskTray、CommandPalette 使用 `fallback={null}`。ErrorBoundary 捕获渲染错误后保留 hasError；在提供 fallback 时直接返回它。这里没有可见重试入口，也没有基于路由或重新打开动作的 resetKey。

结果不是整页白屏，而是该功能在边界存续期间消失。用户点击按钮无反应，很难区分是操作失误、加载中还是模块出错。

**建议：局部恢复，不把刷新整页当成唯一答案**

```tsx
// ErrorBoundary.tsx：完整类结构参考；默认 ErrorState 可保留原项目样式。
import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorState, Button } from "@/components/ui/primitives";

interface Props {
  children: ReactNode;
  fallback?: ReactNode | ((reset: () => void) => ReactNode);
  resetKeys?: readonly unknown[];
}
interface State { hasError: boolean; error: Error | null }

function resetKeysChanged(
  before: readonly unknown[] = [],
  after: readonly unknown[] = [],
): boolean {
  return before.length !== after.length ||
    before.some((value, index) => !Object.is(value, after[index]));
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 接入现有错误采集时保持敏感数据脱敏，不记录草稿 / 凭据。
    console.error("[ErrorBoundary] caught", error, info);
  }

  private handleReset = (): void => {
    this.setState({ hasError: false, error: null });
  };

  componentDidUpdate(previous: Props): void {
    if (this.state.hasError && resetKeysChanged(previous.resetKeys, this.props.resetKeys)) {
      this.handleReset();
    }
  }

  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;
    if (typeof this.props.fallback === "function") {
      return this.props.fallback(this.handleReset);
    }
    if (this.props.fallback !== undefined) return this.props.fallback;
    return (
      <ErrorState
        title="当前视图暂不可用"
        description="可以先重试当前视图。刷新页面前，请确认未保存内容已有副本。"
        onRetry={this.handleReset}
        retryLabel="重试当前视图"
        secondaryAction={
          <Button type="button" variant="secondary" onClick={() => window.location.reload()}>
            刷新页面
          </Button>
        }
      />
    );
  }
}
```

```tsx
// OptionalIsland：可恢复的局部错误提示示例。
function OptionalIsland({
  name,
  resetKey,
  children,
}: {
  name: string;
  resetKey?: string | number;
  children: React.ReactNode;
}) {
  return (
    <ErrorBoundary
      resetKeys={[resetKey]}
      fallback={(reset) => (
        <div className="module-recovery" role="status">
          <span>{name}暂时无法打开。</span>
          <button type="button" onClick={reset}>重试该功能</button>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  );
}
```

`resetKey` 应来自重新打开次数、资源 ID 或确有意义的上下文，不要使用每次 render 都变化的 Date.now()。异步分包加载失败还需要区分部署版本失配和普通渲染错误：部分动态模块加载器会缓存失败，仅 reset 边界未必重新下载。此时显示“保存草稿后刷新”，不能无限自动重试。

**验收**：人为让图片预览渲染失败，其他功能仍可使用；错误可见；局部重试后恢复；失败前的草稿不因边界 reset 被清空。React 错误边界的用途和限制见官方 Component 文档，不应用它替代事件处理器内的错误处理。

---

<a id="b04"></a>

### B04 · HEAD 被兼容封装改写为 GET

**位置**：`apps/web/src/lib/api/http.ts` 与 `apps/web/src/lib/api/queryClient.ts`。

[兼容封装](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/lib/api/http.ts) · [查询客户端](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/lib/api/queryClient.ts)

兼容层把 GET 和 HEAD 都送到 `queryClient.get`；后者最终覆盖 `method: "GET"`。现有 `queryClient.head` 已实现 HEAD 和 `expectNoContent`，却未被这条分支使用。

**局部替换思路**

```ts
// 保留 apiFetch 当前 headers、budget、identity 等处理，不要另起裸 fetch。
if (method === "HEAD") {
  return queryClient.head(path, {
    ...requestInit,
    budget,
  });
}

if (method === "GET") {
  return queryClient.get<T>(path, {
    ...requestInit,
    budget,
  });
}
```

同时修正类型契约：HEAD 的返回类型应为 `Promise<undefined>`，不是谎称存在 JSON 对象的 `Promise<T>`。可以为 HEAD 添加 overload，其余 GET / 写请求保持原调用体验；不要简单把所有返回值改成可空再让调用点到处加 `!`。

在已有泛型 overload 之前加入 HEAD 专用 overload：

```ts
export async function apiFetch(
  path: string,
  init: ApiFetchInit<NoContent> & { method: "HEAD" },
): Promise<NoContent>;
```

已有 head 方法可收窄为：

```ts
// queryClient.ts：保留 get 的现有行为，收窄 head 的返回契约。
import { apiTransport } from "./transport";
import type { RequestBudget } from "./requestBudget";
import type { ResponseValidator } from "./response";

type QueryOptions<T = unknown> = Omit<RequestInit, "method" | "body"> & {
  budget?: RequestBudget;
  validate?: ResponseValidator<T>;
};

export const queryClient = {
  get<T>(path: string, options: QueryOptions<T> = {}): Promise<T> {
    return apiTransport.request<T>(path, {
      ...options,
      method: "GET",
      requestClass: "query",
    }) as Promise<T>;
  },
  head(path: string, options: QueryOptions<unknown> = {}): Promise<undefined> {
    return apiTransport.request(path, {
      ...options,
      method: "HEAD",
      requestClass: "query",
      expectNoContent: true,
    }).then(() => undefined);
  },
};
```

**验收**：拦截实际 transport，断言请求方法 HEAD、响应体不被读取、GET 不受影响、401 / identity mismatch 仍走同一协调层。本轮只证明方法覆盖错误；未找到足够证据说明当前某个业务页面实际调用这条 HEAD 路径。

---

<a id="b05"></a>

### B05 · 异常认证字符串导致 TypeError 而不是安全拒绝

**位置**：`apps/api/app/security.py` 的 `parse_session_cookie`、`verify_csrf_token`；`apps/api/app/deps.py` 的 `require_bot_token`。

[security.py](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/api/app/security.py) · [deps.py](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/api/app/deps.py)

Python `hmac.compare_digest` 接受 ASCII 字符串或相同类型的字节数据。对未经格式验证的非 ASCII 字符串调用，会抛 TypeError。源码中的上述调用点没有先保证这个前提。

最小检查 `hmac.compare_digest("é", "0" * 64)` 已确认抛 TypeError。HTTP 服务最终是如何呈现此错误，还需通过实际 ASGI 请求验证；不能把它写成认证绕过，也不能从一次异常推断服务进程必然崩溃。

**建议代码**

```python
# security.py：只对实际十六进制签名使用此函数。
import hmac
import re

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

def constant_time_signature_matches(provided: str, expected: str) -> bool:
    if _SHA256_HEX.fullmatch(provided) is None:
        return False
    return hmac.compare_digest(provided, expected)

# parse_session_cookie 中：
# if not constant_time_signature_matches(sig, expected):
#     return None
#
# verify_csrf_token 中：
# return constant_time_signature_matches(sig, expected)
```

```python
# deps.py：Bot token 不是十六进制 HMAC，单独保留其契约。
import hmac

def constant_time_ascii_token_matches(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    if not expected.isascii() or not provided.isascii():
        return False
    return hmac.compare_digest(expected, provided)

# require_bot_token 中替换条件；后面的失败限流和 401 保持不变：
# if not constant_time_ascii_token_matches(expected, provided):
#     await _record_bot_auth_failure(request)
#     raise HTTPException(...)
```

将 Bot shared secret 的 ASCII / 长度约束放到启动配置验证中，配置错误应明确报错，不应运行后只表现为全部鉴权失败。不要使用全局 `except Exception: return False` 掩盖其他程序错误。

**建议 pytest 用例**

```python
import pytest
from app.security import constant_time_signature_matches

@pytest.mark.parametrize("value", ["é", "a" * 63, "g" * 64, "", "0" * 65])
def test_invalid_signature_rejects_without_exception(value: str) -> None:
    assert constant_time_signature_matches(value, "a" * 64) is False

def test_signature_match_and_mismatch() -> None:
    assert constant_time_signature_matches("a" * 64, "a" * 64)
    assert not constant_time_signature_matches("b" * 64, "a" * 64)
```

集成测试还应覆盖 Cookie、CSRF header、Bot header 三个入口，确认返回契约化的 401 / 403，且未绕开已有失败限流。

---

<a id="b06"></a>

### B06 · 焦点陷阱把不应进入 Tab 顺序的元素纳入候选集

**位置**：`apps/web/src/components/ui/primitives/mobile/useModalLayer.ts`。

[焦点管理](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/components/ui/primitives/mobile/useModalLayer.ts) · [命令面板调用点](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/components/ui/CommandPalette.tsx)

当前选择器是多个分支的并集，`button:not([disabled])` 不排除 `tabindex="-1"`；末尾 `[tabindex]:not([tabindex='-1'])` 又可把显式 disabled 的元素选回来。之后只检查显示状态，没有再过滤可顺序导航性。

这不是纯理论边界：命令面板所有 option 按钮都设置了 `tabIndex={-1}`，期望让焦点留在 combobox 输入框，通过 aria-activedescendant 表达活动项。当前焦点陷阱却会将这些 option 计入 first / last。

**建议局部修正**

```ts
const FOCUS_CANDIDATE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  'input:not([disabled]):not([type="hidden"])',
  "select:not([disabled])",
  "summary",
  '[contenteditable="true"]',
  "[tabindex]",
].join(",");

function isTabbableCandidate(element: HTMLElement): boolean {
  if (!isElementVisible(element)) return false;
  if (element.matches(":disabled")) return false;
  if (element.hasAttribute("tabindex") && element.tabIndex < 0) return false;

  const styles = window.getComputedStyle(element);
  if (styles.visibility === "hidden" || styles.visibility === "collapse") {
    return false;
  }

  return true;
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(FOCUS_CANDIDATE_SELECTOR),
  ).filter(isTabbableCandidate);
}
```

这是修复已观察到的候选集错误，不是宣称这一段代码完整重现了所有浏览器焦点规则。radio 分组、禁用 fieldset、嵌套 details、portal 内复合控件仍应有真实浏览器测试。已有 modal stack、inert 隔离和焦点恢复值得保留，不应为了这处缺陷再平行引入第二套弹窗栈。

**建议 Playwright 行为测试**

```ts
import { test, expect } from "@playwright/test";

test("命令面板的 Tab 顺序不进入 option", async ({ page }) => {
  await page.goto("/"); // 在项目既有登录 fixture / storageState 下执行。
  await page.keyboard.press("Control+k");
  const input = page.getByRole("combobox");
  await expect(input).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(
    page.getByRole("button", { name: "关闭命令面板" }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(input).toBeFocused();
});
```

本轮 Python 检查只验证 CSS 选择器确实选中负 tabindex / disabled 示例，未运行上述浏览器测试。

---

<a id="b07"></a>

### B07 · 输入法组合期间的 Escape 被当成关闭命令

**位置**：`apps/web/src/components/ui/CommandPalette.tsx` 的 `handleKeyDown`。

[固定提交源码](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/components/ui/CommandPalette.tsx)

当前顺序是先处理 Escape，再检查 `event.nativeEvent.isComposing`。底层 useModalLayer 已经跳过输入法组合期间的 Escape，但事件继续到组件自身的 handler 后仍会关闭面板。

**建议局部替换**

```tsx
const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
  // 所有命令判断之前，先把组合输入事件留给输入法。
  if (event.nativeEvent.isComposing) return;

  if (event.key === "Escape") {
    event.preventDefault();
    closePalette();
    return;
  }

  // 原有 ArrowDown / ArrowUp / Enter 处理放在这里，保持不变。
};

// 全局 Command+K handler 同样应拒绝组合输入和重复按键。
const onKeyDown = (event: globalThis.KeyboardEvent) => {
  if (event.isComposing || event.repeat || event.defaultPrevented) return;
  const isCommandK =
    event.key.toLocaleLowerCase() === "k" && (event.metaKey || event.ctrlKey);
  if (!isCommandK) return;
  event.preventDefault();
  if (open) closePalette();
  else openPalette();
};
```

如果需要兼容特定浏览器的历史组合输入行为，可以封装一个经过浏览器回归的 `isComposingKeyboardEvent`，而不是把 keyCode 229 判断散落到每个组件。不要为了快捷键而劫持所有输入框的 Escape。

**验收**：拼音 / 注音输入候选期间按 Escape，只取消候选；确认组合结束后再按 Escape，关闭面板并恢复原焦点。隔离检查已经验证当前分支顺序确实先返回“关闭”；实际输入法仍需在 macOS、Windows、移动设备上测试。

---

<a id="b08"></a>

### B08 · 命令面板选中项可以离开可见区域

同一文件中，方向键更新 selectedIndex，输入框通过 aria-activedescendant 引用选中项；结果容器高度受限，但没有把活动项滚到可见范围的 effect。

更新 aria-activedescendant 并不等于让浏览器滚动列表。结果多时，用户可能看不到即将由 Enter 执行的命令。

**建议代码**

```tsx
import { useEffect, useRef } from "react";

export function useActiveOptionVisibility({
  open,
  activeId,
  resultsKey,
  presentation,
}: {
  open: boolean;
  activeId: string | undefined;
  resultsKey: string;
  presentation: "dialog" | "sheet";
}) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open || !activeId) return;
    const root = listRef.current;
    const item = document.getElementById(activeId);
    if (!root || !item || !root.contains(item)) return;

    const viewport = root.getBoundingClientRect();
    const bounds = item.getBoundingClientRect();
    const inset = 8;
    const top = viewport.top + inset;
    const bottom = viewport.bottom - inset;
    if (bounds.top < top) root.scrollTop -= top - bounds.top;
    else if (bounds.bottom > bottom) root.scrollTop += bounds.bottom - bottom;
  }, [open, activeId, resultsKey, presentation]);
  return listRef;
}
// 将返回的 listRef 接到原 listbox 元素。
// activeId 与输入框 aria-activedescendant 使用同一值。
// resultsKey 使用“查询串 + 当前结果 ID 顺序”的稳定字符串。
// 搜索输入增加 aria-label="搜索命令或页面"。
```

对于列表出现 / 变高 / 从移动 BottomSheet 切换到桌面 Dialog 的情况，保留容器分支依赖。这里不需要平滑滚动动画：连续按方向键时，精确跟随比装饰性缓动更重要。

**建议 Playwright 断言**

```ts
const input = page.getByRole("combobox");
for (let index = 0; index < 12; index += 1) {
  await page.keyboard.press("ArrowDown");
}
await expect(input).toBeFocused();
const activeId = await input.getAttribute("aria-activedescendant");
expect(activeId).toBeTruthy();
const visible = await page.evaluate((id) => {
  const option = document.getElementById(id!);
  const list = option?.closest('[role="listbox"]');
  if (!option || !list) return false;
  const a = option.getBoundingClientRect();
  const b = list.getBoundingClientRect();
  return a.top >= b.top && a.bottom <= b.bottom;
}, activeId);
expect(visible).toBe(true);
```

此片段应放进已有登录并打开面板的测试上下文。未在本轮浏览器中执行。

---

<a id="b09"></a>

### B09 · 画布投影排序遇到两个“无版本”哨兵会产生 NaN

**位置**：`apps/web/src/lib/canvas/documentMerge.ts`。

[固定提交源码](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/lib/canvas/documentMerge.ts)

`projectionTimestamp` 和 `projectionRevision` 在没有有效值时返回 `Number.NEGATIVE_INFINITY`。调用者通过减法比较：

```ts
const timestampDifference = leftVersion.timestamp - rightVersion.timestamp;
return timestampDifference !== 0
  ? timestampDifference
  : leftVersion.sequence - rightVersion.sequence;
```

当两侧时间戳都没有值时，`-Infinity - -Infinity` 是 NaN，而 `NaN !== 0` 为 true。代码不会继续比较 sequence；后续 `compare(...) > 0` 为 false。

**具体示例**

同一 graph revision 下，双方都没有执行记录和 active runs；当前 selections 包含 a@5、b@3，迟到快照只有 a@4。当前实现算出 NaN，使 `preserveMissingCurrent` 为 false，b 不会被保留。单项 a 的合并可以仍然正确，因此只看一个 selection 的测试容易漏掉它。

这指向客户端投影回退 / 暂时缺项，不等于数据库内容被永久删除。该例已用隔离算法验证。

**建议局部替换：用有序比较，不用带无穷哨兵的减法**

```ts
// 输入由现有 projectionTimestamp / projectionRevision 归一化：
// 只会是有限数值或 -Infinity，不允许未经归一化的 NaN。
function compareProjectionNumber(left: number, right: number): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

function compareDocumentProjection(
  left: CanvasDocument,
  right: CanvasDocument,
): number {
  const a = documentProjectionVersion(left);
  const b = documentProjectionVersion(right);
  return compareProjectionNumber(a.timestamp, b.timestamp) ||
    compareProjectionNumber(a.sequence, b.sequence);
}

function compareSelectionProjection(
  left: CanvasNodeSelection,
  right: CanvasNodeSelection,
): number {
  return compareProjectionNumber(
    projectionRevision(left.revision),
    projectionRevision(right.revision),
  );
}

function compareExecutionProjection(
  left: CanvasNodeExecution,
  right: CanvasNodeExecution,
): number {
  return compareProjectionNumber(
    executionProjectionTimestamp(left),
    executionProjectionTimestamp(right),
  );
}

function compareRunProjection(left: CanvasRun, right: CanvasRun): number {
  return compareProjectionNumber(
    projectionRevision(left.last_event_seq),
    projectionRevision(right.last_event_seq),
  ) || compareProjectionNumber(
    projectionTimestamp(left.updated_at, left.created_at),
    projectionTimestamp(right.updated_at, right.created_at),
  );
}
```

**验收矩阵**

双方时间戳缺失、只有一方缺失、时间戳相同但 sequence 不同、run.last_event_seq 双方缺失、空列表、重复快照、迟到快照、包含未出现在 incoming 中的 current 项，都必须覆盖。检查比较器自反性 `compare(x, x) === 0`、反对称性和结果从不为 NaN。

不要用“只要 incoming 有响应就覆盖全部”来简化这里，也不要用客户端接收时间替代服务端版本顺序。

---

<a id="b10"></a>

### B10 · JSON 语义指纹丢失自有 __proto__ 属性

**位置**：`apps/web/src/lib/api/semanticIdempotencySemantics.ts` 的 `sortJsonValue`。

[固定提交源码](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/lib/api/semanticIdempotencySemantics.ts)

当前用普通对象 `{}` 接收排序后的键，再执行 `sorted[key] = ...`。当 key 是来自 JSON.parse 的自有 `__proto__` 属性时，这不是普通的自有属性写入，而会触发普通对象的原型 setter。JSON.stringify 随后不包含该键。

```ts
const first = JSON.parse('{"__proto__":{"x":1},"text":"same"}');
const second = JSON.parse('{"__proto__":{"x":2},"text":"same"}');
// 当前规范化后可能生成相同字符串。
```

本轮检查确认该碰撞，同时确认 `Object.prototype` 没有因此被修改。**这是规范化 / 指纹契约缺陷，不是已经证明的全局原型污染攻击。** 当前业务表单能否把该自有键传到函数，需要结合所有调用者继续验证，因此列为 P3 潜在问题。

**局部替换**

```ts
function sortJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortJsonValue);
  if (value === null || typeof value !== "object") return value;

  const record = value as Record<string, unknown>;
  const sorted: Record<string, unknown> = Object.create(null);
  for (const key of Object.keys(record).sort()) {
    sorted[key] = sortJsonValue(record[key]);
  }
  return sorted;
}
```

这一修复针对 JSON 数据。若函数继续接受任意 unknown，应明确不支持 Date、Map、循环引用、BigInt 等非 JSON 类型，或者先做输入约束；不能未经设计就自定义它们的序列化语义并改变现有幂等键。

**回归**：不同键顺序应同指纹；数组顺序不同应不同；两个上述 __proto__ 示例必须不同；JSON 中 null、空数组、空对象保持区别；旧版持久化待确认操作如何升级需单独设计，避免切换指纹算法后找不到旧 key。

---

<a id="b11"></a>

### B11 · Tailwind 扫描路径与扫描范围意图不一致

**位置**：`apps/web/src/app/globals.css` 文件顶部。

[固定提交源码](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/app/globals.css)

当前从该 CSS 文件声明 `@source "./src/**/*.{ts,tsx,js,jsx,mdx,html}"`。Tailwind 4 的显式 source 路径相对于样式表，因此它指向 `apps/web/src/app/src`，而不是 `apps/web/src`。另外，普通 `@import "tailwindcss"` 仍启用自动发现；额外的 @source 不会把自动发现关掉。

所以这里是“显式路径错误，而且限制范围的意图没有成立”，不是“整个站点没有生成样式”。自动发现可能正好掩盖错误。

**配置参考**

```css
/* globals.css 位于 apps/web/src/app/。 */
@import "tailwindcss" source(none);
@import "./markdown.css";

/* 明确以 src 为来源；跨包组件若含 class，需另加其实际路径。 */
@source "../";
```

要不要关闭自动发现是工程选择：若仓库没有明确范围控制需求，也可以删除错误的 @source，让默认发现工作；不要保留一个看似有效但实际上无效的限制。

**验收**：从 monorepo 根目录和 apps/web 目录分别执行正式构建；验证 app、features、shared、components 的真实类都存在；同时验证跨包来源；比较生成 CSS 的体积和类清单。不能只加正则断言证明 CSS 字符串变了。

参考：[Tailwind 官方 source 文档](https://tailwindcss.com/docs/detecting-classes-in-source-files)。本轮只验证路径解析及官方规则，没有运行 Tailwind 编译器。

---

<a id="b12"></a>

### B12 · 文字令牌与装饰令牌混用，部分组合对比度不足

**位置**：`apps/web/src/components/ui/primitives/Button.tsx` 的 link variant；`globals.css` 的主题令牌；`CommandPalette.tsx` 的分组说明文字。

[Button](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/components/ui/primitives/Button.tsx) · [主题](https://github.com/cyeinfpro/Lumen/blob/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026/apps/web/src/app/globals.css)

link variant 使用 `--info`，但浅色主题加深的是 `--info-fg`。两者用途不一致：可作为图形或填充色的颜色，不一定适合作为小字号文字色。命令面板的非禁用分组文字使用 fg-3，也有同类问题。

**已执行的不透明令牌组合计算**

| 组合 | 对比度 | 说明 |
| --- | ---: | --- |
| `#3E9EFF` / `#FFFFFF` | 2.79:1 | 当前 info 色放在白色上 |
| `#3E9EFF` / `#F4F5F7` | 2.55:1 | 当前 info 色放在浅色画布上 |
| `#0D74CE` / `#FFFFFF` | 4.77:1 | 已有 info-fg 放在白色上 |
| `#0D74CE` / `#F4F5F7` | 4.37:1 | **单纯切到已有 info-fg，仍非全部场景合格** |
| `#5E5951` / `#121318` | 2.67:1 | 暗色 fg-3 放在 panel 上 |
| `#A7ADB7` / `#EAECF0` | 1.91:1 | 浅色 fg-3 放在 panel 上 |

这些是按源码令牌做的颜色数学，不是完整页面最终 computed style 的扫描。透明叠加、文字尺寸、字重、实际背景都应在浏览器再次验证。普通正文常用的 WCAG AA 目标是至少 4.5:1，大字号文字是 3:1；禁用控件有例外，不能借此把仍需阅读的说明文字当作禁用内容。

**建议局部修复**

```css
:root,
.theme-dark,
.dark {
  --link-fg: #8bc5ff;
}

.theme-light {
  --link-fg: #075da8;
}

@media (prefers-color-scheme: light) {
  :root:not(.theme-dark):not(.dark) {
    --link-fg: #075da8;
  }
}
```

```tsx
// Button.tsx：在原 variants 对象中让 link 使用这个类名。
const LINK_VARIANT_CLASSNAME =
  "bg-transparent text-[var(--link-fg)] underline underline-offset-2 " +
  "hover:opacity-100 hover:decoration-2 border-0 p-0 h-auto";
// variants 对象对应成员改为：link: LINK_VARIANT_CLASSNAME

// CommandPalette.tsx：仍需阅读的分组说明用可读文字令牌。
// className="hidden type-caption text-[var(--fg-muted-aa)] sm:inline"
```

建议的浅色 link `#075DA8` 在当前四种浅色背景上的对比度分别为：白色 6.69、canvas 6.13、panel 5.65、raised 5.05。该计算已执行。悬停不再通过降低 opacity 把对比度重新拉低；改为下划线粗细变化。

不要直接把所有 `--info` 和 `--accent` 全局加深：填充按钮和图标会受到连带影响。应拆成“文字、边框、弱填充、实心填充、填充上的文字”五种用途。

参考：[WCAG Contrast Minimum](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)。

---

## 3. 本轮没有作为 Bug 报告的事项

反复审计容易产生“看见关键词就报问题”。以下保护在已读代码里确实存在，应保留而不是被重构删除。

| 领域 | 已确认存在的机制 | 不应再做的错误建议 |
| --- | --- | --- |
| Agent Runtime 认证 | HMAC、时间窗口、nonce 重放缓存、常量时间比较 | 不应声称完全无认证 / 无重放保护 |
| NDJSON 输出 | 单行限制、串行写入、背压超时、传输失败锁定 | 不应建议继续无限缓冲来“保证输出” |
| Agent 消息创建 | 服务端按 key 去重、指纹校验、事务内任务和预留、Outbox | 不应把 B01 描述为后端完全不幂等 |
| 发送入口 | 同步 submission fence 与 submitting / active-run 判断 | 不应声称只缺一个 disabled 就能解决全部重试问题 |
| 浏览器 API | 身份 epoch / 确认身份、CSRF、请求预算、统一错误协调 | 不应以裸 fetch 替换成熟传输层 |
| 会话认证 | 持久 session 状态、已删除用户校验、会话绑定 CSRF | 不应只根据旧注释判断为简单 double-submit |
| 画布 | 原子操作分组、失败批次保留、草稿恢复、紧急副本 | 不应声称没有自动保存或离线恢复 |
| 超大粘贴 | store 中已有操作数量上限保护 | 不能仅凭批次上限推断用户粘贴必然永久卡住 |
| 弹窗 | modal stack、inert、焦点恢复、角色标注 | 修 B06，不并行再造一个独立弹窗系统 |
| 基础按钮 | 默认 type=button、loading 禁用、触控尺寸补偿 | 不能泛称所有按钮误提交或没有点击面积 |
| UI 设计基础 | 已有语义 surface / typography / spacing tokens | 不应再叠一套完全不相容的令牌 |
| 前端测试入口 | Node 测试发现脚本遍历 src 和 __tests__ | 不应说项目没有测试，也不应把文本匹配测试当行为测试 |

尚未证实的方向包括图片加载后的滚动锚定、特定尺寸实际错位、完整账务竞态、全链路 SSE 重放、所有用户数据导出 / 删除路径。它们进入后文测试矩阵，不混入确认问题数量。

---

<a id="design"></a>

## 4. UI / UX 重构总方向：从功能集合变成专业创作工作台

### 4.1 目标不是“更像某个流行网站”

建议把 Lumen 定位成**内容创作工作台**：用户在这里组织需求、参考素材、Agent 执行、生成结果和项目，而不是不断切换几个带有聊天框的演示页面。

高级感在这里应来自四件事：清楚的主次、稳定的空间、可预测的反馈、完整的失败恢复。不是所有面板都透明，不是所有激活态都发光，也不是所有说明都用更小更淡的文字。

当前中性深色底、琥珀色品牌和语义令牌可以保留。收敛 primary 的渐变 / hover glow、常驻区域的 glass 表面和多重阴影，让这些效果只在确有层级意义的位置出现。不要同时给背景、边框、阴影、图标和文字五处上强调色。

### 4.2 当前源码支持的改造切入点

| 观察位置 | 可以确认的实现 | 重构方向 | 判定边界 |
| --- | --- | --- | --- |
| AgentComposerControls | 图片能力启用时常驻五个 select 和费用区 | 主区保留摘要；低频参数进 Inspector | 设计选择，不是五个 select 本身违反功能契约 |
| AgentComposer | 附件、输入、摘要、工具、错误多层堆叠 | 固定输入主体，附件和高级设置渐进展开 | 精简层级，不删除必要能力 |
| Button | primary 渐变 / amber shadow，glass 变体 | 主按钮纯色；仅图上操作保留必要 scrim | 不是所有现有 glass 都应一刀切删除 |
| DesktopTopNav | 等宽侧列、居中导航、右侧全局工具 | 压力测试中窄桌面；统一压缩策略 | 未以截图确认具体像素错位 |
| LumenAppShell | 关键全局功能空错误回退 | 可见、局部、可恢复状态 | B03 的功能问题优先于美化 |
| globals.css | 深浅主题重复声明与多套语义别名 | 建立单一 token 生成 / 映射入口 | 不在首个 PR 中全量改名 |
| Agent scroll manager | 基于消息版本与置底状态滚动 | 保留读历史优先，补尺寸变化验证 | 图片高度变化是否已由布局抵消需浏览器确认 |

### 4.3 建议的信息架构

保留现有路由和导航可见性策略，用统一的 route adapter 提供导航，不在组件中硬编码第二张菜单。第一阶段改名称、层级和布局，不迁移用户 URL。

```text
Lumen
├─ 创作：目标、参考资料、生成和结果
├─ Agent：连续任务、工具执行、过程和产物
├─ 项目：按项目组织会话 / 画布 / 资产
├─ 资产：查找、比较、筛选和复用
└─ 账户与设置：模型、凭据、用量、隐私、偏好
   └─ 管理：仅对管理员呈现入口，服务端鉴权仍是唯一授权依据
```

“创作”和“Agent”短期可以仍是独立功能，避免硬合并两套数据模型。通过统一的项目上下文、素材挑选和结果操作降低割裂感。视频、画布作为工作模式或已存在的独立路由，都可以由同一 Shell 承载；不要先为导航好看而改执行协议。

### 4.4 桌面空间结构

```text
┌──────────────────────────────────────────────────────────────────┐
│ Lumen  项目 / 当前工作内容                    搜索  任务  账户      │
├──────────────┬──────────────────────────────────┬────────────────┤
│ 项目 / 会话   │ 上下文标题             模式 / 操作 │ 参数 / 素材信息  │
│              ├──────────────────────────────────┤                │
│ 最近内容     │                                  │ 按需打开        │
│ 搜索 / 分组  │    消息、画布或生成结果为主体       │ 不是常驻表单墙   │
│              │                                  │                │
│              ├──────────────────────────────────┤                │
│              │ 参考资料 → 输入 → 摘要 / 费用 / 发送│                │
└──────────────┴──────────────────────────────────┴────────────────┘
```

以上是方案线框，不是已渲染产品截图。右侧 Inspector 在中窄桌面变为按需覆盖层；左侧目录可收起。工作内容必须 `min-width: 0`，列表和画布各自有唯一明确的主滚动容器。

### 4.5 移动端不是把桌面缩小

主工作区保持单列。会话列表进入 Drawer，高级参数进入现有 BottomSheet；主输入区只保留当前任务所需控件。弹出键盘时根据实际 visual viewport 验证输入区可见，不额外叠加两份 safe-area padding。

重要原则：**同一份控制器和草稿状态，两个响应式布局适配器；不是同时挂载两套工作台，再用 CSS 隐藏其中一个。** 否则重复订阅、双重上传入口和焦点问题会与 UI 改造一起进入产品。

---

<a id="pages"></a>

## 5. 逐页面重构清单

本节是面向整个前端的产品方案。对未完整读取的页面，不把方案理由伪装成已经确认的页面缺陷。每项均对应后文的实现代码 / 组件模式。

| 页面 / 区域 | 目标体验 | 具体改法 | 建议代码位置 |
| --- | --- | --- | --- |
| 全局 Shell | 用户知道当前位置，也知道下一步在哪里 | 一套导航契约、一个主工作区、两侧按需展开 | 6.2 WorkbenchShell |
| 创作首页 | 进入即能开始，而非先看营销卡片 | 一个输入主体；少量明确示例；最近项目在次级区 | 6.3 Composer / 6.8 EmptyState |
| Agent | 看得懂正在做什么，也能辨认失败和未知 | 时间线、折叠工具过程、固定执行状态、产物直达 | 6.4 RunActivity / 6.5 OperationStatus |
| 图片生成 | 参数可发现，但不持续占满屏幕 | 常用摘要 + 参数面板；估算不伪装成最终费用 | 6.3 / 6.6 Inspector |
| 视频 | 等待过程可解释 | 显示提交 / 排队 / 处理 / 可下载等真实状态，耗时未知不造百分比 | 6.5 |
| 画布 | 编辑不会被后台同步和跳动打断 | 自动保存状态常驻；冲突在原位显示；恢复副本可辨认 | 6.7 SaveStatus |
| 项目目录 | 找内容快、操作轻 | 紧凑行列表与可选缩略图；名称、更新时间、未完成任务优先 | 6.9 ProjectRow |
| 资产库 | 内容优先，可比较和复用 | 图像预览、独立选择区、来源和状态；不要把卡片整个变成嵌套按钮 | 6.10 AssetCard |
| 预览 / 局部重绘 | 打开、关闭和返回编辑稳定 | 局部错误恢复、资源内尺寸、键盘操作，避免全局重载 | B03 / 6.11 Dialog |
| 设置 | 用户知道设置作用于本次、会话还是全局 | 按任务分组；解释生效范围；字段级错误；脏状态保护 | 6.6 / 6.12 SettingsField |
| 用量 / 账单 | 区分预估、预留、最终结算 | 统一金额格式和标签；不得用“预计”冒充“已扣除” | 6.5 / 6.12 |
| 后台管理 | 安全、密度合理、可审计 | 表格、筛选、行级动作；危险操作前展示对象和影响 | 6.9 / 6.11 |
| 登录 / 空 / 错误 / 离线 | 任何状态都有真实下一步 | 少空话，明确恢复动作；失败不清草稿；无权限不显示假空数据 | 6.8 |

### 5.1 页面状态必须作为设计稿的一部分

每个重要页面至少覆盖：初次加载、空内容、有内容、局部刷新、提交中、后台执行、成功、确定失败、结果未知、离线、权限失效、版本冲突。不能只设计“正常有数据”的那张图，再靠散落的 toast 拼出其余状态。

建议文案：

| 场景 | 不建议 | 建议 |
| --- | --- | --- |
| 丢失提交确认 | “生成失败，请重新生成” | “尚未确认是否已提交。正在核对任务状态。” |
| 网络断开 | “发生未知错误” | “连接已断开。本地内容仍保留，恢复后继续同步。” |
| 参数说明 | “释放灵感，开启创意之旅” | “默认用于本会话的新任务，不改变已经开始的任务。” |
| 任务停止 | 一点停止立即显示“已取消” | 先“正在请求停止”，服务端确认后才显示“已停止” |
| 保存冲突 | “保存失败” | “远端已有新版本。你的修改已保留，选择比较或恢复副本。” |
| 真正的空目录 | “暂无数据” | “还没有项目。创建一个项目，把会话和素材放在一起。” |

这里的“本地内容仍保留”只在持久化确实成功时显示；存储不可用时应如实提示，不能让文案越过技术事实。

---

<a id="implementation"></a>

## 6. 前端重构建议代码

以下组件是**接入参考实现**，用于定义新 UI 的边界和交互，不是可以不看现有类型就覆盖整个项目的成品分支。已存在的传输层、权限校验、草稿存储、事件归并和基础组件优先复用。

### 6.1 UI-01 · 建立受控的新样板层，而不是全局追加覆盖规则

先在一个工作台页面使用 `.workbench-v2`，验证后再把成熟规则合入已有 tokens。不要让 v1/v2 的业务控制器同时工作。下面的颜色、尺寸和动效都是**建议规格**，不是本轮测出的性能结果。

```css
/* 建议：src/styles/workbench-v2.css；仅在试点 Shell 根节点挂类。 */
.workbench-v2 {
  color-scheme: dark;
  --wb-bg: #0b0d10;
  --wb-chrome: #101318;
  --wb-panel: #151920;
  --wb-raised: #1d232c;
  --wb-text: #f2f3f5;
  --wb-secondary: #adb4be;
  --wb-muted: #939caa;
  --wb-line: #303844;
  --wb-control-line: #748194;
  --wb-selected: #222b36;
  --wb-accent: #f2a93a;
  --wb-on-accent: #18130b;
  --wb-link: #8bc5ff;
  --wb-focus: #f2a93a;
  --wb-danger: #ff9297;
  --wb-radius-control: 8px;
  --wb-radius-panel: 12px;
  --wb-space: 4px;
  --wb-duration: 140ms;
  --wb-shadow-overlay: 0 16px 48px rgb(0 0 0 / 24%);
  color: var(--wb-text);
  background: var(--wb-bg);
  font-family: var(--font-body, system-ui, sans-serif);
  font-size: 14px;
  line-height: 1.5;
}

.theme-light .workbench-v2 {
  color-scheme: light;
  --wb-bg: #f4f5f7;
  --wb-chrome: #ffffff;
  --wb-panel: #ffffff;
  --wb-raised: #e8ecf1;
  --wb-text: #17202a;
  --wb-secondary: #465160;
  --wb-muted: #586273;
  --wb-line: #d4dae2;
  --wb-control-line: #758190;
  --wb-selected: #e5ebf2;
  --wb-link: #075da8;
  --wb-focus: #87530c;
  --wb-danger: #b4232d;
  --wb-shadow-overlay: 0 16px 48px rgb(23 32 42 / 14%);
}

/* 试点阶段兼容系统主题；最终与现有主题 bootstrap 合并为一处真源。 */
@media (prefers-color-scheme: light) {
  :root:not(.theme-dark):not(.dark):not(.theme-light) .workbench-v2 {
    color-scheme: light;
    --wb-bg: #f4f5f7;
    --wb-chrome: #ffffff;
    --wb-panel: #ffffff;
    --wb-raised: #e8ecf1;
    --wb-text: #17202a;
    --wb-secondary: #465160;
    --wb-muted: #586273;
    --wb-line: #d4dae2;
    --wb-control-line: #758190;
    --wb-selected: #e5ebf2;
    --wb-link: #075da8;
    --wb-focus: #87530c;
    --wb-danger: #b4232d;
    --wb-shadow-overlay: 0 16px 48px rgb(23 32 42 / 14%);
  }
}

.workbench-v2 :where(button, a, input, textarea, select, summary):focus-visible {
  outline: 2px solid var(--wb-focus);
  outline-offset: 3px;
}
.workbench-v2 :where(button, input, textarea, select) { font: inherit; }
.workbench-v2 button { cursor: pointer; }
.workbench-v2 button:disabled { cursor: not-allowed; opacity: .6; }
.workbench-v2 a { color: var(--wb-link); }
.workbench-v2 .wb-button {
  min-height: 36px;
  padding: 7px 12px;
  border: 1px solid var(--wb-control-line);
  border-radius: var(--wb-radius-control);
  color: var(--wb-text);
  background: var(--wb-panel);
  transition: background-color var(--wb-duration), border-color var(--wb-duration);
}
.workbench-v2 .wb-button:hover:not(:disabled) { background: var(--wb-raised); }
.workbench-v2 .wb-button[data-tone="primary"] {
  background: var(--wb-accent);
  color: var(--wb-on-accent);
  border-color: transparent;
  box-shadow: none;
}
.workbench-v2 .wb-button[data-tone="danger"] { color: var(--wb-danger); }
.workbench-v2 .wb-muted { color: var(--wb-muted); }
.workbench-v2 .wb-error { color: var(--wb-danger); }
.workbench-v2 .wb-number { font-variant-numeric: tabular-nums; }

@media (pointer: coarse) {
  .workbench-v2 :where(.wb-button, .wb-touch-target, summary) {
    min-width: 44px;
    min-height: 44px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .workbench-v2 *, .workbench-v2 *::before, .workbench-v2 *::after {
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    scroll-behavior: auto !important;
  }
}
```

落地时把 `.wb-button` 的成熟样式合回现有 `Button` variants，不保留两套永久按钮。试点阶段也可以通过现有 Button 的 `className` 使用这些样式。44px 是这里选择的触控产品规格，不是在说 WCAG 对所有目标都统一要求 44px。

对 JavaScript / Framer Motion 驱动的动画，还必须使用已有库的 reduced-motion 能力；仅写 CSS 不能阻止 JavaScript 持续更新 transform：

```tsx
import { motion, useReducedMotion } from "framer-motion";

function PanelEntrance({ children }: { children: React.ReactNode }) {
  const reduceMotion = useReducedMotion();
  return (
    <motion.div
      initial={reduceMotion ? false : { opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduceMotion ? 0 : 0.14 }}
    >
      {children}
    </motion.div>
  );
}
```

只让新出现的面板过渡，不给整个长列表统一 layout 动画；不要为了 hover 反馈给上百张缩略图创建动画实例。

### 6.2 UI-02 · 一份工作台 Shell，明确滚动和宽度归属

```tsx
"use client";

import { useId, type ReactNode } from "react";

export interface WorkbenchShellProps {
  header: ReactNode;
  directory?: ReactNode;
  contextBar?: ReactNode;
  inspector?: ReactNode;
  composer?: ReactNode;
  mode?: "conversation" | "canvas";
  children: ReactNode;
}

export function WorkbenchShell({
  header,
  directory,
  contextBar,
  inspector,
  composer,
  mode = "conversation",
  children,
}: WorkbenchShellProps) {
  const mainId = useId();
  return (
    <div className="workbench-v2 wb-shell" data-app-viewport>
      <a className="wb-skip" href={`#${mainId}`}>跳到工作区</a>
      <div className="wb-global-header">{header}</div>
      <div
        className="wb-body"
        data-directory={directory ? "open" : "closed"}
        data-inspector={inspector ? "open" : "closed"}
      >
        {directory ? (
          <aside className="wb-directory" aria-label="项目与会话目录">
            {directory}
          </aside>
        ) : null}
        <main id={mainId} tabIndex={-1} className="wb-stage" data-mode={mode}>
          <div className="wb-context">{contextBar}</div>
          <div className="wb-content">{children}</div>
          {composer ? <div className="wb-composer-slot">{composer}</div> : null}
        </main>
        {inspector ? (
          <aside className="wb-inspector" aria-label="当前内容的参数与详情">
            {inspector}
          </aside>
        ) : null}
      </div>
    </div>
  );
}
```

```css
.wb-shell {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  /* 由最外层 viewport 适配器提供可用高度，避免重复扣安全区 / 横幅。 */
  height: var(--workbench-available-height, 100dvh);
  min-height: 0;
  overflow: hidden;
}
.wb-skip { position: fixed; left: 12px; top: -120px; z-index: 1000; }
.wb-skip:focus { top: 12px; padding: 10px 16px; background: var(--wb-panel); }
.wb-global-header { border-bottom: 1px solid var(--wb-line); }
.wb-body {
  --directory-width: 248px;
  --inspector-width: 320px;
  display: grid;
  grid-template-columns: var(--directory-width) minmax(0, 1fr) var(--inspector-width);
  min-height: 0;
}
.wb-body[data-directory="closed"] { --directory-width: 0px; }
.wb-body[data-inspector="closed"] { --inspector-width: 0px; }
.wb-directory { grid-column: 1; overflow: auto; border-right: 1px solid var(--wb-line); }
.wb-inspector { grid-column: 3; overflow: auto; border-left: 1px solid var(--wb-line); }
.wb-stage {
  grid-column: 2;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  min-width: 0;
  min-height: 0;
}
.wb-context { min-width: 0; }
.wb-content { min-height: 0; min-width: 0; overflow: auto; scrollbar-gutter: stable; }
.wb-stage[data-mode="canvas"] .wb-content { overflow: hidden; }
.wb-composer-slot { padding: 12px 20px 16px; background: var(--wb-bg); }
.wb-prose { width: min(100%, 800px); margin-inline: auto; padding: 24px; }

@media (max-width: 767px) {
  .wb-composer-slot {
    padding: 8px 12px max(8px, env(safe-area-inset-bottom, 0px));
  }
  .wb-prose { padding: 16px; }
}
```

目录和 Inspector 的 docked / overlay 选择由布局适配器决定；在窄屏下传入空 docked slot，并使用已有 Drawer / Dialog / BottomSheet 呈现对应内容。不要直接将完整的左右栏压成 120px，也不要只把屏外面板视觉藏起来却保留焦点和订阅。

现有 DesktopTopNav 已区分全局操作与页面工具，继续复用其导航可见性和账户状态。新 Shell 不应再额外创建一条不同权限、不同路由的全局导航。

### 6.3 UI-03 · 输入区只承载目标、参考资料和一次明确操作

以下组件是受控展示层：不创建幂等键、不提交裸 fetch、不清空草稿。父控制器负责 B01 中的逻辑操作和响应确认。

```tsx
"use client";

import { useId, type FormEvent, type KeyboardEvent, type ReactNode } from "react";

type ComposerPhase = "idle" | "submitting" | "running" | "stopping" | "uncertain";

interface CompactComposerProps {
  text: string;
  phase: ComposerPhase;
  attachments?: ReactNode;
  hasAttachments: boolean;
  attachmentsPending: boolean;
  parameterSummary: string;
  estimateLabel: string | null;
  error: string | null;
  submitOnEnter: boolean;
  onTextChange: (text: string) => void;
  onAddAttachment: () => void;
  onOpenParameters: () => void;
  onSubmit: () => void;
  onStop: () => void;
  onReconcile: () => void;
}

export function CompactComposer(props: CompactComposerProps) {
  const labelId = useId();
  const helpId = useId();
  const errorId = useId();
  const contentReady = props.text.trim().length > 0 || props.hasAttachments;
  const canSend = props.phase === "idle" && contentReady && !props.attachmentsPending;
  const lockInput = props.phase === "submitting" || props.phase === "stopping";

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (canSend) props.onSubmit();
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing || event.repeat) return;
    if (event.key !== "Enter") return;
    const shortcut = event.metaKey || event.ctrlKey ||
      (props.submitOnEnter && !event.shiftKey && !event.altKey);
    if (!shortcut) return;
    event.preventDefault();
    if (canSend) props.onSubmit();
  }

  return (
    <form className="wb-composer" onSubmit={submit} aria-labelledby={labelId}>
      <label id={labelId} className="wb-composer-label" htmlFor={`${labelId}-input`}>
        {props.phase === "running" ? "准备下一轮" : "描述目标"}
      </label>
      {props.attachments ? <div className="wb-attachments">{props.attachments}</div> : null}
      <textarea
        id={`${labelId}-input`}
        value={props.text}
        rows={3}
        maxLength={10_000}
        disabled={lockInput}
        aria-describedby={`${helpId}${props.error ? ` ${errorId}` : ""}`}
        aria-invalid={props.error ? true : undefined}
        onChange={(event) => props.onTextChange(event.target.value)}
        onKeyDown={keyDown}
        placeholder="例如：参考这两张图片，整理一套适合竖版封面的方案。"
      />
      <div className="wb-composer-footer">
        <div className="wb-composer-tools">
          <button type="button" className="wb-button" disabled={lockInput} onClick={props.onAddAttachment}>
            添加参考
          </button>
          <button type="button" className="wb-button" disabled={lockInput} onClick={props.onOpenParameters}>
            {props.parameterSummary || "参数"}
          </button>
        </div>
        <div className="wb-composer-submit">
          <span className="wb-muted wb-number">
            {props.estimateLabel ? `预计 ${props.estimateLabel}` : "费用待估算"}
          </span>
          {props.phase === "running" || props.phase === "stopping" ? (
            <button
              type="button"
              className="wb-button"
              disabled={props.phase === "stopping"}
              onClick={props.onStop}
            >
              {props.phase === "stopping" ? "正在请求停止" : "停止"}
            </button>
          ) : props.phase === "uncertain" ? (
            <button type="button" className="wb-button" onClick={props.onReconcile}>
              核对提交状态
            </button>
          ) : (
            <button type="submit" className="wb-button" data-tone="primary" disabled={!canSend}>
              {props.phase === "submitting" ? "提交中" : "发送"}
            </button>
          )}
        </div>
      </div>
      <p id={helpId} className="wb-composer-help wb-muted">
        {props.submitOnEnter ? "Enter 发送，Shift+Enter 换行。" : "支持换行，使用发送按钮或 Ctrl / ⌘ + Enter 发送。"}
      </p>
      {props.error ? <p id={errorId} role="alert" className="wb-error">{props.error}</p> : null}
    </form>
  );
}
```

```css
.wb-composer {
  max-width: 880px;
  margin-inline: auto;
  padding: 12px;
  border: 1px solid var(--wb-control-line);
  border-radius: var(--wb-radius-panel);
  background: var(--wb-panel);
}
.wb-composer-label { display: block; margin-bottom: 8px; font-weight: 600; }
.wb-composer textarea {
  display: block;
  width: 100%;
  box-sizing: border-box;
  min-height: 72px;
  max-height: 200px;
  resize: vertical;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--wb-text);
  padding: 4px;
}
.wb-attachments { max-height: 144px; overflow: auto; margin-bottom: 8px; }
.wb-composer-footer, .wb-composer-tools, .wb-composer-submit {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wb-composer-footer { justify-content: space-between; flex-wrap: wrap; padding-top: 8px; }
.wb-composer-tools { min-width: 0; flex-wrap: wrap; }
.wb-composer-submit { margin-left: auto; }
.wb-composer-help { margin: 8px 0 0; font-size: 12px; }
```

估算不可用不一定意味着可以阻断发送：应依据真实计费与配额策略决定。展示层不能因为字符串为空就推断“免费”，也不能让估算请求失败悄悄改变用户的模型设置。

### 6.4 UI-04 · Agent 过程是可折叠的工作记录，不是彩色卡片墙

```tsx
import type { ReactNode } from "react";

export interface ActivityItem {
  id: string; // 来自已归并的 toolCallId / run event identity，不使用数组索引。
  title: string;
  state: "queued" | "running" | "succeeded" | "failed" | "canceled";
  summary: string;
  error?: string;
  details?: ReactNode;
  artifactActions?: ReactNode;
}

const activityLabels: Record<ActivityItem["state"], string> = {
  queued: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "未完成",
  canceled: "已停止",
};

export function RunActivity({ items }: { items: readonly ActivityItem[] }) {
  return (
    <ol className="wb-activity" aria-label="执行过程">
      {items.map((item) => (
        <li key={item.id} data-state={item.state}>
          <details>
            <summary>
              <span className="wb-activity-title">{item.title}</span>
              <span className="wb-muted">{activityLabels[item.state]}</span>
            </summary>
            <div className="wb-activity-detail">
              <p>{item.summary}</p>
              {item.details}
            </div>
          </details>
          {item.error ? <p className="wb-error">{item.error}</p> : null}
          {item.artifactActions ? <div className="wb-artifact-actions">{item.artifactActions}</div> : null}
        </li>
      ))}
    </ol>
  );
}
```

```css
.wb-activity { padding: 0; margin: 16px 0; list-style: none; }
.wb-activity > li { border-bottom: 1px solid var(--wb-line); padding: 8px 0; }
.wb-activity summary { cursor: pointer; padding: 8px 4px; }
.wb-activity-title { font-weight: 500; margin-right: 12px; }
.wb-activity-detail { padding: 0 4px 8px; color: var(--wb-secondary); overflow-wrap: anywhere; }
.wb-artifact-actions { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px; }
```

这个 ActivityItem 只是展示模型。数据应由现有 `model/events.ts`、`reconciliation.ts` 和 store 投影而来，保留 execution epoch、event sequence、tool-call identity；不要把网络到达顺序直接当业务顺序。

“pi 原生体验”的核心是让 pi 负责已经承担的 agent 循环、工具选择与上下文策略，Lumen 做可靠的授权、执行接入、传输和界面投影。不要为了统一卡片外观再包一个独立 planner，或者在前端伪造“思考步骤”。输出只展示系统实际提供且适宜展示的摘要、工具执行和产物。

### 6.5 UI-05 · 把“失败”和“尚不确定”做成不同状态

```tsx
export type OperationView =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "accepted"; label: string }
  | { kind: "running"; label: string }
  | { kind: "cancel-requested" }
  | { kind: "succeeded"; resultLabel: string }
  | { kind: "failed"; message: string; retryable: boolean }
  | { kind: "uncertain"; message: string; checking: boolean };

export function OperationStatus({
  value,
  onRetry,
  onReconcile,
}: {
  value: OperationView;
  onRetry: () => void;
  onReconcile: () => void;
}) {
  if (value.kind === "idle") return null;
  let label: string;
  switch (value.kind) {
    case "submitting": label = "正在提交"; break;
    case "accepted": label = value.label; break;
    case "running": label = value.label; break;
    case "cancel-requested": label = "正在请求停止，等待服务端确认"; break;
    case "succeeded": label = value.resultLabel; break;
    case "failed": label = value.message; break;
    case "uncertain": label = value.message; break;
  }
  return (
    <div className="wb-operation" data-state={value.kind}>
      <p role="status" aria-live="polite" aria-atomic="true">{label}</p>
      {value.kind === "failed" && value.retryable ? (
        <button type="button" className="wb-button" onClick={onRetry}>重试本次操作</button>
      ) : null}
      {value.kind === "uncertain" ? (
        <button type="button" className="wb-button" disabled={value.checking} onClick={onReconcile}>
          {value.checking ? "核对中" : "核对任务状态"}
        </button>
      ) : null}
    </div>
  );
}
```

不要把服务端 run 状态直接替换成这个 union；它是一个显示适配层，状态转换仍由现有版本化事实决定。屏幕阅读器播报阶段变化，不要每个 token 都通过 aria-live 朗读。

费用同理，展示数据应明确区分：

```ts
export type CostView =
  | { kind: "unavailable" }
  | { kind: "estimated"; formatted: string }
  | { kind: "reserved"; formatted: string }
  | { kind: "settled"; formatted: string };

export function costLabel(value: CostView): string {
  switch (value.kind) {
    case "unavailable": return "暂无法估算";
    case "estimated": return `预计 ${value.formatted}`;
    case "reserved": return `已预留 ${value.formatted}`;
    case "settled": return `已结算 ${value.formatted}`;
  }
}
```

这里不计算账单金额，不把浮点数乘来乘去。继续使用服务端定义的金额单位、精度和格式化策略，不能在 UI 重构中悄悄改变计费语义。

### 6.6 UI-06 · 参数进入 Inspector，并写清楚生效范围

```tsx
import { useId, type ReactNode } from "react";

export function ParameterSection({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className="wb-parameter-section" aria-labelledby={id}>
      <h3 id={id}>{title}</h3>
      {description ? <p className="wb-muted">{description}</p> : null}
      <div className="wb-parameter-fields">{children}</div>
    </section>
  );
}

export function GenerationInspector({
  common,
  advanced,
}: {
  common: ReactNode;
  advanced: ReactNode;
}) {
  return (
    <div className="wb-inspector-content">
      <ParameterSection title="生成参数" description="用于下一次提交，不影响已经开始的任务。">
        {common}
      </ParameterSection>
      <details className="wb-advanced">
        <summary>高级参数</summary>
        {advanced}
      </details>
    </div>
  );
}
```

把现有 count / aspect / quality 等受控 Select 接入 `common`，把低频背景、渲染质量等接入 advanced。哪些属于“常用”应根据实际使用数据调整，不应为了整齐把模型能力限制藏起来。

摘要应从同一份草稿推导，不再维护另一份 `summaryState`：

```ts
import type { AgentImageDefaults } from "@/features/agent/model/contracts";

export function imageParameterSummary(value: AgentImageDefaults): string {
  return `${value.count} 张 · ${value.aspect_ratio} · ${value.quality.toUpperCase()}`;
}
```

模型切换导致某参数不支持时，明确提示变化并让用户确认，不使用无声 fallback“兜底跑通”。默认值、用户显式选择、服务端实际采用值应能区分。

### 6.7 UI-07 · 画布保存状态不能只藏在 toast 里

```tsx
export type SaveView =
  | { kind: "saved"; revision: number }
  | { kind: "dirty"; locallyDurable: boolean }
  | { kind: "saving"; locallyDurable: boolean }
  | { kind: "conflict"; hasRecoveryCopy: boolean }
  | { kind: "error"; locallyDurable: boolean };

export function SaveStatus({
  value,
  onRetry,
  onCompare,
  onExportLocal,
}: {
  value: SaveView;
  onRetry: () => void;
  onCompare: () => void;
  onExportLocal: () => void;
}) {
  const label = value.kind === "saved" ? `已保存 · 版本 ${value.revision}`
    : value.kind === "dirty" ? "有尚未同步的修改"
    : value.kind === "saving" ? "正在保存"
    : value.kind === "conflict" ? "版本冲突，已暂停覆盖远端"
    : "未能完成保存";
  const localCopy = "locallyDurable" in value ? value.locallyDurable
    : value.kind === "conflict" ? value.hasRecoveryCopy : false;
  return (
    <div className="wb-save-status">
      <span role="status" aria-live="polite">{label}</span>
      {localCopy ? <span className="wb-muted">本地副本可用</span> : null}
      {value.kind === "error" ? (
        <button type="button" className="wb-button" onClick={onRetry}>重试保存</button>
      ) : null}
      {value.kind === "conflict" ? (
        <button type="button" className="wb-button" onClick={onCompare}>比较版本</button>
      ) : null}
      {value.kind !== "saved" ? (
        <button type="button" className="wb-button" onClick={onExportLocal}>导出当前副本</button>
      ) : null}
    </div>
  );
}
```

SaveView 从现有 Canvas store 和 draft writer 的确认结果推导。不要把“调用了 IndexedDB 写入”当成“已经持久化成功”，也不要因为关闭页面前尝试写入就宣称恢复一定可用。

“比较版本”至少显示远端版本、本地基线、待保存修改数和可选择的动作。在未实现可靠图结构三方合并前，宁可提供副本和明确冲突，不要自动用最后返回的 graph 覆盖用户编辑。

### 6.8 UI-08 · 空、离线和加载失败用同一结构，但不同语义

```tsx
export interface WorkspaceStateProps {
  kind: "empty" | "loading" | "offline" | "error" | "forbidden";
  title: string;
  description: string;
  action?: { label: string; run: () => void };
}

export function WorkspaceState(props: WorkspaceStateProps) {
  return (
    <section className="wb-state" aria-busy={props.kind === "loading"}>
      <h2>{props.title}</h2>
      <p className="wb-muted" role={props.kind === "error" ? "alert" : undefined}>
        {props.description}
      </p>
      {props.action ? (
        <button type="button" className="wb-button" onClick={props.action.run}>
          {props.action.label}
        </button>
      ) : null}
    </section>
  );
}
```

```css
.wb-state { max-width: 420px; margin: auto; padding: 32px 20px; }
.wb-state h2 { font-size: 20px; line-height: 1.4; margin: 0 0 8px; }
.wb-state p { margin: 0 0 20px; }
.wb-save-status, .wb-operation {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 8px 12px;
}
.wb-parameter-section { padding: 20px; border-bottom: 1px solid var(--wb-line); }
.wb-parameter-section h3 { margin: 0 0 8px; font-size: 14px; }
.wb-parameter-section p { margin: 0 0 16px; }
.wb-parameter-fields { display: grid; gap: 16px; }
.wb-advanced { padding: 16px 20px; }
.wb-advanced summary { cursor: pointer; }
```

接入时可直接用当前 `EmptyState`、`ErrorState` 等原语实现同样契约，不要求新建另一个公共组件。空状态必须建立在请求成功且数据为空之上；请求失败不显示“还没有项目”。首屏 skeleton 应保留标题、工具条和内容栏宽度，不要让整页反复变成居中的旋转图标。

### 6.9 UI-09 · 项目与后台列表先保证扫描效率

```tsx
import Link from "next/link";

export interface ProjectRowView {
  id: string;
  href: string; // 由现有路由层提供受信任的内部路径。
  title: string;
  summary: string;
  updatedLabel: string;
  activeTaskLabel?: string;
}

export function ProjectRow({
  item,
  onOpenActions,
}: {
  item: ProjectRowView;
  onOpenActions: (projectId: string) => void;
}) {
  return (
    <li className="wb-project-row">
      <Link href={item.href} className="wb-project-main">
        <strong>{item.title}</strong>
        <span className="wb-muted">{item.summary}</span>
      </Link>
      <span className="wb-muted wb-number">{item.updatedLabel}</span>
      {item.activeTaskLabel ? <span>{item.activeTaskLabel}</span> : <span />}
      <button
        type="button"
        className="wb-button"
        aria-label={`打开 ${item.title} 的操作`}
        onClick={() => onOpenActions(item.id)}
      >
        操作
      </button>
    </li>
  );
}
```

```css
.wb-project-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto auto;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--wb-line);
}
.wb-project-main { min-width: 0; display: grid; gap: 2px; text-decoration: none; }
.wb-project-main strong { color: var(--wb-text); font-weight: 550; }
.wb-project-main span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 767px) {
  .wb-project-row { grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
  .wb-project-row > :nth-child(2), .wb-project-row > :nth-child(3) { grid-column: 1; }
  .wb-project-row > button { grid-column: 2; grid-row: 1 / span 3; }
}
```

普通项目用语义列表；需要比较金额、权限、供应商状态等列数据的后台页面用真正的 table，不把 table 语义替换成一堆随意 div。搜索、筛选、排序写到 URL 的可分享部分；敏感关键词是否允许留在 URL 需按隐私设计决定。

已存在的虚拟列表可继续使用。启用虚拟化前先确认数据规模与行高变化；不要为了“性能优化”让键盘用户无法访问尚未渲染的目标。

### 6.10 UI-10 · 资产卡片：内容优先，交互互不嵌套

```tsx
"use client";

import Image from "next/image";
import Link from "next/link";
import { useState } from "react";

export interface AssetCardView {
  id: string;
  title: string;
  href: string;
  thumbnailUrl: string;
  dimensionsLabel: string;
  sourceLabel: string;
}

function AssetPreview({ asset }: { asset: AssetCardView }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="wb-asset-preview">
      {failed ? <span className="wb-muted">预览暂不可用</span> : (
        <Image
          src={asset.thumbnailUrl}
          alt=""
          fill
          unoptimized
          sizes="(max-width: 767px) 50vw, (max-width: 1199px) 33vw, 280px"
          style={{ objectFit: "contain" }}
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
}

export function AssetCard({
  asset,
  selected,
  onSelect,
  onUseReference,
}: {
  asset: AssetCardView;
  selected: boolean;
  onSelect: (id: string, checked: boolean) => void;
  onUseReference: (id: string) => void;
}) {
  return (
    <article className="wb-asset-card" data-selected={selected}>
      <label className="wb-asset-check wb-touch-target">
        <input
          type="checkbox"
          checked={selected}
          onChange={(event) => onSelect(asset.id, event.target.checked)}
          aria-label={`选择 ${asset.title}`}
        />
      </label>
      <Link className="wb-asset-link" href={asset.href}>
        <AssetPreview key={asset.thumbnailUrl} asset={asset} />
        <div className="wb-asset-info">
          <strong>{asset.title}</strong>
          <span className="wb-muted">{asset.dimensionsLabel} · {asset.sourceLabel}</span>
        </div>
      </Link>
      <div className="wb-asset-actions">
        <button type="button" className="wb-button" onClick={() => onUseReference(asset.id)}>
          用作参考
        </button>
      </div>
    </article>
  );
}
```

```css
.wb-asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }
.wb-asset-card { position: relative; border: 1px solid var(--wb-line); border-radius: 12px; background: var(--wb-panel); }
.wb-asset-card[data-selected="true"] { outline: 2px solid var(--wb-focus); outline-offset: 2px; }
.wb-asset-link { display: block; text-decoration: none; }
.wb-asset-preview { aspect-ratio: 4 / 3; position: relative; display: grid; place-items: center; background: var(--wb-bg); border-radius: 12px 12px 0 0; overflow: hidden; }
.wb-asset-check { position: absolute; top: 4px; left: 4px; z-index: 1; display: grid; place-items: center; width: 44px; height: 44px; border-radius: 8px; background: var(--wb-panel); }
.wb-asset-info { padding: 12px; display: grid; gap: 4px; }
.wb-asset-info strong { color: var(--wb-text); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.wb-asset-info span { font-size: 12px; }
.wb-asset-actions { padding: 0 12px 12px; }
@media (max-width: 767px) {
  .wb-asset-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
}
```

这里用 `unoptimized` 的目的是避免样例擅自改变受保护缩略图的取图链路；实际接入优先继续使用项目已有资产图片组件、签名地址与缓存策略。不要自行拼对象存储 URL，也不要因为缩略图加载失败就把资产误标为删除。

完整图片查看用 contain，资产墙是否 crop 应由设计明确选择，并提供完整预览。下载、复制链接、删除等动作放到同一个菜单里；不要嵌套在已经可点击的 Link / button 内。

### 6.11 UI-11 · 确认对话框围绕“对象和影响”，不是只有一句确定吗

本次追加核对了现有 `ConfirmDialog.tsx`：已有同步执行锁和短窗口防重入，应该复用。不要简单用原生 `window.confirm` 替换，也不要再加第三层点击冷却。

```tsx
"use client";

import { useState } from "react";
import { ConfirmDialog } from "@/components/ui/primitives";

export function DeleteResourceDialog({
  open,
  resource,
  onOpenChange,
  remove,
}: {
  open: boolean;
  resource: { id: string; title: string; impact: string };
  onOpenChange: (open: boolean) => void;
  remove: (id: string) => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setError(null);
    setPending(true);
    try {
      await remove(resource.id);
      onOpenChange(false);
    } catch {
      // API 细节在现有日志 / request-id 机制中保留，不向用户展示原始内部异常。
      setError("未能确认删除结果。请核对列表状态后再操作。");
    } finally {
      setPending(false);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!pending) {
          setError(null);
          onOpenChange(next);
        }
      }}
      title={`删除“${resource.title}”？`}
      description={
        <>
          <p>{resource.impact}</p>
          {error ? <p role="alert" className="wb-error">{error}</p> : null}
        </>
      }
      confirmText="删除"
      cancelText="保留"
      tone="danger"
      confirming={pending}
      onConfirm={confirm}
    />
  );
}
```

由调用方在资源切换时使用 `key={resource.id}`，不要让旧资源错误提示留在新对象确认框里。不可逆动作不要承诺不存在的“撤销”；高风险账务、密钥、管理员动作应继续遵循各自业务授权与审计，不套这个通用删除示例就算完成。

### 6.12 UI-12 · 设置字段：显式名称、作用范围、字段错误

```tsx
import { useId, type InputHTMLAttributes, type ReactNode } from "react";

type SettingsFieldProps = Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "id" | "aria-describedby" | "aria-invalid"
> & {
  label: string;
  description?: ReactNode;
  error?: string | null;
};

export function SettingsField({
  label,
  description,
  error,
  ...inputProps
}: SettingsFieldProps) {
  const id = useId();
  const describedBy = [
    description ? `${id}-help` : null,
    error ? `${id}-error` : null,
  ].filter(Boolean).join(" ") || undefined;

  return (
    <div className="wb-settings-field">
      <label htmlFor={id}>{label}</label>
      {description ? <p id={`${id}-help`} className="wb-muted">{description}</p> : null}
      <input
        {...inputProps}
        id={id}
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
      />
      {error ? <p id={`${id}-error`} role="alert" className="wb-error">{error}</p> : null}
    </div>
  );
}
```

```css
.wb-settings-field { display: grid; gap: 6px; max-width: 640px; }
.wb-settings-field label { font-weight: 550; }
.wb-settings-field p { margin: 0; font-size: 13px; }
.wb-settings-field input {
  box-sizing: border-box;
  min-height: 40px;
  width: 100%;
  border: 1px solid var(--wb-control-line);
  border-radius: 8px;
  background: var(--wb-panel);
  color: var(--wb-text);
  padding: 8px 12px;
}
.wb-settings-field input[aria-invalid="true"] { border-color: var(--wb-danger); }
```

保存策略应按类型选择：视觉偏好适合即时保存；可能影响费用、全局默认模型、密钥或任务行为的设置适合显式保存。失败要保留输入；服务端确认前不要把“正在保存”改成“已保存”。密钥表单不要把真实密钥作为已填默认值送回浏览器，不在成功 toast、URL、埋点里记录凭据。

### 6.13 UI-13 · 保持滚动锚定与 reduced motion 一致

当前 Agent 已有用户离开底部后不强制置底的逻辑，保留它。建议补充图片尺寸变化、折叠工具记录展开、历史前插与 resize 的行为测试后，再决定是否增加内容 ResizeObserver。

先修正明确的动效策略，不要重写整个滚动管理器：

```ts
function preferredScrollBehavior(requestSmooth: boolean): ScrollBehavior {
  const reduceMotion = typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return requestSmooth && !reduceMotion ? "smooth" : "auto";
}

// useAgentScrollManager.ts 的两个 scrollTo 调用分别替换 behavior：
// behavior: preferredScrollBehavior(localSubmission)
// behavior: preferredScrollBehavior(true)
```

若需要 ResizeObserver，应观察**内容尺寸容器**而非仅观察固定高滚动视口，并在“用户仍置底且没有前插锚点”时才调整。不要使用每 50ms 强制 scrollTo 或 setInterval 来压过用户滚动。

---

<a id="architecture"></a>

## 7. 结构重构：减少边界穿透，不再造一个超级 store

### 7.1 建议的职责划分

当前已经存在 features/agent、features/assets、features/generation 等拆分，应在这个基础上继续推进，而不是把所有组件都重命名一遍。

```text
src/
├─ app/                         路由、页面组合、服务端入口
├─ components/ui/primitives/    Button / Dialog / Select 等稳定原语
├─ components/ui/shell/         框架、导航、响应式适配
├─ features/
│  ├─ agent/
│  │  ├─ api/                   DTO 校验、逻辑请求服务
│  │  ├─ model/                 事件归并、版本规则、展示投影
│  │  ├─ containers/            业务协调与现有 hooks
│  │  └─ ui/                    受控、尽可能无副作用的展示组件
│  ├─ assets/                   挑选 / 预览 / 复用契约
│  └─ generation/               提交、参数、结果
├─ lib/api/                     传输、身份、预算、幂等，不感知页面外观
├─ lib/canvas/                  画布图、操作、历史、保存协议
├─ store/                       编辑状态与必要的客户端交互状态
└─ styles/                      主题来源、语义映射、样板迁移层
```

这里是目标职责示意，不是要求移动所有现有文件。删除或合并兼容层前，先查调用者；文件少并不等于耦合少。

### 7.2 三类状态应有不同归属

| 状态 | 建议归属 | 避免的做法 |
| --- | --- | --- |
| 服务端事实：会话、任务、资产、配额 | 既有查询缓存 / 版本化事件投影 | 每个页面复制一份自有任务真源 |
| 编辑状态：未发送草稿、画布操作、选区 | 现有 Zustand / 编辑器 store，按需持久化 | API refetch 直接覆盖正在编辑的值 |
| 临时界面：打开哪个弹窗、搜索输入 | 就近组件或已有 UI store | 为每个 hover、展开状态加入全局 store |

持久化草稿、幂等 key、最终 run ID 和临时 optimistic ID 是不同概念。不要为了“统一 ID”把它们合并成一个字符串。

### 7.3 API 服务层应返回领域结果，而不是让页面猜错误

```ts
// 接入契约示例：用于显示层，不替换现有 ApiError 或传输重试规则。
export type DeliveryOutcome<T> =
  | { kind: "confirmed"; value: T }
  | { kind: "rejected"; code: string; message: string }
  | { kind: "uncertain"; operationId: string; message: string };

export function assertNever(value: never): never {
  throw new Error(`Unhandled state: ${String(value)}`);
}
```

若已有 exception API 足够稳定，不必为了这个 union 全仓改返回类型。可以只在 controller → UI 的投影处引入。核心是不能让 UI 通过 `error.message.includes("timeout")` 自己推断服务端是否创建了任务。

### 7.4 不建议采用的“大重构捷径”

不要同时更换状态库、UI 库、路由架构和动画库。不要在所有未知异常上统一重试，不要用兼容 fallback 消除模型能力差异，不要让后台事件覆盖有版本保护的快照，不要删除看似冗长但实际维护账务 / 身份 / 草稿一致性的边界。

可以减少的是重复状态、散落的默认值、重复组件样式、重复副作用和无边界的职责，而不是删掉正确性约束。

---

<a id="testing"></a>

## 8. 回归测试与 CI 建议

### 8.1 使用现有测试框架

已读取 `apps/web/scripts/run-tests.mjs`：其从 `__tests__` 和 `src` 发现测试文件，通过 Node 内置 test runner 执行。项目也声明了 Playwright。建议沿用，不为了这份报告另引入 Vitest / Jest / 另一套 E2E 平台。

最终脚本以仓库当前 package.json 和锁定 Node 版本为准。以下是建议接入形态，**本轮未执行**：

```bash
# 在已经安装仓库锁定依赖、且具备项目要求的 Node 环境中执行。
cd apps/web
npm test
npx tsc --noEmit
npm run build

# 使用项目既有 Playwright 配置、测试账号和服务启动方式。
npx playwright test
```

不要把本轮隔离环境的 Node 22.16.0 当作项目的推荐最低版本；那只是执行附录脚本时的环境记录。仓库测试中的 TypeScript 加载方式和运行版本应按项目约定执行。

### 8.2 最优先补的集成用例

| 编号 | 用例 | 必须断言的结果 |
| --- | --- | --- |
| T01 | 接收提交后丢失响应，再手动重试 | 同 key、同 run、同逻辑预留，乐观消息不重复 |
| T02 | 504 与连接断开分别发生在提交前 / 后 | 客户端不把无法判断的结果当确定失败 |
| T03 | Agent 读取正文阶段收到 SIGTERM | 不启动新的执行；已有任务按 grace 协调 |
| T04 | 同一图 revision 的迟到 selections 快照 | 当前更高投影版本不被 NaN 分支误覆盖 |
| T05 | 登录过期、切换账号、旧响应晚到 | 私有缓存、草稿和任务不会跨身份写入 |
| T06 | 画布保存成功但 ACK 丢失 | 重试原 mutation，不重复应用图操作 |
| T07 | IndexedDB / localStorage 不可用 | 说明不可用，不显示虚假的恢复成功 |
| T08 | 快速切换会话、返回、打开历史链接 | URL、当前会话、草稿和 SSE scope 一致 |
| T09 | 命令面板键盘与中文输入法 | composition 正确、活动项可见、Tab 不进入 option |
| T10 | 弹窗打开、嵌套、关闭、渲染异常 | 栈顶处理按键，背景 inert，焦点恢复，有恢复入口 |
| T11 | 深浅色和高对比模式 | 有效文字、边界和焦点可辨认；颜色不是唯一状态信号 |
| T12 | 图片晚加载 / 长文本 / 工具记录展开 | 历史阅读不被强制拉到底，布局不遮挡输入区 |
| T13 | 停止请求返回慢或失败 | “请求停止”不提前变成“已停止” |
| T14 | 刷新、关闭标签页、恢复草稿 | 未确认操作和未提交草稿分别恢复，不能重复提交 |
| T15 | 后台配额 / 权限 / 模型能力变更 | UI 重新对齐权威状态，不偷偷沿用过期默认值 |

其中 T05～T08、T12、T14、T15 包含本轮未证实问题的风险覆盖，不表示这些功能当前都坏了。

### 8.3 视觉 / 交互验收矩阵

建议检查 360、390、768、1024、1280、1440 CSS px 宽度，以及 200% 缩放。这些是测试目标，不是本轮已经生成截图的设备列表。

每个尺寸至少检查：主导航、会话侧栏、输入区、参数面板、结果卡片、确认框、错误提示、长标题、中文与英文混排。移动端加键盘展开、横竖屏、safe-area、浏览器工具栏收缩；桌面加键盘连续操作和触控板滚动。

字体或高分辨率屏幕带来的轻微抗锯齿差异不能成为截图测试噪声；但按钮消失、溢出、关键文案截断、焦点不可见必须失败。

### 8.4 行为测试优先于源码字符串测试

源码正则测试适合约束“不得直接调用裸 fetch”“必须引用某组件”等架构规则，但无法证明功能可用。B08 即使存在 aria-activedescendant 也可能不可见；B06 即使字符串包含 tabindex 排除项也仍会被其他选择器分支选中。

新增边界函数应写实际输入输出测试；交互写 Playwright；服务端准入写真实 HTTP 和异步屏障；账务写数据库事务断言。每种测试只证明自己能观察到的那一层。

### 8.5 性能预算：设目标，不虚报结果

本报告没有 Lighthouse、真实用户指标或性能 trace。建议未来建立：输入反馈、长会话更新、资产列表滚动、预览首显、参数面板打开、首屏 JS 体积、重复请求数、资源释放等基线。

采用 UI 动效 120～180ms 这样的规格前，要验证中低端设备。性能优化应以 trace 找到的瓶颈为依据；不要全仓加 useMemo、把所有列表虚拟化或无限 preload 图片。

最有价值的预防是：一个功能只有一个活动订阅者、只预取当前用户确实可访问的资源、清理对象 URL 和观察器、避免长输出每个 token 重建整个 DOM。

---

<a id="migration"></a>

## 9. 分阶段落地，不做一次性大切换

| 阶段 | 提交范围 | 验收门槛 | 回滚边界 |
| --- | --- | --- | --- |
| A | B01 / B02 / B09，先加失败测试再修实现 | 幂等、排空、投影排序测试通过 | 保持服务端协议兼容；不能回滚丢失待确认 key 的存储方案 |
| B | B03～B08、B10～B12 | 键盘、输入法、认证异常、构建与令牌回归 | 小范围回滚；指纹变化需兼容旧待确认操作 |
| C | 单个 Agent / 创作工作台 v2 样板 | 深浅色、移动端、正常 / 错误 / 未知状态完整 | 仅布局切回旧版，业务控制器与草稿不复制 |
| D | 项目、资产、画布、视频复用 Shell / Inspector | 跨页面导航、草稿与任务一致 | 按路由开关，不做账号数据格式迁移 |
| E | 设置与后台收敛，删除旧样式 / 兼容层 | 调用者清单为空、视觉与行为回归通过 | 删除前保留明确依赖关系与迁移记录 |

### 9.1 功能开关建议

开关来自现有运行时默认配置 / 权限系统，不增加未经设计的本地全局开关。只切展示层，避免实例化两套控制器。

```tsx
// 组合示意：controller hooks 只调用一次。
function WorkspacePresentation({
  design,
  legacy,
  redesigned,
}: {
  design: "legacy" | "workbench-v2";
  legacy: React.ReactNode;
  redesigned: React.ReactNode;
}) {
  return design === "workbench-v2" ? redesigned : legacy;
}
```

`legacy` 和 `redesigned` 应接收同一个上层控制器提供的状态与动作。不要把控制器藏到两个 ReactNode 内后同时构造自有服务实例。切换布局可以重新挂载纯展示，但待提交请求、上传和草稿需要留在稳定的业务层。

### 9.2 完成定义

“看起来更新了”不是完成。一个页面完成重构应同时满足：视觉令牌不再重复；主操作可发现；键盘可完成关键流程；输入法正常；loading / empty / error / uncertain 都完整；草稿不因切换丢失；请求身份和幂等未退化；费用状态不撒谎；已有浏览器测试仍通过。

---

<a id="coverage"></a>

## 10. 扫描覆盖与未覆盖范围

### 10.1 本轮重点读取的源码

以下按模块记录，不用目录树的文件数冒充逐文件审计量。区间是本轮请求的源码区间，不是 GitHub 工具包装 JSON 的 L2 行号。

| 模块 | 文件 | 读取范围 / 方式 |
| --- | --- | --- |
| 项目约束 | AGENTS.md、MEMORY.md、apps/web/package.json | 读取配置 / 说明 |
| 全局 Shell | components/LumenAppShell.tsx | 完整 |
| 错误恢复 | components/ErrorBoundary.tsx | 完整 |
| API 兼容层 | lib/api/http.ts、queryClient.ts | 完整 |
| API 传输 | lib/api/transport.ts | 1～260 |
| 登录 API | lib/apiClient.ts | 1～200 |
| 语义幂等 | lib/api/semanticIdempotency.ts | 前部、620～结尾等分段；并非全文件逐行结论 |
| 语义指纹 | lib/api/semanticIdempotencySemantics.ts | 完整 |
| 请求头构造 | lib/api/semanticIdempotencyRequest.ts | 完整 |
| 主题 | app/globals.css | 1～460，另做定向检索；未读完全部 CSS |
| 基础组件 | Button.tsx、Dialog.tsx、ConfirmDialog.tsx | 完整 |
| 焦点管理 | primitives/mobile/useModalLayer.ts | 完整 |
| 导航 | shell/DesktopTopNav.tsx | 完整 |
| 命令面板 | ui/CommandPalette.tsx | 分段覆盖完整文件 |
| Agent 门控 | features/agent/containers/ResponsiveAgent.tsx | 完整 |
| Agent 提交 | containers/agentSubmission.ts、api/agentApi.ts | 完整 |
| Agent 控制器 | containers/AgentWorkspaceController.tsx | 260～510，提交与协调重点 |
| Agent 输入 | ui/AgentComposer.tsx | 1～170、300～560 |
| Agent 参数 | ui/AgentComposerControls.tsx | 完整 |
| Agent 滚动 | containers/useAgentScrollManager.ts | 完整 |
| Canvas 保存原语 | lib/canvas/autosave.ts | 完整 |
| Canvas 投影 | lib/canvas/documentMerge.ts | 完整 |
| Canvas store | lib/canvas/store.ts | 1～210，另检索操作上限调用点 |
| Canvas 持久化 | ui/canvas/CanvasWorkspacePersistence.ts | 1～230 |
| Runtime 入口 | apps/agent-runtime/src/server.ts | 分段覆盖完整文件 |
| Runtime 认证 / 输出 | auth.ts、ndjson.ts、health.ts | 完整 |
| API 认证 | apps/api/app/security.py、deps.py | security.py 完整；deps.py 读取 1～410 |
| Agent 路由 | apps/api/app/routes/agent_sessions.py | 完整 |
| Agent 服务组合 | services/agent/sessions.py | 完整 |
| Agent 后端提交 | services/agent/message_submission.py | 1～250、500～740 |
| 前端测试发现 | apps/web/scripts/run-tests.mjs | 完整 |

Web 路径在表中省略的前缀为 `apps/web/src/`。此外读取 / 查询过多个目录树、调用点和测试片段，用于确认模块存在和排除误报；这些不能算成完整审计了对应目录。

### 10.2 明确未完成的验证

没有运行全仓单元测试、类型检查和正式构建；没有数据库与 Redis 实例；没有真实 Agent provider；没有真实账单核对；没有 Chromium 可执行环境和产品截图；没有完成全量依赖漏洞扫描。

Worker、Telegram bot 应用、完整计费 / 退款 / 结算、所有管理路由、备份恢复、部署升级脚本、迁移和清理任务未做逐模块完整审计。它们是后续审计的独立范围，不是这份前端为主报告可以替代的工作。

### 10.3 本轮额外做过的验证

执行环境：Node v22.16.0、Python 3.13.5。JavaScript 脚本使用内置 assert；Python 认证和对比度使用标准库，选择器验证使用 BeautifulSoup。没有访问生产数据库，没有进行破坏性测试。

| 检查 | 本轮结果 | 证明范围 |
| --- | --- | --- |
| HEAD 覆盖 | 通过，复现 GET | 抽取的调用展开顺序 |
| body await 后停机 | 通过，复现排空后仍启动 | 屏障式控制流模型 |
| IME Escape 顺序 | 通过，复现先关闭 | handler 条件分支 |
| 投影 NaN | 通过，复现无法进入 sequence 比较 | 数值与合并决策 |
| __proto__ 指纹 | 通过，复现两个不同 JSON 指纹相同 | 规范化算法；非全局原型污染 |
| 手动重试新 key | 通过，模型中创建第二个执行 | 按 key 去重与新 key 的关系 |
| 504 分类 | 通过，确认 Agent 与共享分类不同 | 分类条件 |
| Tailwind 相对路径 | 通过，落在 app/src | 路径解析，不是 CSS 构建 |
| compare_digest 非 ASCII | 通过，捕获 TypeError | Python 标准库行为 |
| 安全 ASCII 比较 | 通过，异常值被正常拒绝 | 建议比较函数 |
| focus selector | 通过，选中负 tabindex 与 disabled 示例 | CSS 选择器匹配，不是浏览器 Tab 行为 |
| 颜色组合 | 通过，得到 B12 表内比值 | 不透明令牌数学，不是整页 axe 扫描 |

另外，对文档中的 37 个 JavaScript / TypeScript / TSX 代码块执行了 TypeScript 转译语法检查，未发现语法诊断错误；Python 片段通过 AST 解析，JSON 结果通过解析，内部导航与代码围栏也已检查。**这不是跨模块类型检查，也不证明所有接入参考代码已在 Lumen 编译通过。**

后附完整脚本，便于复核这些检查到底测了什么。脚本中的简化模型不包含完整业务状态，不能拿它替代前面的 T01～T15。

---

## 11. 官方依据与固定提交源码入口

### 11.1 官方依据

- [Python hmac.compare_digest](https://docs.python.org/3/library/hmac.html)：ASCII 字符串 / 相同类型字节比较的前提。
- [Tailwind 4 source detection](https://tailwindcss.com/docs/detecting-classes-in-source-files)：显式 source 路径与 source(none)。
- [React Component / Error boundaries](https://react.dev/reference/react/Component)：错误边界状态与捕获范围。
- [WAI-ARIA Dialog pattern](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)：模态框的焦点与键盘预期。
- [WCAG 2.2 Contrast Minimum](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)：正文对比度目标与例外。

报告中的设计方向是针对 Lumen 的建议，不是假称这些官方文档规定了某种“高级风格”。

### 11.2 源码定位方法

本报告所有源码问题均以固定 SHA 为准。行数会随后续提交变化，因此每项同时给出路径和函数名。若之后 main 已变化，应先比较变更再应用建议，不能对新版直接套用旧结论。

固定提交浏览入口：

```text
https://github.com/cyeinfpro/Lumen/tree/3278e6da2f5c9c31e0342a8e9fa58eb7c4d6c026
```

代码位置链接已放在相应问题段落中；下方执行脚本是本轮另行编写的检查文件，不属于该 GitHub 提交。

---

<a id="reproductions"></a>

## 附录 A · 可复核的最小检查脚本

### A.1 JavaScript

保存为 `repro.mjs`，用 Node 执行：`node repro.mjs`。每个检查的 passed 表示断言与预期一致；对于旧实现，预期通常是“成功复现缺陷”，不是“旧代码没有问题”。

```js
// Isolated reproductions of extracted control-flow/algorithms, NOT repository integration tests.
import assert from 'node:assert/strict';
import { resolve, dirname } from 'node:path';
const results = [];
const check = async (name, run) => { await run(); results.push({name,status:'passed'}); };

await check('HEAD is overwritten by queryClient.get', () => {
  const transport = (init) => init.method;
  const get = (options) => transport({...options, method: 'GET', requestClass: 'query'});
  const legacy = (init) => ['GET','HEAD'].includes(init.method) ? get(init) : null;
  assert.equal(legacy({method:'HEAD'}), 'GET');
  const patched = (init) => init.method === 'HEAD' ? transport({...init,method:'HEAD',expectNoContent:true}) : get(init);
  assert.equal(patched({method:'HEAD'}), 'HEAD');
});

await check('Admission after body await needs a fresh drain check', async () => {
  async function scenario(patched) {
    let draining=false, starts=0, release;
    const body = new Promise(r => release=r);
    const admission = (async () => {
      if (draining) return false;
      await body;
      if (patched && draining) return false;
      starts++; return true;
    })();
    draining=true; release();
    await admission;
    return starts;
  }
  assert.equal(await scenario(false),1);
  assert.equal(await scenario(true),0);
});

await check('IME Escape closes palette before composition guard', () => {
  function legacy(e) {
    if(e.key==='Escape') return 'closed';
    if(e.nativeEvent.isComposing) return 'unchanged';
    return 'unchanged';
  }
  const e={key:'Escape',nativeEvent:{isComposing:true}};
  assert.equal(legacy(e),'closed');
  const fixed=e=>e.nativeEvent.isComposing?'unchanged':legacy(e);
  assert.equal(fixed(e),'unchanged');
});

function order(a,b) { return a===b?0:a>b?1:-1; }
function docVersion(doc) {
  return {
    timestamp:Math.max(...doc.recent_executions.map(e=>Date.parse(e.updated_at)),
      ...doc.active_runs.map(r=>Date.parse(r.updated_at)),Number.NEGATIVE_INFINITY),
    sequence:Math.max(...doc.selections.map(s=>s.revision),
      ...doc.active_runs.map(r=>r.last_event_seq),Number.NEGATIVE_INFINITY)
  };
}
function compareLegacy(a,b) {
  const x=docVersion(a),y=docVersion(b),td=x.timestamp-y.timestamp;
  return td!==0?td:x.sequence-y.sequence;
}
function compareFixed(a,b) {
  const x=docVersion(a),y=docVersion(b);
  return order(x.timestamp,y.timestamp)||order(x.sequence,y.sequence);
}
await check('Empty projection timestamps yield NaN and drop missing selections', () => {
  const base={revision:1,recent_executions:[],active_runs:[]};
  const current={...base,selections:[{node_id:'a',revision:5},{node_id:'b',revision:3}]};
  const incoming={...base,selections:[{node_id:'a',revision:4}]};
  assert.equal(Number.isNaN(compareLegacy(current,incoming)),true);
  assert.equal(compareLegacy(current,incoming)>0,false);
  assert.equal(compareFixed(current,incoming),1);
  assert.equal(order(-Infinity,-Infinity),0);
});

function sortLegacy(value) {
  if(Array.isArray(value)) return value.map(sortLegacy);
  if(value===null || typeof value!=='object') return value;
  const sorted={};
  for(const key of Object.keys(value).sort()) sorted[key]=sortLegacy(value[key]);
  return sorted;
}
function sortFixed(value) {
  if(Array.isArray(value)) return value.map(sortFixed);
  if(value===null || typeof value!=='object') return value;
  const sorted=Object.create(null);
  for(const key of Object.keys(value).sort()) sorted[key]=sortFixed(value[key]);
  return sorted;
}
await check('Own __proto__ key is lost from semantic fingerprint', () => {
  const a=JSON.parse('{"__proto__":{"x":1},"text":"same"}');
  const b=JSON.parse('{"__proto__":{"x":2},"text":"same"}');
  assert.equal(JSON.stringify(sortLegacy(a)),JSON.stringify(sortLegacy(b)));
  assert.notEqual(JSON.stringify(sortFixed(a)),JSON.stringify(sortFixed(b)));
  assert.equal(Object.hasOwn(sortFixed(a),'__proto__'),true);
  assert.equal(Object.prototype.x,undefined); // not global prototype pollution
});

await check('Fresh key on manual resend bypasses key-based deduplication', () => {
  const accepted = new Map(); let executions=0;
  const server=key=>{if(!accepted.has(key)) accepted.set(key,++executions);return accepted.get(key)};
  server('first-key'); server('first-key'); // same-attempt automatic replay
  assert.equal(executions,1);
  server('new-manual-key');
  assert.equal(executions,2);
});

await check('Agent delivery classifier misses a 504 compared to shared policy', () => {
  const e={status:504,code:'upstream_error'};
  const legacy=e=>e.status===0||e.code==='network_error'||e.code==='request_timeout';
  const shared=e=>e.status===408||e.status===425||e.status===429||e.status>=500;
  assert.equal(legacy(e),false); assert.equal(shared(e),true);
});

await check('Tailwind source path resolves below app instead of web/src', () => {
  const sheet='/repo/apps/web/src/app/globals.css';
  assert.equal(resolve(dirname(sheet),'./src'),'/repo/apps/web/src/app/src');
  assert.equal(resolve(dirname(sheet),'../'),'/repo/apps/web/src');
});
console.log(JSON.stringify({kind:'isolated algorithm/control-flow reproductions',node:process.version,tests:results},null,2));
```

### A.2 Python

保存为 `repro.py`。需安装 `beautifulsoup4` 才能执行选择器一项；认证与颜色计算本身使用标准库。运行 `python repro.py`，结果写入脚本同目录。

```python
"""Isolated standard-library, selector and token-math checks; not FastAPI/browser tests."""
import hmac, json, sys
from pathlib import Path
from bs4 import BeautifulSoup

def contrast(a,b):
    def luminance(h):
        channels=[int(h[i:i+2],16)/255 for i in (1,3,5)]
        c=[x/12.92 if x<=0.04045 else ((x+.055)/1.055)**2.4 for x in channels]
        return sum(x*y for x,y in zip(c,(.2126,.7152,.0722)))
    a,b=sorted((luminance(a),luminance(b)))
    return (b+.05)/(a+.05)

checks=[]
try:
    hmac.compare_digest('é','0'*64)
    raise AssertionError('Expected TypeError')
except TypeError:
    checks.append({'name':'Non-ASCII string in compare_digest raises TypeError','status':'passed'})

def safe_compare(a,b):
    return bool(a and b and a.isascii() and b.isascii() and hmac.compare_digest(a,b))
assert safe_compare('é','0'*64) is False
assert safe_compare('a'*64,'a'*64) is True
assert safe_compare('a'*64,'b'*64) is False
checks.append({'name':'ASCII validation makes malformed input reject cleanly','status':'passed'})

selector=','.join(['a[href]','button:not([disabled])','textarea:not([disabled])',
 'input:not([disabled]):not([type="hidden"])','select:not([disabled])','summary',
 '[contenteditable="true"]','[tabindex]:not([tabindex="-1"])'])
soup=BeautifulSoup('<input id="query"><button id="close">Close</button><button id="option" tabindex="-1">Result</button><button id="disabled" disabled tabindex="0">No</button>','html.parser')
ids=[el['id'] for el in soup.select(selector)]
assert 'option' in ids and 'disabled' in ids
checks.append({'name':'Current modal selector includes tabindex=-1 and disabled tabindex=0','status':'passed','matches':ids,'scope':'CSS selector matching, not browser focus behavior'})
ratios={
 'current_info_on_white':contrast('#3E9EFF','#FFFFFF'),
 'current_info_on_canvas_light':contrast('#3E9EFF','#F4F5F7'),
 'proposed_info_fg_on_white':contrast('#0D74CE','#FFFFFF'),
 'proposed_info_fg_on_canvas_light':contrast('#0D74CE','#F4F5F7'),
 'command_group_fg3_on_dark_panel':contrast('#5E5951','#121318'),
 'command_group_fg3_on_light_panel':contrast('#A7ADB7','#EAECF0'),
}
assert ratios['current_info_on_white']<4.5
assert ratios['proposed_info_fg_on_white']>=4.5
checks.append({'name':'Color-token contrast calculation','status':'passed','ratios':ratios,'scope':'opaque token combinations, not computed browser styles'})
Path(__file__).resolve().with_name('python-results.json').write_text(json.dumps({'python':sys.version.split()[0],'tests':checks},ensure_ascii=False,indent=2))
print(json.dumps(checks,ensure_ascii=False,indent=2))
```

### A.3 本轮执行结果

下面是本轮脚本产生的 JSON，保留原始检查名称与限制说明。

```json
{
  "kind": "isolated algorithm/control-flow reproductions",
  "node": "v22.16.0",
  "tests": [
    {
      "name": "HEAD is overwritten by queryClient.get",
      "status": "passed"
    },
    {
      "name": "Admission after body await needs a fresh drain check",
      "status": "passed"
    },
    {
      "name": "IME Escape closes palette before composition guard",
      "status": "passed"
    },
    {
      "name": "Empty projection timestamps yield NaN and drop missing selections",
      "status": "passed"
    },
    {
      "name": "Own __proto__ key is lost from semantic fingerprint",
      "status": "passed"
    },
    {
      "name": "Fresh key on manual resend bypasses key-based deduplication",
      "status": "passed"
    },
    {
      "name": "Agent delivery classifier misses a 504 compared to shared policy",
      "status": "passed"
    },
    {
      "name": "Tailwind source path resolves below app instead of web/src",
      "status": "passed"
    }
  ]
}
```

```json
{
  "python": "3.13.5",
  "tests": [
    {
      "name": "Non-ASCII string in compare_digest raises TypeError",
      "status": "passed"
    },
    {
      "name": "ASCII validation makes malformed input reject cleanly",
      "status": "passed"
    },
    {
      "name": "Current modal selector includes tabindex=-1 and disabled tabindex=0",
      "status": "passed",
      "matches": [
        "query",
        "close",
        "option",
        "disabled"
      ],
      "scope": "CSS selector matching, not browser focus behavior"
    },
    {
      "name": "Color-token contrast calculation",
      "status": "passed",
      "ratios": {
        "current_info_on_white": 2.7853045362505586,
        "current_info_on_canvas_light": 2.5532836957181204,
        "proposed_info_fg_on_white": 4.765669927751281,
        "proposed_info_fg_on_canvas_light": 4.36868111451885,
        "command_group_fg3_on_dark_panel": 2.670979648675614,
        "command_group_fg3_on_light_panel": 1.9082477717305895
      },
      "scope": "opaque token combinations, not computed browser styles"
    }
  ]
}
```

### A.4 补充颜色校验

下面为两种候选链接色、已有 muted 与 warning 文字色在当前浅色背景上的对比度；只用于配色决策，不表示这些组合全部适用于所有组件。

```json
{
  "#07549C": {
    "#FFFFFF": 7.6164,
    "#F4F5F7": 6.9819,
    "#EAECF0": 6.4394,
    "#DDE0E5": 5.7551
  },
  "#075DA8": {
    "#FFFFFF": 6.6875,
    "#F4F5F7": 6.1304,
    "#EAECF0": 5.6541,
    "#DDE0E5": 5.0532
  },
  "#5D646E": {
    "#FFFFFF": 5.977,
    "#F4F5F7": 5.4791,
    "#EAECF0": 5.0534,
    "#DDE0E5": 4.5164
  },
  "#9F5700": {
    "#FFFFFF": 5.4724,
    "#F4F5F7": 5.0165,
    "#EAECF0": 4.6267,
    "#DDE0E5": 4.1351
  }
}
```

## 最终建议

先让“提交过什么、当前在做什么、数据是否已保存、失败后怎么办”都可信，再做视觉升级。对于 Lumen，减少不确定性比增加任何一层毛玻璃更能建立成熟产品的质感。

本报告提供了本轮能证实的问题与一套可分阶段实施的重构方案；完整上线验收仍需在固定依赖、真实浏览器、数据库和运行环境中完成。
