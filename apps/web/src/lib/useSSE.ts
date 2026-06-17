"use client";

// DESIGN 附录 B：前端 SSE hook
// - 同一 channels key 复用一个模块级 EventSource；订阅者 ref-count 归零后关闭
// - 带 withCredentials（browser 自动带 cookie；服务端 CORS 需 Access-Control-Allow-Credentials: true）
// - 断线自动重连（指数退避 1s→2s→4s，上限 30s）。手动重建 EventSource 时用 last_event_id
//   查询参数恢复浏览器原生 Last-Event-ID 的 replay 语义。
// - 页面 visibilitychange：hidden 延迟 close，visible → open（节流）
// - 调用方自己在 handlers 里 dispatch（不在 hook 里耦合 store）

import { useEffect, useMemo, useRef, useState } from "react";
import { sseUrl } from "./apiClient";
import { logError } from "./logger";

// 规避 React 19 的 "Cannot update ref during render" 检查：在 effect 内同步 ref。

export type SSEHandler = (data: unknown, id: string) => void;

export interface SSEHandlers {
  [eventName: string]: SSEHandler;
}

// 模块级注册表：允许调用方在 useSSE 之外按事件名挂全局 handler（通常用于
// 上游新增的事件类型，例如 reasoning_summary / compaction_summary）。
// 这里只暴露能力，不在本文件主动调用——保持与 useSSE 局部 handlers 解耦。
const globalSSEHandlers: Map<string, Set<SSEHandler>> = new Map();

/**
 * 注册一个全局 SSE handler。返回取消注册的函数。
 * 被注册的 handler 由 useSSE 在收到匹配事件时调用，且不会替代局部 handlers。
 * 若同一事件类型存在多个 handler，按注册顺序依次调用，单个 handler 抛错不影响其他。
 */
export function registerSSEHandler(type: string, handler: SSEHandler): () => void {
  if (!type || typeof handler !== "function") {
    return () => {};
  }
  let set = globalSSEHandlers.get(type);
  if (!set) {
    set = new Set();
    globalSSEHandlers.set(type, set);
  }
  set.add(handler);
  for (const connection of sharedSSEConnections.values()) {
    connection.ensureNamedListener(type);
  }
  return () => {
    const cur = globalSSEHandlers.get(type);
    if (!cur) return;
    cur.delete(handler);
    if (cur.size === 0) globalSSEHandlers.delete(type);
    for (const connection of sharedSSEConnections.values()) {
      connection.syncNamedListeners();
    }
  };
}

// 未知事件类型 console.warn 去重缓存：每种 type 只警告一次，避免日志风暴。
const warnedUnknownTypes: Set<string> = new Set();
const sharedSSEConnections: Map<string, SharedSSEConnection> = new Map();

export type SSEStatus = "connecting" | "open" | "closed" | "error";

export interface UseSSEOptions {
  onOpen?: (ev: Event) => void;
  onError?: (ev: Event) => void;
  hiddenCloseDelayMs?: number;
  maxRetryCount?: number;
}

const DEFAULT_HIDDEN_CLOSE_DELAY_MS = 30_000;
const DEFAULT_DESKTOP_MAX_RETRY_COUNT = 20;
// 4K 生图 10+ min 期间网络抖动可能耗光重试；5 次太紧会让前台移动用户提早看到 error。
// 退避封顶 30s × 20 次 ≈ 10 min；之后 visibilitychange 仍会重置重连。
const DEFAULT_MOBILE_MAX_RETRY_COUNT = 20;

function defaultMaxRetryCount(): number {
  if (typeof window === "undefined") return DEFAULT_DESKTOP_MAX_RETRY_COUNT;
  const coarsePointer = window.matchMedia?.("(pointer: coarse)").matches ?? false;
  if (coarsePointer || navigator.maxTouchPoints > 0) {
    return DEFAULT_MOBILE_MAX_RETRY_COUNT;
  }
  return DEFAULT_DESKTOP_MAX_RETRY_COUNT;
}

function initialStatus(): SSEStatus {
  // BUG-020: SSR 期间 document 不可用，返回安全默认值 "hidden"（closed），
  // 避免 hydration mismatch。客户端挂载后由 effect 切换到真实状态。
  if (typeof document === "undefined") return "closed";
  if (document.visibilityState === "hidden") return "closed";
  return "connecting";
}

