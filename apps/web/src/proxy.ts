import { NextResponse, type NextRequest } from "next/server";

const DESKTOP_UNSUPPORTED_PREFIXES = [
  "/admin",
  "/api/admin",
  "/api/billing",
  "/invite",
  "/library",
  "/login",
  "/me/wallet",
  "/poster-styles",
  "/projects",
  "/reset-password",
  "/settings/api-key",
  "/settings/privacy",
  "/settings/telegram",
  "/settings/usage",
  "/signup",
] as const;

function normalizeBackendUrl(): string {
  const raw = process.env.LUMEN_BACKEND_URL?.trim() || "http://127.0.0.1:8000";
  const url = new URL(raw);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`LUMEN_BACKEND_URL must use http: or https:, got: ${url.protocol}`);
  }
  url.pathname = url.pathname.replace(/\/+$/, "");
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/+$/, "");
}

function backendPath(pathname: string): string | null {
  if (pathname === "/events") return "/events";
  if (pathname === "/api") return "/";
  if (pathname.startsWith("/api/")) return pathname.slice(4);
  return null;
}

function matchesPrefix(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

export function proxy(request: NextRequest) {
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-lumen-pathname", request.nextUrl.pathname);
  if (process.env.NEXT_PUBLIC_LUMEN_RUNTIME === "desktop") {
    if (
      DESKTOP_UNSUPPORTED_PREFIXES.some((prefix) =>
        matchesPrefix(request.nextUrl.pathname, prefix),
      )
    ) {
      return NextResponse.redirect(new URL("/", request.url));
    }
    const token = process.env.LUMEN_LOCAL_TOKEN?.trim();
    if (token) requestHeaders.set("x-lumen-local-token", token);
  }
  const targetPath = backendPath(request.nextUrl.pathname);
  if (targetPath) {
    const target = new URL(`${normalizeBackendUrl()}${targetPath}`);
    target.search = request.nextUrl.search;
    return NextResponse.rewrite(target, {
      request: {
        headers: requestHeaders,
      },
    });
  }

  return NextResponse.next({
    request: {
      headers: requestHeaders,
    },
  });
}

export const config = {
  matcher: [
    "/api/:path*",
    "/events",
    "/((?!_next/static|_next/image|favicon.ico|robots.txt).*)",
  ],
};
