"use client";

// 只读 Markdown 预览（当前用于 admin 更新面板的 release notes）。
//
// 安全约定：这里**不接受 HTML**，只接受 markdown 源码。
// 早期实现直接 dangerouslySetInnerHTML 渲染后端拼的 body_html，属于 XSS 汇聚点：
// 只要上游（GitHub release 正文）或后端拼装逻辑有一处没转义，就会直接在管理员会话里执行脚本。
// 现改为走仓库统一的 Markdown 渲染器（react-markdown）：
// 未启用 rehype-raw，原始 HTML 一律降级为纯文本，href/src 也由其默认 urlTransform 过滤，
// 因此无需再引入 dompurify 之类的额外净化依赖。

import { Markdown } from "@/components/ui/Markdown";

type Props = {
  /** markdown 源码（不是 HTML）；渲染器会丢弃其中的原始 HTML */
  body: string;
  className?: string;
  limitLines?: number;
};

export function MarkdownPreview({
  body,
  className,
  limitLines,
}: Props) {
  return (
    <div
      className={className}
      style={limitLines ? { maxHeight: `${limitLines * 1.75}em` } : undefined}
    >
      <Markdown autoDetectCode={false}>{body}</Markdown>
    </div>
  );
}