interface SSESubscriber {
  handlersRef: { current: SSEHandlers };
  onOpenRef: { current: ((ev: Event) => void) | undefined };
  onErrorRef: { current: ((ev: Event) => void) | undefined };
  hiddenCloseDelayRef: { current: number };
  maxRetryCountRef: { current: number };
  eventNames: Set<string>;
  setStatus: (status: SSEStatus) => void;
}

function computeJitteredDelay(baseDelay: number): number {
  let ratio: number;
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const arr = new Uint8Array(1);
    crypto.getRandomValues(arr);
    ratio = ((arr[0] % 40) + 80) / 100; // 0.80-1.19
  } else {
    ratio = 0.8 + Math.random() * 0.4;
  }
  return Math.round(baseDelay * ratio);
}

class SharedSSEConnection {
  private es: EventSource | null = null;
  private retryAttempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private hiddenCloseTimer: ReturnType<typeof setTimeout> | null = null;
  private hiddenAt: number | null = null;
  private namedListeners = new Map<string, (ev: MessageEvent) => void>();
  private subscribers = new Set<SSESubscriber>();
  private disposed = false;
  private connectionSeq = 0;
  private status: SSEStatus = "connecting";
  private lastEventId: string | null = null;
  private readonly channelList: string[];
  private readonly onVisibility = () => {
    if (document.visibilityState === "hidden") {
      this.hiddenAt = Date.now();
      this.clearHiddenCloseTimer();
      this.hiddenCloseTimer = setTimeout(() => {
        if (this.disposed || document.visibilityState !== "hidden") return;
        this.closeEventSource();
        this.notifyStatus("closed");
      }, this.hiddenCloseDelayMs());
      return;
    }

    const wasHiddenForMs =
      this.hiddenAt == null ? 0 : Date.now() - this.hiddenAt;
    this.hiddenAt = null;
    this.clearHiddenCloseTimer();
    if (
      wasHiddenForMs >= this.hiddenCloseDelayMs() ||
      this.retryAttempt >= this.maxRetryCount()
    ) {
      this.retryAttempt = 0;
      this.open();
    } else if (!this.es || this.es.readyState === EventSource.CLOSED) {
      this.retryAttempt = 0;
      this.open();
    } else if (this.es.readyState === EventSource.OPEN) {
      this.notifyStatus("open");
    } else {
      this.notifyStatus("connecting");
    }
  };

  constructor(private readonly channelKey: string) {
    this.channelList = channelKey.split(",").filter(Boolean);
  }

  subscribe(subscriber: SSESubscriber): () => void {
    this.subscribers.add(subscriber);
    subscriber.setStatus(this.status);
    this.syncNamedListeners();

    if (this.subscribers.size === 1) {
      this.disposed = false;
      document.addEventListener("visibilitychange", this.onVisibility);
      if (document.visibilityState === "visible") {
        this.open();
      } else {
        setTimeout(() => {
          if (!this.disposed) this.notifyStatus("closed");
        }, 0);
      }
    } else if (this.es) {
      this.syncNamedListeners();
    }

    return () => {
      this.subscribers.delete(subscriber);
      if (this.subscribers.size > 0) {
        this.syncNamedListeners();
        return;
      }
      this.dispose();
      sharedSSEConnections.delete(this.channelKey);
    };
  }

  ensureNamedListener(name: string): void {
    if (!this.es || this.namedListeners.has(name)) return;
    if (name === "message" || name === "open" || name === "error") return;
    const listener = (ev: MessageEvent) => {
      const seq = this.connectionSeq;
      if (this.disposed || seq !== this.connectionSeq) return;
      this.dispatchNamed(ev);
    };
    this.namedListeners.set(name, listener);
    this.es.addEventListener(name, listener);
  }

  syncNamedListeners(): void {
    const eventNames = new Set<string>();
    for (const subscriber of this.subscribers) {
      for (const name of subscriber.eventNames) eventNames.add(name);
    }
    for (const name of globalSSEHandlers.keys()) eventNames.add(name);
    for (const [name, listener] of Array.from(this.namedListeners)) {
      if (eventNames.has(name)) continue;
      if (this.es) {
        try {
          this.es.removeEventListener(name, listener);
        } catch {
          /* ignore */
        }
      }
      this.namedListeners.delete(name);
    }
    for (const name of eventNames) this.ensureNamedListener(name);
  }

