import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV !== "production";

// Lumen 反代约定：
//   - 外层 nginx 只需要把 https://domain/* → web:3000/*（一条 location 就够）
//   - /api/* 和 /events 由 Next.js 自己 rewrites 到后端（LUMEN_BACKEND_URL）
//   - 这样代码就不依赖"反代层分流路径"，避免跨机部署时漏配路由导致 Mixed Content 等问题
//
// LUMEN_BACKEND_URL 是**服务端**变量（不带 NEXT_PUBLIC_），只在 next 进程内生效，
// 改它不用重新 build 前端 bundle。

function normalizeHttpUrl(value: string, envName: string): string {
  const raw = value.trim();
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`${envName} must be an absolute http(s) URL, got: ${value}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`${envName} must use http: or https:, got: ${url.protocol}`);
  }
  if (url.search || url.hash) {
    throw new Error(`${envName} must not include query or hash fragments`);
  }
  return url.toString().replace(/\/+$/, "");
}

function optionalHttpOrigin(value: string | undefined, envName: string): string | null {
  const raw = value?.trim();
  if (!raw || raw.startsWith("/")) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`${envName} must be an absolute http(s) URL when configured`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`${envName} must use http: or https:, got: ${url.protocol}`);
  }
  return url.origin;
}

function optionalSentryOrigin(value: string | undefined): string | null {
  const raw = value?.trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.origin;
  } catch {
    return null;
  }
}

function unique(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))];
}

const BACKEND_URL = normalizeHttpUrl(
  process.env.LUMEN_BACKEND_URL ?? "http://127.0.0.1:8000",
  "LUMEN_BACKEND_URL",
);
const publicApiOrigin = optionalHttpOrigin(
  process.env.NEXT_PUBLIC_API_BASE,
  "NEXT_PUBLIC_API_BASE",
);
const sentryOrigin = optionalSentryOrigin(
  process.env.NEXT_PUBLIC_SENTRY_DSN ?? process.env.SENTRY_DSN,
);

// connect-src 说明：
//   - 'self' 已覆盖同源的 /api 与 /events（Next.js rewrites 转发到后端，
//     在浏览器视角仍是同源），因此**不要**在这里硬编码后端 IP/域名。
//   - publicApiOrigin 仅在显式配置了跨域 NEXT_PUBLIC_API_BASE 时才有值；
//     默认 "/api" 走相对路径，publicApiOrigin 为 null。
//   - sentryOrigin 仅在配置了 Sentry DSN 时附加。
//   - dev-only 的 localhost/127.0.0.1 是给本地 next dev 与 backend 用的。
const connectSrc = unique([
  "'self'",
  publicApiOrigin,
  sentryOrigin,
  isDev ? "http://localhost:*" : null,
  isDev ? "http://127.0.0.1:*" : null,
  isDev ? "ws://localhost:*" : null,
  isDev ? "ws://127.0.0.1:*" : null,
]).join(" ");
const imgSrc = unique([
  "'self'",
  "data:",
  "blob:",
  publicApiOrigin,
  isDev ? "http://localhost:*" : null,
  isDev ? "http://127.0.0.1:*" : null,
]).join(" ");
// Next.js emits inline bootstrap/RSC scripts in production. Without a per-request
// nonce pipeline those scripts must be allowed or the app will not hydrate.
const scriptSrc = isDev ? "'self' 'unsafe-inline' 'unsafe-eval'" : "'self' 'unsafe-inline'";
const upgradeInsecureRequests =
  !isDev && process.env.LUMEN_UPGRADE_INSECURE_REQUESTS === "true";
const hsts = unique([
  "max-age=31536000",
  process.env.LUMEN_HSTS_INCLUDE_SUBDOMAINS === "true" ? "includeSubDomains" : null,
]).join("; ");

const nextConfig: NextConfig = {
  // Next.js v16 experimental.proxyClientMaxBodySize：
  // rewrites/proxy 读 body 时默认只 buffer 10MB；图片上传最大约 50MB，
  // 这里给 60MB 留 multipart 开销。该实验 API 升级 Next 时需对照 changelog 复核。
  experimental: {
    proxyClientMaxBodySize: "60mb",
    sri: {
      algorithm: "sha256",
    },
  },
  async rewrites() {
    return {
      // beforeFiles：在 next 文件路由匹配之前处理 —— 避免 /api/share/:token 与
      // app/share/[token] 混淆（API 前缀独占，绝不落到 app 路由）
      beforeFiles: [
        { source: "/api/:path*", destination: `${BACKEND_URL}/:path*` },
        // SSE：/events 是长连接流。Next.js 会以流式 pipe 转发（实测 v16 OK）。
        // 如果后端要带 query（channels=...），Next.js 自动透传 query。
        { source: "/events", destination: `${BACKEND_URL}/events` },
      ],
    };
  },
  async headers() {
    const csp = [
      "default-src 'self'",
      "base-uri 'self'",
      "object-src 'none'",
      "frame-ancestors 'none'",
      "form-action 'self'",
      `img-src ${imgSrc}`,
      "font-src 'self' data:",
      // Next.js 开发模式需要 inline/eval 支持 HMR 与错误 overlay；生产环境仍需要
      // inline 支持启动/水合脚本，eval 只在开发环境开放。
      "style-src 'self' 'unsafe-inline'",
      `script-src ${scriptSrc}`,
      `connect-src ${connectSrc}`,
      "worker-src 'self' blob:",
      // Only enable behind a real HTTPS origin. Direct HTTP deployments
      // (for example http://10.x.x.x:3000) would otherwise upgrade Next.js
      // chunk URLs to https://... and leave the app stuck before hydration.
      ...(upgradeInsecureRequests ? ["upgrade-insecure-requests"] : []),
    ].join("; ");
    const headers = [
      { key: "Content-Security-Policy", value: csp },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
    ];
    if (!isDev) {
      // includeSubDomains 影响所有子域，默认关闭；确认全站子域均 HTTPS 后设置
      // LUMEN_HSTS_INCLUDE_SUBDOMAINS=true 再启用。
      headers.push({ key: "Strict-Transport-Security", value: hsts });
    }
    return [
      {
        source: "/:path*",
        headers,
      },
      // Service Worker 必须随时能更新：浏览器只在 /sw.js HTTP 响应"较新"时才
      // 触发 update 检查；任何 CDN/浏览器长缓存都会让 SW 升级被卡住，老用户
      // 一直跑旧 worker。`max-age=0 + must-revalidate` 确保每次 navigation
      // 都回源校验。
      {
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "public, max-age=0, must-revalidate" },
          // Service-Worker-Allowed 不需要（scope 在同目录就生效），但显式声明
          // 避免未来挪到 /static/ 等子路径时忘记加。
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
      // Manifest 修改后（图标 / 名字 / 主题色）应及时重新评估，否则已"安装"
      // 的 PWA 不会刷新元数据。1h 是 installability 检查与 CDN 命中之间的折中。
      {
        source: "/manifest.webmanifest",
        headers: [
          { key: "Cache-Control", value: "public, max-age=3600, must-revalidate" },
        ],
      },
    ];
  },
};

export default nextConfig;
