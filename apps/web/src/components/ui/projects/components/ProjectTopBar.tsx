"use client";

// 项目页使用全局主导航，确保"项目"与"创作 / 图库 / 我的"处于同一层级。

import { DesktopTopNav } from "@/components/ui/shell";

interface ProjectTopBarProps {
  right?: React.ReactNode;
}

export function ProjectTopBar({ right }: ProjectTopBarProps) {
  return <DesktopTopNav active="projects" right={right} />;
}
