"use client";

// 薄壳：复用 shell/MobileStreamTopBar 已实现的视觉（标题 + compact 切换 + 两个 icon
// toggle）。本文件存在理由是 stream 目录内部 import 路径统一 + 未来扩展（如搜索
// pill 内嵌、未读点）。

import { MobileStreamTopBar } from "@/components/ui/shell/MobileStreamTopBar";

export interface StreamTopBarProps {
  compact: boolean;
  total: number;
  promptCount: number;
  searchActive: boolean;
  filterActive: boolean;
  onToggleSearch: () => void;
  onToggleFilter: () => void;
}

export function StreamTopBar({
  compact,
  total,
  promptCount,
  searchActive,
  filterActive,
  onToggleSearch,
  onToggleFilter,
}: StreamTopBarProps) {
  const countLabel = `共 ${total} 张 · ${promptCount} 段 prompt`;
  return (
    <MobileStreamTopBar
      compact={compact}
      countLabel={countLabel}
      searchActive={searchActive}
      filterActive={filterActive}
      onToggleSearch={onToggleSearch}
      onToggleFilter={onToggleFilter}
    />
  );
}
