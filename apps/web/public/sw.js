// Lumen Service Worker —— 最小 passthrough。
//
// 目的：满足 PWA installability（HTTPS + manifest + 含 fetch handler 的 SW），
// 让浏览器允许"添加到主屏 / 安装"。**不**做缓存、不做离线兜底——lumen 是 SSE
// 实时应用，错把 API/事件流缓存住会出诡异问题。
//
// 升级机制：
//   1. next.config.ts 给 /sw.js 设了 no-cache，浏览器每次 navigation 校验
//   2. 改 SW_VERSION 字符串 = 强制浏览器视为"新 SW"，触发 install/activate
//   3. install 里 skipWaiting + activate 里 clients.claim → 配合注册侧的
//      controllerchange 监听，新版本接管时刷新页面
//
// 未来如果加缓存：
//   - 用 SW_VERSION 作为 cache name 前缀（lumen-${SW_VERSION}-...）
//   - activate 时遍历 caches.keys() 删掉非当前版本的，避免老版本 cache 残留
//   - **永远不要**缓存 /api/* /events /sw.js manifest——见 fetch handler 注释

const SW_VERSION = "2026-05-01-1";

self.addEventListener("install", () => {
  // 立即进入 waiting 状态后直接激活，避免旧 SW 与新页面 mismatch。
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // 清理任何老版本 cache（当前没用 cache，但骨架先留好；以后加 cache 时
      // 直接按 SW_VERSION 前缀过滤即可，不会出"老用户残留旧 cache"问题）。
      try {
        const keys = await self.caches.keys();
        await Promise.all(
          keys
            .filter((k) => k.startsWith("lumen-") && !k.startsWith(`lumen-${SW_VERSION}`))
            .map((k) => self.caches.delete(k)),
        );
      } catch {
        // caches API 在某些隐私模式 / 旧浏览器下可能不可用，忽略。
      }
      await self.clients.claim();
    })(),
  );
});

// passthrough fetch handler：必须存在，浏览器才认作 PWA。
// 不调 event.respondWith → 浏览器走默认网络。
//
// **防御性约束**：未来任何人想给这里加缓存，必须先 bypass：
//   - /api/*       业务 API，强一致
//   - /events      SSE 长连接
//   - /sw.js       SW 自身，浏览器升级路径
//   - /manifest.webmanifest
// 否则会破坏 lumen 的实时数据流。
self.addEventListener("fetch", () => {});

// 允许注册侧主动触发立即激活（postMessage({type: "SKIP_WAITING"})）。
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});
