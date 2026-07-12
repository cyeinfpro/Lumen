// Next.js 16 约定：根 app/not-found.tsx 处理整个应用的 404。
// Server Component（无 "use client"）。用 EmptyState 原语保持视觉一致。

import Link from "next/link";
import { EmptyState } from "@/components/ui/primitives";
import { Home, Compass } from "lucide-react";

export default function NotFound() {
  return (
    <div className="safe-area flex min-h-[100dvh] w-full flex-1 items-center justify-center bg-[var(--bg-0)] px-4 py-6 sm:px-6">
      <div className="w-full max-w-md">
        <p
          aria-hidden="true"
          className="mb-2 text-center font-mono text-[40px] leading-none tracking-normal text-[var(--accent)]/70 select-none md:text-[48px]"
        >
          404
        </p>
        <EmptyState
          icon={<Compass className="w-5 h-5" aria-hidden="true" />}
          title="找不到这个页面"
          description="它可能被移走了，也可能你输错了地址。"
          action={
            <Link
              href="/"
              className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-medium text-[var(--accent-on)] shadow-[var(--shadow-amber)] transition-[filter,opacity] hover:brightness-110 active:opacity-[var(--op-press)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
            >
              <Home className="h-3.5 w-3.5" aria-hidden />
              返回首页
            </Link>
          }
        />
      </div>
    </div>
  );
}
