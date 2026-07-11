// 公开页（无需登录即可访问）的 pathname 判断。
// - RuntimeDefaultsBootstrap：用于禁用 me 拉取，避免登录页 / 注册页 401
// - http.ts handle401：用于跳过 /login 重定向，避免死循环刷新
//
// 入参语义：传入 pathname（不带 query）；isPublicPath 内部自行剥离潜在的 ?query
// 后再做精确 / 前缀匹配，让两边调用点都好用。

export function isPublicPath(pathname: string): boolean {
  // 兼容传入 pathname + search 的旧调用点（如 window.location.pathname + search）。
  const queryIdx = pathname.indexOf("?");
  const path = queryIdx >= 0 ? pathname.slice(0, queryIdx) : pathname;
  if (path === "/login") return true;
  if (path === "/signup") return true;
  if (path.startsWith("/reset-password")) return true;
  if (path.startsWith("/invite/")) return true;
  return false;
}
