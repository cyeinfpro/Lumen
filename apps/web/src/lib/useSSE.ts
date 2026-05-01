"use client";

// DESIGN 附录 B：前端 SSE hook
// - 一个组件实例一个 EventSource；channels 变化时 close+open
// - 带 withCredentials（browser 自动带 cookie；服务端 CORS 需 Access-Control-Allow-Credentials: true）
// - 断线自动重连（指数退避 1s→2s→4s，上限 30s）。浏览器原生 EventSource 会在重连时带 Last-Event-ID
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
  return () => {
    const cur = globalSSEHandlers.get(type);
    if (!cur) return;
    cur.delete(handler);
    if (cur.size === 0) globalSSEHandlers.delete(type);
  };
}

// 未知事件类型 console.warn 去重缓存：每种 type 只警告一次，避免日志风暴。
const warnedUnknownTypes: Set<string> = new Set();

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

    let es: EventSource | null = null;
    let retryAttempt = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let hiddenCloseTimer: ReturnType<typeof setTimeout> | null = null;
    let hiddenAt: number | null = null;
    const namedListeners = new Map<string, (ev: MessageEvent) => void>();
    let disposed = false;

    const clearRetryTimer = () => {
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const clearHiddenCloseTimer = () => {
      if (hiddenCloseTimer) {
        clearTimeout(hiddenCloseTimer);
        hiddenCloseTimer = null;
      }
    };

    const close = () => {
      clearRetryTimer();
      clearHiddenCloseTimer();
      if (es) {
        namedListeners.forEach((listener, name) => {
          try {
            es?.removeEventListener(name, listener);
          } catch {
            /* ignore */
          }
        });
        es.onopen = null;
        es.onmessage = null;
        es.onerror = null;
        try {
          if (es.readyState !== EventSource.CLOSED) {
            es.close();
          }
        } catch {
          /* ignore */
        }
        es = null;
      }
      // 无条件清空：即便 es 为 null 也要清，防止 open() 重复 addEventListener。
      namedListeners.clear();
    };

    const dispatchNamed = (ev: MessageEvent) => {
      const name = ev.type;
      const localFn = handlersRef.current[name];
      const globalSet = globalSSEHandlers.get(name);
      const hasLocal = typeof localFn === "function";
      const hasGlobal = !!globalSet && globalSet.size > 0;

      // 未知事件类型：上游可能下发 useSSE 调用方未注册的事件
      // （例如未来的 compaction_summary / reasoning_summary）。
      // 决策：每种 type 只 console.warn 一次，去重；不抛错、不断连，保证连接活着。
      if (!hasLocal && !hasGlobal) {
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
        return;
      }

      let parsed: unknown = ev.data;
      if (typeof ev.data === "string") {
        try {
          parsed = JSON.parse(ev.data);
        } catch {
          parsed = ev.data;
        }
      }
      if (hasLocal) {
        try {
          localFn(parsed, ev.lastEventId);
        } catch (err) {
          // handler 内部抛异常不应影响 SSE 连接
          logError(err, { scope: "useSSE", extra: { event: name } });
        }
      }
      if (hasGlobal) {
        // 单个全局 handler 抛错不影响其他 handler，也不断连。
        for (const fn of Array.from(globalSet!)) {
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
    };

    const open = () => {
      if (disposed) return;
      close();
      setStatus("connecting");
      try {
        const channelList = channelKey.split(",").filter(Boolean);
        es = new EventSource(sseUrl(channelList), { withCredentials: true });
      } catch (err) {
        logError(err, { scope: "useSSE", extra: { phase: "open" } });
        setStatus("error");
        scheduleRetry();
        return;
      }

      es.onopen = (ev) => {
        retryAttempt = 0;
        setStatus("open");
        try {
          onOpenRef.current?.(ev);
        } catch (cbErr) {
          logError(cbErr, { scope: "useSSE", extra: { phase: "onOpen-callback" } });
        }
      };

      // 通配：未命名的 message 事件（name === "message"）也尝试 dispatch（如果 handlers 里有 "message"）。
      es.onmessage = (ev: MessageEvent) => {
        dispatchNamed(ev);
      };

      es.onerror = (ev) => {
        setStatus("error");
        try {
          onErrorRef.current?.(ev);
        } catch (cbErr) {
          logError(cbErr, { scope: "useSSE", extra: { phase: "onError-callback" } });
        }
        // 浏览器内置重连在某些情况下会自动恢复；但为确保指数退避受控，这里主动 close + schedule。
        close();
        scheduleRetry();
      };

      // 注册命名事件（对齐 DESIGN §5.7）
      const eventNames = new Set(handlerEventKey.split(",").filter(Boolean));
      // 把全局注册表里的 type 也一并 addEventListener，这样未来上游下发新事件时
      // 调用方只要 registerSSEHandler 就能接收，不需要修改 useSSE 调用点。
      for (const t of globalSSEHandlers.keys()) {
        eventNames.add(t);
      }
      for (const name of eventNames) {
        if (name === "message" || name === "open" || name === "error") continue;
        const listener = (ev: MessageEvent) => dispatchNamed(ev);
        namedListeners.set(name, listener);
        es.addEventListener(name, listener);
      }
    };

    // 优先使用 crypto.getRandomValues 生成 0.8-1.2 的 jitter ratio，
      // 避免 Math.random() 在同步控制流中造成弱随机或被误用于渲染路径。
    const computeJitteredDelay = (baseDelay: number): number => {
      let ratio: number;
      if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
        const arr = new Uint8Array(1);
        crypto.getRandomValues(arr);
        ratio = ((arr[0] % 40) + 80) / 100; // 0.80-1.19
      } else {
        ratio = 0.8 + Math.random() * 0.4;
      }
      return Math.round(baseDelay * ratio);
    };

    const scheduleRetry = () => {
      if (disposed) return;
      clearRetryTimer();
      // 超过最大重试次数后停止，避免离线时无限重试耗尽电池。
      // 默认桌面 20 次、移动/触屏 5 次；调用方可通过 maxRetryCount 覆盖。
      if (retryAttempt >= maxRetryCountRef.current) {
        setStatus("error");
        return;
      }
      // 1s → 2s → 4s → 8s → 16s → 30s 封顶
      const baseDelay = Math.min(30_000, 1000 * 2 ** retryAttempt);
      const delay = Math.min(30_000, computeJitteredDelay(baseDelay));
      retryAttempt += 1;
      retryTimer = setTimeout(() => {
        if (document.visibilityState === "visible") {
          open();
        }
        // 若仍 hidden，visibilitychange 会再次触发 open
      }, delay);
    };

    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
        clearHiddenCloseTimer();
        hiddenCloseTimer = setTimeout(() => {
          if (disposed || document.visibilityState !== "hidden") return;
          close();
          setStatus("closed");
        }, hiddenCloseDelayRef.current);
      } else {
        const wasHiddenForMs = hiddenAt == null ? 0 : Date.now() - hiddenAt;
        hiddenAt = null;
        clearHiddenCloseTimer();
        // BUG-009: 页面恢复可见时重置重试计数，重新连接。
        // 即使之前因超过 maxRetryCount 停止了，visibilitychange 也可恢复。
        if (
          wasHiddenForMs >= hiddenCloseDelayRef.current ||
          retryAttempt >= maxRetryCountRef.current
        ) {
          retryAttempt = 0;
          open();
        } else if (!es || es.readyState === EventSource.CLOSED) {
          retryAttempt = 0;
          open();
        } else if (es.readyState === EventSource.OPEN) {
          setStatus("open");
        } else {
          setStatus("connecting");
        }
      }
    };

    document.addEventListener("visibilitychange", onVisibility);

    if (document.visibilityState === "visible") {
      open();
    } else {
      // 异步置 closed，避免在 effect 同步阶段 setState 触发级联。
      setTimeout(() => {
        if (!disposed) setStatus("closed");
      }, 0);
    }

    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      clearHiddenCloseTimer();
      close();
    };
  }, [channelKey, handlerEventKey]);

  return { status };
}
