import { NextResponse, type NextRequest } from "next/server";

type HideableNavKey = "studio" | "video" | "projects" | "assets";
type NavVisibility = Partial<Record<HideableNavKey, boolean>>;

const RUNTIME_DEFAULTS_COOKIE = "lumen_runtime_defaults_v1";

const NAV_ROUTE_PREFIXES: readonly {
  key: HideableNavKey;
  route: string;
  prefixes: readonly string[];
}[] = [
  { key: "studio", route: "/", prefixes: ["/"] },
  { key: "video", route: "/video", prefixes: ["/video"] },
  { key: "projects", route: "/projects", prefixes: ["/projects"] },
  { key: "assets", route: "/assets", prefixes: ["/assets", "/stream", "/library"] },
];

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

function matchesNavPrefix(pathname: string, prefix: string): boolean {
  if (prefix === "/") return pathname === "/" || pathname === "";
  return matchesPrefix(pathname, prefix);
}

function readNavVisibilityCookie(request: NextRequest): NavVisibility | null {
  const raw = request.cookies.get(RUNTIME_DEFAULTS_COOKIE)?.value;
  if (!raw || raw.length > 512) return null;
  try {
    const parsed = JSON.parse(decodeURIComponent(raw)) as {
      nav_visibility?: NavVisibility;
    };
    return parsed.nav_visibility && typeof parsed.nav_visibility === "object"
      ? parsed.nav_visibility
      : null;
  } catch {
    return null;
  }
}

function isVisible(visibility: NavVisibility | null, key: HideableNavKey): boolean {
  return visibility?.[key] !== false;
}

function redirectForHiddenNavPath(
  pathname: string,
  visibility: NavVisibility | null,
): string | null {
  if (!visibility) return null;
  const matched = NAV_ROUTE_PREFIXES.find((item) =>
    item.prefixes.some((prefix) => matchesNavPrefix(pathname, prefix)),
  );
  if (!matched || isVisible(visibility, matched.key)) return null;
  return (
    NAV_ROUTE_PREFIXES.find((item) => isVisible(visibility, item.key))?.route ?? "/me"
  );
}

export function proxy(request: NextRequest) {
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-lumen-pathname", request.nextUrl.pathname);
  const targetPath = backendPath(request.nextUrl.pathname);
  if (!targetPath) {
    const redirectTo = redirectForHiddenNavPath(
      request.nextUrl.pathname,
      readNavVisibilityCookie(request),
    );
    if (redirectTo) {
      return NextResponse.redirect(new URL(redirectTo, request.url));
    }
  }
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
