"use client";

// 桌面端创作画布：单一内容轴 + Scene 分隔 + DevelopingCard 显影扫光。
// 按 messages 顺序两两配对（user → assistant），渲染 Scene NN 分隔条。
// 跟移动端 MobileConversationCanvas 设计哲学一致，差异：
//   - 桌面端提示词、文本和单图统一到 760px 内容轴
//   - 单图按视口高度限制，优先完整显示
//   - 右键 / hover"···" 触发上下文菜单（移动端长按）
//   - 保留虚拟化（messages > 80）

import {
  type ReactNode,
  useEffect,
  useRef,
} from "react";
import { createPortal } from "react-dom";
import {
  Clipboard,
  Download,
  ExternalLink,
  ImagePlus,
  RefreshCw,
} from "lucide-react";
import { toast } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { tryCopyTextToClipboard } from "@/lib/clipboard";
import { imageBinaryUrl } from "@/lib/apiClient";
import { triggerImageDownload } from "@/components/ui/lightbox/utils";

interface ImageMenuInfo {
  imageId: string;
  prompt: string;
  genId: string;
  x: number;
  y: number;
}

interface ImageContextMenuProps {
  info: ImageMenuInfo | null;
  onClose: () => void;
  onEditImage: (imageId: string) => void;
  onRetryGen: (genId: string) => void;
  onLocate: (imageId: string) => void;
}

export function ImageContextMenu({
  info,
  onClose,
  onEditImage,
  onRetryGen,
  onLocate,
}: ImageContextMenuProps) {
  if (!info) return null;
  return (
    <ImageContextMenuInner
      info={info}
      onClose={onClose}
      onEditImage={onEditImage}
      onRetryGen={onRetryGen}
      onLocate={onLocate}
    />
  );
}

interface ImageContextMenuInnerProps {
  info: ImageMenuInfo;
  onClose: () => void;
  onEditImage: (imageId: string) => void;
  onRetryGen: (genId: string) => void;
  onLocate: (imageId: string) => void;
}

export function ImageContextMenuInner({
  info,
  onClose,
  onEditImage,
  onRetryGen,
  onLocate,
}: ImageContextMenuInnerProps) {
  const menuRef = useRef<HTMLDivElement | null>(null);

  // 首帧 React 按"用户光标点"渲染；layout 完成后由 effect 直接改写 DOM style，
  // 把菜单夹到视口内。避免在 effect 中 setState 触发级联渲染（React 19 hooks 规则）。
  useEffect(() => {
    const el = menuRef.current;
    if (!el) return;
    const { offsetWidth, offsetHeight } = el;
    const vw = typeof window !== "undefined" ? window.innerWidth : 1024;
    const vh = typeof window !== "undefined" ? window.innerHeight : 768;
    const padding = 8;
    const left = Math.min(
      Math.max(padding, info.x),
      Math.max(padding, vw - offsetWidth - padding),
    );
    const top = Math.min(
      Math.max(padding, info.y),
      Math.max(padding, vh - offsetHeight - padding),
    );
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  }, [info.x, info.y]);

  // 点外 / ESC 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onDown = (e: MouseEvent) => {
      const el = menuRef.current;
      if (el && e.target instanceof Node && !el.contains(e.target)) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    // capture：防止页内其它 mousedown 把菜单自己吃掉
    window.addEventListener("mousedown", onDown, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown, true);
    };
  }, [onClose]);

  if (typeof document === "undefined") return null;

  const actions: Array<{ key: string; label: string; icon: ReactNode; onSelect: () => void }> = [
    {
      key: "ref",
      label: "做参考图",
      icon: <ImagePlus className="w-4 h-4" />,
      onSelect: () => onEditImage(info.imageId),
    },
    {
      key: "save",
      label: "下载原图",
      icon: <Download className="w-4 h-4" />,
      onSelect: () => {
        const url = imageBinaryUrl(info.imageId);
        const filename = `lumen-${info.imageId}.png`;
        void triggerImageDownload(url, filename).catch(() => {
          toast.error("下载失败,已在新标签页打开");
          window.open(url, "_blank", "noopener,noreferrer");
        });
      },
    },
    {
      key: "copy",
      label: "复制 prompt",
      icon: <Clipboard className="w-4 h-4" />,
      onSelect: () => {
        void tryCopyTextToClipboard(info.prompt).then((success) => {
          if (success) toast.success("已复制 prompt");
          else toast.error("复制失败");
        });
      },
    },
    {
      key: "regen",
      label: "再生一张",
      icon: <RefreshCw className="w-4 h-4" />,
      onSelect: () => onRetryGen(info.genId),
    },
    {
      key: "locate",
      label: "在图库定位",
      icon: <ExternalLink className="w-4 h-4" />,
      onSelect: () => onLocate(info.imageId),
    },
  ];

  const style: React.CSSProperties = {
    top: info.y,
    left: info.x,
  };

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      aria-label="图片操作"
      className={cn(
        "fixed z-[1000] min-w-[172px] py-1",
        "rounded-[var(--radius-panel)] border border-[var(--border)]",
        "bg-[var(--bg-1)]/90 backdrop-blur-xl shadow-[var(--shadow-3)]",
      )}
      style={style}
    >
      {actions.map((a) => (
        <button
          key={a.key}
          type="button"
          role="menuitem"
          onClick={() => {
            a.onSelect();
            onClose();
          }}
          className={cn(
            "flex h-8 min-h-11 w-full items-center gap-2 px-3 text-left",
            "text-[13px] text-[var(--fg-0)]",
            "hover:bg-[var(--bg-2)] transition-colors duration-100",
            "focus-visible:outline-none focus-visible:bg-[var(--bg-2)]",
          )}
        >
          <span className="text-[var(--fg-2)] shrink-0">{a.icon}</span>
          {a.label}
        </button>
      ))}
    </div>,
    document.body,
  );
}
