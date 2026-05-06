// Next.js 16 约定：根 app/not-found.tsx 处理整个应用的 404。
// Server Component（无 "use client"）。用 EmptyState 原语保持视觉一致。

import Link from "next/link";
import { EmptyState, Button } from "@/components/ui/primitives";
import { Home, Compass } from "lucide-react";

export default function NotFound() {
  return (
    <div className="min-h-[100dvh] w-full flex-1 flex items-center justify-center bg-[var(--bg-0)] px-4 sm:px-6 safe-area">
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
            <Link href="/">
              <Button
                variant="primary"
                size="md"
                leftIcon={<Home className="w-3.5 h-3.5" />}
              >
                返回首页
              </Button>
            </Link>
          }
        />
      </div>
    </div>
  );
}
