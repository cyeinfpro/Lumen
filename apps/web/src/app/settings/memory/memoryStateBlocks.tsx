"use client";

import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { errorMessage } from "@/lib/errorUtils";

// 记忆设置页的三个纯展示状态块。抽出来是因为 page.tsx 已经贴着 1500 行的文件
// 预算，这三个组件不含任何页面逻辑，放在这里既能腾出预算也便于复用。

export function LoadingBlock() {
  return (
    <div className="flex items-center justify-center gap-2 p-8 type-body-sm text-[var(--fg-2)]">
      <Loader2 className="h-4 w-4 animate-spin" />
      {copy.state.loading}
    </div>
  );
}

export function EmptyBlock({ text }: { text: string }) {
  return <div className="p-8 text-center type-body-sm text-[var(--fg-2)]">{text}</div>;
}

// 加载失败态：必须与 EmptyBlock 明确区分, 否则请求挂了会被读成"确实没有数据",
// 用户既不知道该重试、也可能误以为记忆被清空了.
export function ErrorBlock({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 p-8 text-center"
    >
      <AlertTriangle className="h-5 w-5 text-danger" />
      <div className="min-w-0">
        <p className="type-body-sm text-[var(--fg-0)]">加载失败</p>
        <p className="mt-1 break-words type-caption text-[var(--fg-2)]">
          {errorMessage(error) ?? copy.error.unknown}
        </p>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={onRetry}
        leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
      >
        {copy.action.retry}
      </Button>
    </div>
  );
}
