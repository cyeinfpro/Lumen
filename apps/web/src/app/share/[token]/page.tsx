import type { Metadata } from "next";
import { headers } from "next/headers";
import Link from "next/link";
import { cache } from "react";
import {
  AlertCircle,
  Clock,
  FileX,
  ImageOff,
  Sparkles,
} from "lucide-react";

import { ShareContentClient } from "./ShareContentClient";
import type { PublicShareOut } from "@/lib/types";

export const dynamic = "force-dynamic";

interface SharePageProps {
  params: Promise<{ token: string }>;
}

interface ShareLoadError {
  status: number;
  message: string;
}

type ShareLoadResult =
  | { data: PublicShareOut; error?: never }
  | { data?: never; error: ShareLoadError };

export default async function SharePage({ params }: SharePageProps) {
  const { token } = await params;
  const result = await getPublicShareForSsr(token);

  return (
    <ShareShell>
      {result.data ? (
        <ShareContentClient data={result.data} />
      ) : (
        <ShareError error={result.error} />
      )}
    </ShareShell>
  );
}

export async function generateMetadata({
  params,
}: SharePageProps): Promise<Metadata> {
  const { token } = await params;
  const result = await getPublicShareForSsr(token);
  const base = await requestOrigin();
  const images = result.data ? normalizeMetadataImages(result.data) : [];
  const first = images[0];
  const title = result.data
    ? `图片分享 · ${images.length} 张`
    : "图片分享";
  const description = first
    ? `查看一张 ${first.width} x ${first.height} 的图片。`
    : "查看分享图片。";
  const imageUrl = first ? absoluteUrl(first.url, base) : undefined;

  return {
    title,
    description,
    robots: {
      index: false,
      follow: false,
    },
    openGraph: {
      title,
      description,
      type: "website",
      images: imageUrl
        ? [
            {
              url: imageUrl,
              width: first?.width,
              height: first?.height,
              alt: "分享图片",
            },
          ]
        : undefined,
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: imageUrl ? [imageUrl] : undefined,
    },
  };
}

const getPublicShareForSsr = cache(async function getPublicShareForSsr(
  token: string,
): Promise<ShareLoadResult> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { error: { status: 404, message: "share not found" } };
  }

  const url = `${serverApiBase()}/share/${encodeURIComponent(cleanToken)}`;
  let response: Response;
  try {
    const requestHeaders = await shareApiHeaders();
    response = await fetch(url, {
      method: "GET",
      cache: "no-store",
      headers: requestHeaders,
    });
  } catch (error) {
    return {
      error: {
        status: 0,
        message: error instanceof Error ? error.message : "network error",
      },
    };
  }

  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : await response.text().catch(() => null);

  if (!response.ok) {
    return {
      error: {
        status: response.status,
        message: errorMessageFromPayload(payload, `HTTP ${response.status}`),
      },
    };
  }

  return { data: payload as PublicShareOut };
});

function serverApiBase(): string {
  return (process.env.LUMEN_BACKEND_URL ?? "http://127.0.0.1:8000").replace(
    /\/+$/,
    "",
  );
}

async function shareApiHeaders(): Promise<HeadersInit> {
  const out: Record<string, string> = {
    Accept: "application/json",
  };
  const incoming = await headers();
  for (const name of [
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
  ]) {
    const value = incoming.get(name);
    if (value) out[name] = value;
  }
  return out;
}

async function requestOrigin(): Promise<string> {
  const incoming = await headers();
  const proto = incoming.get("x-forwarded-proto") ?? "https";
  const host = incoming.get("x-forwarded-host") ?? incoming.get("host");
  return host ? `${proto}://${host}` : "";
}

function absoluteUrl(url: string, base: string): string {
  if (!base) return url;
  try {
    return new URL(url, base).toString();
  } catch {
    return url;
  }
}

function normalizeMetadataImages(data: PublicShareOut) {
  const images =
    Array.isArray(data.images) && data.images.length > 0
      ? data.images
      : [
          {
            image_url: data.image_url,
            display_url: null,
            preview_url: null,
            width: data.width,
            height: data.height,
          },
        ];
  return images.map((image) => ({
    url: image.preview_url ?? image.display_url ?? image.image_url,
    width: image.width,
    height: image.height,
  }));
}

