"use client";

// 项目专属顶栏。可选 leadingSlot 用于详情页注入面包屑；
// 默认显示"创作 ← / 项目 / 图库 / 我的"。

import { ArrowLeft } from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";

interface ProjectTopBarProps {
  active?: "projects" | "stream" | "me";
  leadingSlot?: React.ReactNode;
}

export function ProjectTopBar({ active = "projects", leadingSlot }: ProjectTopBarProps) {
  return (
    <header className="sticky top-0 z-[var(--z-header)] flex h-11 items-center justify-between border-b border-white/[0.05] bg-[var(--bg-0)]/80 px-3 backdrop-blur-xl md:px-5">
      <div className="flex min-w-0 items-center gap-2">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
        >
          <ArrowLeft className="h-4 w-4" />
          创作
        </Link>
        {leadingSlot ? (
          <>
            <span aria-hidden className="text-[var(--fg-3)]">/</span>
            <div className="min-w-0 flex-1 truncate">{leadingSlot}</div>
          </>
        ) : null}
      </div>
      <nav className="flex items-center gap-3 text-[13px] text-[var(--fg-2)]">
        <Link
          href="/projects"
          className={cn(
            "transition-colors",
            active === "projects" ? "text-[var(--fg-0)]" : "hover:text-[var(--fg-0)]",
          )}
        >
          项目
        </Link>
        <Link
          href="/stream"
          className={cn(
            "transition-colors",
            active === "stream" ? "text-[var(--fg-0)]" : "hover:text-[var(--fg-0)]",
          )}
        >
          图库
        </Link>
        <Link
          href="/me"
          className={cn(
            "transition-colors",
            active === "me" ? "text-[var(--fg-0)]" : "hover:text-[var(--fg-0)]",
          )}
        >
          我的
        </Link>
      </nav>
    </header>
  );
}
