"use client";

// Lumen 统一 Markdown 渲染器。
// react-markdown + remark-gfm（表格/任务列表/删除线）+ rehype-highlight（代码高亮）。
// 外层加 .lumen-md 类，在 globals.css 中控制段落/列表/代码块节奏。
// highlight.js 的主题样式由 globals.css 里的 @import 统一注入，避免组件重复 side-effect。
//
// 增强：
//  - 代码块右上角显示语言标签 + 复制按钮（复制后 2s 显示 Check 已复制）
//  - 外链自动 target=_blank rel=noopener；仅 URL 作为文本的链接截断显示
//  - 图片点击统一打开全局 Lightbox（移动端保留手势缩放 / 下拉关闭 / safe-area）
//  - components 对象 useMemo 缓存，避免每次渲染重建引用

import { memo, useCallback, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Check, Copy } from "lucide-react";
import { Button } from "./primitives";
import { cn } from "@/lib/utils";
import { copy } from "@/lib/copy";
import { logWarn } from "@/lib/logger";
import { useUiStore } from "@/store/useUiStore";

export interface MarkdownProps {
  children: string;
  className?: string;
  autoDetectCode?: boolean;
}

// 递归把 react children 拍平为字符串。code/pre 的内容多为字符串或 <code>…</code>。
function extractText(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (typeof node === "object" && "props" in node) {
    const props = (node as { props?: { children?: React.ReactNode } }).props;
    return props ? extractText(props.children) : "";
  }
  return "";
}

// 从 <pre><code class="language-xxx"> 提取语言名
function detectLanguage(children: React.ReactNode): string | null {
  if (!children || typeof children !== "object") return null;
  const arr = Array.isArray(children) ? children : [children];
  for (const c of arr) {
    if (typeof c !== "object" || c == null || !("props" in c)) continue;
    const props = (c as { props?: { className?: string } }).props ?? {};
    const cls = props.className ?? "";
    const m = cls.match(/language-([\w+-]+)/);
    if (m) return m[1];
  }
  return null;
}

function CodeBlock({
  children,
  ...rest
}: React.HTMLAttributes<HTMLPreElement>) {
  const [copied, setCopied] = useState(false);
  const lang = useMemo(() => detectLanguage(children), [children]);

  const handleCopy = useCallback(() => {
    if (typeof navigator === "undefined" || !navigator.clipboard) return;
    // 取 <pre> 内的纯文本，避免把高亮 span 一起拷过去
    const text = extractText(children);
    void navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
      })
      .catch((err) => {
        logWarn("markdown.copy_failed", { scope: "markdown", extra: { err: String(err) } });
      });
  }, [children]);

  return (
    <div className="group relative min-w-0 max-w-full">
      <pre
        {...rest}
        className={cn("max-w-full overflow-x-auto", rest.className)}
      >
        {children}
      </pre>
      <div
        className={cn(
          "absolute top-2 right-2 flex items-center gap-1.5 transition-opacity",
          // 触控设备不支持 hover，保持 80% 可见；桌面端保持原来的 hover 显示行为
          "opacity-80 sm:opacity-0 sm:group-hover:opacity-100 focus-within:opacity-100",
        )}
      >
        {lang && (
          <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase tracking-wide text-[var(--fg-1)] bg-black/40 border border-white/5">
            {lang}
          </span>
        )}
        <Button
          variant="glass"
          size="sm"
          onClick={handleCopy}
          aria-label={copied ? copy.state.copied : "复制代码"}
          title={copied ? copy.state.copied : "复制代码"}
          className="text-[11px] gap-1"
          leftIcon={copied ? <Check className="w-3 h-3 text-[var(--ok)]" /> : <Copy className="w-3 h-3" />}
        >
          {copied ? copy.state.copied : copy.action.copy}
        </Button>
      </div>
    </div>
  );
}

function truncateUrl(url: string, max = 48): string {
  if (url.length <= max) return url;
  // 尝试保留 host + "…" + 尾部
  try {
    const u = new URL(url);
    const host = u.host;
    const tail = u.pathname + u.search;
    const keep = Math.max(6, max - host.length - 3);
    return `${host}${tail.length > keep ? tail.slice(0, keep) + "…" : tail}`;
  } catch {
    return url.slice(0, max - 1) + "…";
  }
}

const ALLOWED_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:", "tel:"]);
// blob: removed for SSRF defense, no current callers — Markdown 只渲染 assistant 文本，
// 业务里 createObjectURL 都走 <img>/<a> 直渲，不进 markdown 渲染管线。
const ALLOWED_IMAGE_PROTOCOLS = new Set(["http:", "https:"]);