  private notifyStatus(status: SSEStatus): void {
    this.status = status;
    for (const subscriber of Array.from(this.subscribers)) {
      subscriber.setStatus(status);
    }
  }

  private clearRetryTimer(): void {
    if (!this.retryTimer) return;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private clearHiddenCloseTimer(): void {
    if (!this.hiddenCloseTimer) return;
    clearTimeout(this.hiddenCloseTimer);
    this.hiddenCloseTimer = null;
  }

  private hiddenCloseDelayMs(): number {
    let delay = DEFAULT_HIDDEN_CLOSE_DELAY_MS;
    for (const subscriber of this.subscribers) {
      delay = Math.min(delay, subscriber.hiddenCloseDelayRef.current);
    }
    return delay;
  }

  private maxRetryCount(): number {
    let count = 0;
    for (const subscriber of this.subscribers) {
      count = Math.max(count, subscriber.maxRetryCountRef.current);
    }
    return count || defaultMaxRetryCount();
  }

  private closeEventSource(): void {
    this.connectionSeq += 1;
    this.clearRetryTimer();
    this.clearHiddenCloseTimer();
    if (this.es) {
      for (const [name, listener] of this.namedListeners) {
        try {
          this.es.removeEventListener(name, listener);
        } catch {
          /* ignore */
        }
      }
      this.es.onopen = null;
      this.es.onmessage = null;
      this.es.onerror = null;
      try {
        if (this.es.readyState !== EventSource.CLOSED) {
          this.es.close();
        }
      } catch {
        /* ignore */
      }
      this.es = null;
    }
    this.namedListeners.clear();
  }

  private open(): void {
    if (this.disposed || this.subscribers.size === 0) return;
    this.closeEventSource();
    const seq = this.connectionSeq;
    this.notifyStatus("connecting");
    try {
      this.es = new EventSource(sseUrl(this.channelList, this.lastEventId), {
        withCredentials: true,
      });
    } catch (err) {
      logError(err, { scope: "useSSE", extra: { phase: "open" } });
      this.notifyStatus("error");
      this.scheduleRetry();
      return;
    }

    this.es.onopen = (ev) => {
      if (this.disposed || seq !== this.connectionSeq) return;
      this.retryAttempt = 0;
      this.notifyStatus("open");
      for (const subscriber of Array.from(this.subscribers)) {
        try {
          subscriber.onOpenRef.current?.(ev);
        } catch (cbErr) {
          logError(cbErr, {
            scope: "useSSE",
            extra: { phase: "onOpen-callback" },
          });
        }
      }
    };

    this.es.onmessage = (ev: MessageEvent) => {
      if (this.disposed || seq !== this.connectionSeq) return;
      this.dispatchNamed(ev);
    };

    this.es.onerror = (ev) => {
      if (this.disposed || seq !== this.connectionSeq) return;
      this.notifyStatus("error");
      for (const subscriber of Array.from(this.subscribers)) {
        try {
          subscriber.onErrorRef.current?.(ev);
        } catch (cbErr) {
          logError(cbErr, {
            scope: "useSSE",
            extra: { phase: "onError-callback" },
          });
        }
      }
      this.closeEventSource();
      this.scheduleRetry();
    };

    this.syncNamedListeners();
  }

  private scheduleRetry(): void {
    if (this.disposed || this.subscribers.size === 0) return;
    this.clearRetryTimer();
    if (this.retryAttempt >= this.maxRetryCount()) {
      this.notifyStatus("error");
      return;
    }
    const baseDelay = Math.min(30_000, 1000 * 2 ** this.retryAttempt);
    const delay = Math.min(30_000, computeJitteredDelay(baseDelay));
    this.retryAttempt += 1;
    this.retryTimer = setTimeout(() => {
      if (document.visibilityState === "visible") {
        this.open();
      }
    }, delay);
  }

  private dispatchNamed(ev: MessageEvent): void {
    const name = ev.type;
    if (ev.lastEventId) this.lastEventId = ev.lastEventId;
    const globalSet = globalSSEHandlers.get(name);
    let delivered = false;

    let parsed: unknown = ev.data;
    if (typeof ev.data === "string") {
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        parsed = ev.data;
      }
    }

    for (const subscriber of Array.from(this.subscribers)) {
      if (!subscriber.eventNames.has(name)) continue;
      const localFn = subscriber.handlersRef.current[name];
      if (typeof localFn !== "function") continue;
      delivered = true;
      try {
        localFn(parsed, ev.lastEventId);
      } catch (err) {
        logError(err, { scope: "useSSE", extra: { event: name } });
      }
    }

    if (globalSet && globalSet.size > 0) {
      delivered = true;
      for (const fn of Array.from(globalSet)) {
        try {
          fn(parsed, ev.lastEventId);
        } catch (err) {
          logError(err, {
            scope: "useSSE",
            extra: { event: name, source: "global" },
          });
        }
      }
    }

    if (delivered) return;
    if (
      name &&
      name !== "message" &&
      name !== "open" &&
      name !== "error" &&
      !warnedUnknownTypes.has(name)
    ) {
      warnedUnknownTypes.add(name);
      try {
        console.warn(
          `[useSSE] unknown event type '${name}' (no handler). Future events of this type will be silently ignored.`,
        );
      } catch {
        /* console 不可用时忽略 */
      }
    }
  }