function errorMessageFromPayload(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = (payload as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return fallback;
  const error = (detail as { error?: unknown }).error;
  if (!error || typeof error !== "object") return fallback;
  const message = (error as { message?: unknown }).message;
  return typeof message === "string" && message ? message : fallback;
}

function ShareShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-[100dvh] w-full flex-1 flex-col bg-[linear-gradient(180deg,var(--bg-0)_0%,var(--bg-1)_52%,var(--bg-0)_100%)] text-[var(--fg-0)]">
      <header className="sticky top-0 z-10 border-b border-[var(--border)] bg-[var(--bg-0)]/88 pt-[env(safe-area-inset-top)] backdrop-blur-xl">
        <div className="safe-x-page-wide mx-auto flex h-14 max-w-6xl items-center justify-between">
          <Link
            href="/"
            className="inline-flex items-center gap-2 text-sm text-[var(--fg-1)] hover:text-[var(--fg-0)] transition-colors"
          >
            {/* eslint-disable-next-line no-restricted-syntax -- amber→orange-200 品牌徽章渐变 */}
            <span className="w-6 h-6 rounded-full bg-gradient-to-tr from-[var(--color-lumen-amber)] to-orange-200 shadow-[var(--shadow-amber)]" />
            <span className="font-medium tracking-tight">Lumen</span>
            <span className="hidden sm:inline text-xs text-[var(--fg-2)]">
              · 分享
            </span>
          </Link>
          <Link
            href="/"
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-full bg-white/5 hover:bg-white/10 border border-[var(--border)] text-xs text-[var(--fg-1)] hover:text-[var(--fg-0)] transition-colors"
          >
            <Sparkles className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">打开主页</span>
            <span className="sm:hidden">打开</span>
          </Link>
        </div>
      </header>

      <main className="safe-x-page-wide flex flex-1 flex-col items-center justify-start py-6 md:py-10">
        {children}
      </main>

      <footer className="safe-x-page-wide safe-bottom border-t border-[var(--border)] py-8 text-center text-xs text-[var(--fg-2)]">
        <p>
          <Link
            href="/"
            className="inline-flex min-h-11 items-center justify-center px-2 text-[var(--fg-1)] hover:text-[var(--color-lumen-amber)] transition-colors"
          >
            主页
          </Link>
          {" "}分享
        </p>
      </footer>
    </div>
  );
}

function ShareError({ error }: { error: ShareLoadError }) {
  const isNotFound = error.status === 404;
  const isGone = error.status === 410;

  return (
    <div className="w-full max-w-md">
      <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/72 backdrop-blur-sm p-8 text-center space-y-4 shadow-[var(--shadow-2)]">
        <div className="mx-auto w-14 h-14 rounded-[var(--radius-card)] bg-white/5 border border-[var(--border)] flex items-center justify-center">
          {isNotFound ? (
            <FileX className="w-6 h-6 text-[var(--fg-1)]" />
          ) : isGone ? (
            <Clock className="w-6 h-6 text-[var(--fg-1)]" />
          ) : (
            <ImageOff className="w-6 h-6 text-[var(--fg-1)]" />
          )}
        </div>
        <div className="space-y-1.5">
          <p className="text-lg text-[var(--fg-0)] font-medium">
            {isNotFound
              ? "分享不存在"
              : isGone
                ? "此分享链接已过期"
                : "加载失败"}
          </p>
          {isNotFound && (
            <p className="text-xs text-[var(--fg-2)]">
              链接可能被删除，或从未存在。
            </p>
          )}
          {isGone && (
            <p className="text-xs text-[var(--fg-2)]">
              可以联系分享者重新生成一条链接。
            </p>
          )}
          {!isNotFound && !isGone && (
            <p className="flex items-center justify-center gap-1.5 type-caption text-danger">
              <AlertCircle className="w-3.5 h-3.5" />
              {error.message}
            </p>
          )}
        </div>
        <Link
          href="/"
          className="inline-flex items-center justify-center gap-1.5 h-9 px-5 rounded-[var(--radius-card)] bg-[var(--color-lumen-amber)] hover:brightness-110 active:scale-[0.97] text-black text-sm font-medium transition-all"
        >
          <Sparkles className="w-3.5 h-3.5" /> 打开主页
        </Link>
      </div>
    </div>
  );
}