// P3-2：外链 target/rel 用常量集中维护，避免散落在多处的字符串拼写漂移导致 window.opener 漏洞
const EXTERNAL_LINK_PROPS = {
  target: "_blank",
  rel: "noopener noreferrer",
} as const;

function sanitizeUrl(value: string | undefined, allowedProtocols: Set<string>): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  if (trimmed.startsWith("#")) {
    return trimmed;
  }
  const isRelative =
    trimmed.startsWith("/") ||
    trimmed.startsWith("./") ||
    trimmed.startsWith("../");
  if (isRelative) {
    if (trimmed.startsWith("//") || trimmed.includes("\\")) return undefined;
    const firstSegment =
      trimmed
        .replace(/^(?:\/|\.\.?\/)+/, "")
        .split(/[/?#]/, 1)[0] ?? "";
    return firstSegment.includes(":") ? undefined : trimmed;
  }
  try {
    const parsed = new URL(trimmed, "https://lumen.local");
    return allowedProtocols.has(parsed.protocol) ? trimmed : undefined;
  } catch {
    return undefined;
  }
}

function buildComponents(): Components {
  return {
    a: ({ children, href, ...props }) => {
      const url = sanitizeUrl(typeof href === "string" ? href : undefined, ALLOWED_LINK_PROTOCOLS);
      const isExternal = Boolean(url && /^https?:\/\//i.test(url));
      // 若链接文本即 URL（react-markdown 传 children 为 [string]），截断显示
      let renderedChildren: React.ReactNode = children;
      if (
        url &&
        Array.isArray(children) &&
        children.length === 1 &&
        typeof children[0] === "string" &&
        children[0] === url
      ) {
        renderedChildren = truncateUrl(url);
      }
      return (
        <a
          {...props}
          href={url}
          style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
          {...(isExternal ? EXTERNAL_LINK_PROPS : {})}
        >
          {renderedChildren}
        </a>
      );
    },
    table: ({ children, ...props }) => (
      <div
        role="region"
        aria-label="可横向滚动的表格"
        tabIndex={0}
        className="-mx-1 max-w-full overflow-x-auto px-1"
      >
        <table {...props} className={cn("min-w-max", props.className)}>
          {children}
        </table>
      </div>
    ),
    tr: ({ children, ...props }) => (
      <tr {...props} className="hover:bg-white/[0.03] transition-colors">
        {children}
      </tr>
    ),
    pre: ({ children, ...props }) => (
      <CodeBlock {...props}>{children}</CodeBlock>
    ),
  };
}

function MarkdownImpl({
  children,
  className,
  autoDetectCode = true,
}: MarkdownProps) {
  const cls = className ? `lumen-md ${className}` : "lumen-md";
  const openLightbox = useUiStore((s) => s.openLightboxFromItems);
  const components = useMemo(() => {
    return {
      ...buildComponents(),
      img: ({ src, alt, ...props }) => {
        const url = sanitizeUrl(
          typeof src === "string" ? src : undefined,
          ALLOWED_IMAGE_PROTOCOLS,
        );
        if (!url) return null;
        const handleOpen = (event: React.MouseEvent<HTMLAnchorElement>) => {
          if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
          event.preventDefault();
          openLightbox(
            [
              {
                id: url,
                url,
                previewUrl: url,
                thumbUrl: url,
                prompt: alt || undefined,
              },
            ],
            url,
          );
        };
        return (
          <a
            {...EXTERNAL_LINK_PROPS}
            href={url}
            aria-label={alt || "查看原图"}
            className="inline-block max-w-full cursor-zoom-in"
            onClick={handleOpen}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              {...props}
              src={url}
              alt={alt ?? ""}
              className="h-auto max-w-full transition-opacity hover:opacity-90"
            />
          </a>
        );
      },
    } satisfies Components;
  }, [openLightbox]);
  const rehypePlugins = useMemo(
    () =>
      [
        [
          rehypeHighlight,
          { detect: autoDetectCode, ignoreMissing: true },
        ],
      ] as const,
    [autoDetectCode],
  );
  const remarkPlugins = useMemo(() => [remarkGfm], []);
  return (
    <div className={cls}>
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={
          rehypePlugins as unknown as React.ComponentProps<
            typeof ReactMarkdown
          >["rehypePlugins"]
        }
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);