  private dispose(): void {
    this.disposed = true;
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.closeEventSource();
    this.notifyStatus("closed");
  }
}

function acquireSharedSSEConnection(channelKey: string): SharedSSEConnection {
  let connection = sharedSSEConnections.get(channelKey);
  if (!connection) {
    connection = new SharedSSEConnection(channelKey);
    sharedSSEConnections.set(channelKey, connection);
  }
  return connection;
}

export function useSSE(
  channels: string[],
  handlers: SSEHandlers,
  opts?: UseSSEOptions,
): { status: SSEStatus } {
  const [status, setStatus] = useState<SSEStatus>(initialStatus);
  // handlers 放入 ref；同步到最新在 effect 里做，避免 "update ref during render"。
  const handlersRef = useRef<SSEHandlers>(handlers);
  const onOpenRef = useRef<((ev: Event) => void) | undefined>(opts?.onOpen);
  const onErrorRef = useRef<((ev: Event) => void) | undefined>(opts?.onError);
  const hiddenCloseDelayRef = useRef<number>(
    opts?.hiddenCloseDelayMs ?? DEFAULT_HIDDEN_CLOSE_DELAY_MS,
  );
  const maxRetryCountRef = useRef<number>(
    opts?.maxRetryCount ?? defaultMaxRetryCount(),
  );
  // 每次 render 后同步 ref。inline `{}` / 裸函数每次 render 引用都不同,
  // 写依赖数组反而每次都触发；省略 deps 等价 "每次 render 之后跑",
  // 是 React 文档明确认可的 ref 同步写法（render 阶段本身不访问 ref）。
  useEffect(() => {
    handlersRef.current = handlers;
    onOpenRef.current = opts?.onOpen;
    onErrorRef.current = opts?.onError;
    hiddenCloseDelayRef.current =
      opts?.hiddenCloseDelayMs ?? DEFAULT_HIDDEN_CLOSE_DELAY_MS;
    maxRetryCountRef.current = opts?.maxRetryCount ?? defaultMaxRetryCount();
  });

  // channels 用 sorted-join 做稳定 key：顺序不同也不视为变化。
  const channelKey = useMemo(() => [...channels].sort().join(","), [channels]);
  const handlerEventKey = useMemo(() => Object.keys(handlers).sort().join(","), [handlers]);

  useEffect(() => {
    if (
      !channelKey ||
      typeof window === "undefined" ||
      typeof EventSource === "undefined"
    ) {
      // 异步置 "closed"，不在 effect 同步阶段触发 setState 级联。
      const t = setTimeout(() => setStatus("closed"), 0);
      return () => clearTimeout(t);
    }

    const subscriber: SSESubscriber = {
      handlersRef,
      onOpenRef,
      onErrorRef,
      hiddenCloseDelayRef,
      maxRetryCountRef,
      eventNames: new Set(handlerEventKey.split(",").filter(Boolean)),
      setStatus,
    };
    return acquireSharedSSEConnection(channelKey).subscribe(subscriber);
  }, [channelKey, handlerEventKey]);

  return { status };
}
