import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { cookies, headers } from "next/headers";
import localFont from "next/font/local";

import "./globals.css";
import { LumenAppShell } from "@/components/LumenAppShell";
import { GlobalGsapMotion } from "@/components/ui/motion/GlobalGsapMotion";
import type { RuntimeDefaults } from "@/components/RuntimeDefaultsBootstrap";

// Self-hosted to keep `next build` offline. Google Fonts fetch fails behind
// region-blocked / proxied production networks; switching to next/font/local
// removes the network dependency entirely.
const instrumentSerif = localFont({
  src: "./fonts/InstrumentSerif-Regular.woff2",
  variable: "--font-serif",
  weight: "400",
  display: "swap",
  preload: true,
});

const geist = localFont({
  src: "./fonts/Geist-Variable.woff2",
  variable: "--font-body",
  weight: "100 900",
  display: "swap",
  preload: true,
});

const ibmPlexMono = localFont({
  src: [
    { path: "./fonts/IBMPlexMono-Regular.woff2", weight: "400", style: "normal" },
    { path: "./fonts/IBMPlexMono-Medium.woff2", weight: "500", style: "normal" },
  ],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Lumen Studio",
  description: "Lumen image and chat workspace.",
  applicationName: "Lumen",
  // appleWebApp 不会自动 imply manifest，要单独声明。manifest 走 app/manifest.ts。
  appleWebApp: {
    capable: true,
    title: "Lumen",
    statusBarStyle: "black-translucent",
  },
  formatDetection: { telephone: false },
  other: {
    // 默认 dark，保留 light 作为系统切换回退（配合 prefers-color-scheme / cookie）。
    "color-scheme": "dark light",
    // iOS PWA 全屏（statusBarStyle 已写过；mobile-web-app-capable 是新标准别名）。
    "mobile-web-app-capable": "yes",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  userScalable: true,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#F8F4EA" },
    { media: "(prefers-color-scheme: dark)", color: "#08080A" },
  ],
};

type ThemePreference = "light" | "dark" | "system";

const RUNTIME_DEFAULTS_COOKIE = "lumen_runtime_defaults_v1";
const DEFAULT_RUNTIME_DEFAULTS: RuntimeDefaults = { fast: true };

// 无 cookie / 未知值时交给系统主题；显式 light/dark 时固定。
function normalizeTheme(value: string | undefined): ThemePreference {
  return value === "light" || value === "dark" || value === "system"
    ? value
    : "system";
}

function normalizeRuntimeDefaults(value: unknown): RuntimeDefaults {
  if (!value || typeof value !== "object") return DEFAULT_RUNTIME_DEFAULTS;
  const raw = value as RuntimeDefaults;
  const next: RuntimeDefaults = {
    fast: typeof raw.fast === "boolean" ? raw.fast : true,
    canvas_enabled: raw.canvas_enabled === true,
  };
  if (
    typeof raw.upload_max_source_bytes === "number" &&
    Number.isFinite(raw.upload_max_source_bytes) &&
    raw.upload_max_source_bytes > 0
  ) {
    next.upload_max_source_bytes = raw.upload_max_source_bytes;
  }
  if (raw.nav_visibility && typeof raw.nav_visibility === "object") {
    const nav = raw.nav_visibility;
    next.nav_visibility = {
      studio: nav.studio !== false,
      video: nav.video !== false,
      projects: nav.projects !== false,
      assets: nav.assets !== false,
    };
  }
  return next;
}

async function readInitialTheme(): Promise<ThemePreference> {
  try {
    const cookieStore = await cookies();
    return normalizeTheme(cookieStore.get("theme")?.value);
  } catch {
    return "system";
  }
}

async function readRequestPathname(): Promise<string> {
  try {
    const headerStore = await headers();
    return headerStore.get("x-lumen-pathname") ?? "";
  } catch {
    return "";
  }
}

async function readRuntimeDefaultsCookie(): Promise<RuntimeDefaults> {
  try {
    const cookieStore = await cookies();
    const raw = cookieStore.get(RUNTIME_DEFAULTS_COOKIE)?.value;
    if (!raw || raw.length > 512) return DEFAULT_RUNTIME_DEFAULTS;
    return normalizeRuntimeDefaults(JSON.parse(decodeURIComponent(raw)));
  } catch {
    return DEFAULT_RUNTIME_DEFAULTS;
  }
}

// Server Component：读 cookie 决定初始 className。
// - "system" → 不加类，让 CSS prefers-color-scheme 接管
// - "light" → 加 theme-light
// - "dark" / 未知 → 加 theme-dark（默认）
export default async function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const [theme, pathname, runtimeDefaults] = await Promise.all([
    readInitialTheme(),
    readRequestPathname(),
    readRuntimeDefaultsCookie(),
  ]);

  const themeClass = theme === "system" ? undefined : `theme-${theme}`;
  const isPublicShareRoute = pathname.startsWith("/share/");

  return (
    <html
      lang="zh-CN"
      dir="ltr"
      className={themeClass}
      data-theme={theme}
      suppressHydrationWarning
    >
      <body
        className={`${instrumentSerif.variable} ${geist.variable} ${ibmPlexMono.variable} antialiased bg-[var(--bg-0)] text-[var(--fg-0)] min-h-[100dvh] flex flex-col overflow-x-hidden`}
      >
        <GlobalGsapMotion>
          {isPublicShareRoute ? (
            children
          ) : (
            <LumenAppShell initialRuntimeDefaults={runtimeDefaults}>
              {children}
            </LumenAppShell>
          )}
        </GlobalGsapMotion>
      </body>
    </html>
  );
}
