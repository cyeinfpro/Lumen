import type { Metadata, Viewport } from "next";
import { cookies, headers } from "next/headers";
import { Instrument_Serif, Geist, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";
import { Lightbox } from "@/components/ui/Lightbox";
import { GlobalTaskTray } from "@/components/ui/GlobalTaskTray";
import { SSEProvider } from "@/components/SSEProvider";
import { QueryProvider } from "@/components/QueryProvider";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { OfflineBanner } from "@/components/OfflineBanner";
import { ToastViewport } from "@/components/ui/primitives";
import { MobileToastViewport } from "@/components/ui/primitives/mobile/Toast";
import { PageTransitions } from "@/components/ui/shell/PageTransitions";
import { CommandPalette } from "@/components/ui/CommandPalette";
import { ServiceWorkerRegister } from "@/components/ServiceWorkerRegister";

const instrumentSerif = Instrument_Serif({
  variable: "--font-display",
  subsets: ["latin"],
  weight: "400",
  display: "swap",
  preload: true,
});

const geist = Geist({
  variable: "--font-body",
  subsets: ["latin"],
  display: "swap",
  preload: true,
});

const ibmPlexMono = IBM_Plex_Mono({
  variable: "--font-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
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

// 无 cookie / 未知值时交给系统主题；显式 light/dark 时固定。
function normalizeTheme(value: string | undefined): ThemePreference {
  return value === "light" || value === "dark" || value === "system"
    ? value
    : "system";
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

// Server Component：读 cookie 决定初始 className。
// - "system" → 不加类，让 CSS prefers-color-scheme 接管
// - "light" → 加 theme-light
// - "dark" / 未知 → 加 theme-dark（默认）
export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const [theme, pathname] = await Promise.all([
    readInitialTheme(),
    readRequestPathname(),
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
        {isPublicShareRoute ? (
          children
        ) : (
          <ErrorBoundary>
            <QueryProvider>
              <SSEProvider>
                <PageTransitions>{children}</PageTransitions>
              </SSEProvider>
              <Lightbox />
              <GlobalTaskTray />
              <OfflineBanner />
              <ToastViewport />
              <MobileToastViewport />
              <CommandPalette />
              <ServiceWorkerRegister />
            </QueryProvider>
          </ErrorBoundary>
        )}
      </body>
    </html>
  );
}
