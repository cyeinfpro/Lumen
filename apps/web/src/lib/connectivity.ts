// 简单连通性事件总线：浏览器 online/offline → 订阅者 callback
// 设计动机：
// - layout 顶部 banner（OfflineBanner）只展示状态，不知道哪些 in-flight 操作要重发
// - store 端可以订阅 onOnlineRestore，触发 pollInflightTasks 等自愈行为
// - 不依赖 zustand / react，server / 工具函数也可调用
//
// 这里没有重写 navigator.onLine 状态机；只是一个 onOnlineRestore("...") 的事件订阅薄壳。

export type ConnectivityEvent = "online" | "offline";

type Listener = () => void;

interface ConnectivityState {
  onlineRestoreListeners: Set<Listener>;
  offlineListeners: Set<Listener>;
  initialized: boolean;
  startRefs: number;
  lastOnline: boolean;
  onlineHandler: (() => void) | null;
  offlineHandler: (() => void) | null;
}

// versioned key 让多版本/iframe 中加载的同一库可以独立持有自己的 state，
// 避免不同实例共享 listener 导致事件互相污染。
const STATE_KEY = "__lumenConnectivityState_v1__";

const globalConnectivity = globalThis as typeof globalThis & {
  [STATE_KEY]?: ConnectivityState;
};

const state =
  globalConnectivity[STATE_KEY] ??
  (globalConnectivity[STATE_KEY] = {
    onlineRestoreListeners: new Set<Listener>(),
    offlineListeners: new Set<Listener>(),
    initialized: false,
    startRefs: 0,
    lastOnline: true,
    onlineHandler: null,
    offlineHandler: null,
  });
state.startRefs ??= 0;

function detachWindowListeners(): void {
  if (typeof window !== "undefined") {
    if (state.onlineHandler) {
      window.removeEventListener("online", state.onlineHandler);
    }
    if (state.offlineHandler) {
      window.removeEventListener("offline", state.offlineHandler);
    }
  }
  state.onlineHandler = null;
  state.offlineHandler = null;
  state.initialized = false;
}

function ensureInitialized(): void {
  if (state.initialized) return;
  if (typeof window === "undefined") return;
  detachWindowListeners();
  state.initialized = true;
  state.lastOnline = navigator.onLine;
  state.onlineHandler = () => {
    if (!state.lastOnline) {
      state.lastOnline = true;
      for (const l of state.onlineRestoreListeners) {
        try {
          l();
        } catch {
          /* swallow */
        }
      }
    }
  };
  state.offlineHandler = () => {
    state.lastOnline = false;
    for (const l of state.offlineListeners) {
      try {
        l();
      } catch {
        /* swallow */
      }
    }
  };
  window.addEventListener("online", state.onlineHandler);
  window.addEventListener("offline", state.offlineHandler);
}

const hot = (import.meta as ImportMeta & { hot?: { dispose: (cb: () => void) => void } }).hot;
if (hot) {
  hot.dispose(() => {
    state.startRefs = 0;
    detachWindowListeners();
  });
}

/** 启动浏览器 online/offline 监听。通常由根组件 effect 管理生命周期。 */
export function startConnectivity(): () => void {
  state.startRefs += 1;
  ensureInitialized();
  let active = true;
  return () => {
    if (!active) return;
    active = false;
    state.startRefs = Math.max(0, state.startRefs - 1);
    if (state.startRefs === 0) {
      detachWindowListeners();
    }
  };
}

/** 订阅"从离线恢复在线"事件。返回 unsubscribe。 */
export function onOnlineRestore(listener: Listener): () => void {
  state.onlineRestoreListeners.add(listener);
  return () => {
    state.onlineRestoreListeners.delete(listener);
  };
}

/** 订阅"开始离线"事件。 */
export function onOffline(listener: Listener): () => void {
  state.offlineListeners.add(listener);
  return () => {
    state.offlineListeners.delete(listener);
  };
}

/** 当前是否在线（SSR 默认 true）。 */
export function isOnline(): boolean {
  if (typeof navigator === "undefined") return true;
  return navigator.onLine;
}
