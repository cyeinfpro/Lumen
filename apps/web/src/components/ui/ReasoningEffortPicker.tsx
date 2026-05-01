"use client";

// 推理强度选择器：chat / vision_qa 有效；传给上游 reasoning.effort。
// 不选 = 上游默认，"none" = 不思考，选"低 / 中 / 高" = 更强推理档位。
//
// popover 用原生 <dialog> 元素：浏览器 top-layer 一等公民，永远在最上面，
// 不受任何祖先 z-index / overflow / transform 影响。showModal() 原生带
// backdrop 和 Esc 关闭，最少代码最稳。

import { useRef, useState } from "react";
import { Brain, ChevronDown } from "lucide-react";
import { useChatStore, type ReasoningEffort } from "@/store/useChatStore";
import { cn } from "@/lib/utils";

type Option = { id: ReasoningEffort | "off"; label: string; hint: string };

const OPTIONS: ReadonlyArray<Option> = [
  { id: "off", label: "默认", hint: "使用模型默认推理强度" },
  { id: "none", label: "极速", hint: "不思考，最快回复" },
  { id: "low", label: "低", hint: "轻度推理，快速回答" },
  { id: "medium", label: "中", hint: "推理质量与速度平衡" },
  { id: "high", label: "高", hint: "深入推理，较慢" },
  { id: "xhigh", label: "极高", hint: "最深入推理，最慢" },
];

function currentLabel(v: ReasoningEffort | undefined): string {
  if (!v) return "默认";
  if (v === "none" || v === "minimal") return "极速";
  if (v === "low") return "低";
  if (v === "medium") return "中";
  if (v === "high") return "高";
  if (v === "xhigh") return "极高";
  return "默认";
}

export function ReasoningEffortPicker() {
  const effort = useChatStore((s) => s.composer.reasoningEffort);
  const setEffort = useChatStore((s) => s.setReasoningEffort);

  const dialogRef = useRef<HTMLDialogElement>(null);
  // open 仅用于驱动按钮箭头朝向；真实 open 态由 dialog.open 决定
  const [isOpen, setIsOpen] = useState(false);

  const active = !!effort;

  const openDialog = () => {
    const d = dialogRef.current;
    if (!d) return;
    if (d.open) {
      d.close();
    } else {
      try {
        d.showModal();
        setIsOpen(true);
      } catch {
        // 旧浏览器不支持 showModal；退化到 open 属性
        d.setAttribute("open", "");
        setIsOpen(true);
      }
    }
  };

  const closeDialog = () => {
    const d = dialogRef.current;
    if (!d) return;
    if (d.open) d.close();
    setIsOpen(false);
  };

  const select = (id: ReasoningEffort | "off") => {
    setEffort(id === "off" ? undefined : id);
    closeDialog();
  };

  return (
    <>
      <button
        type="button"
        onClick={openDialog}
        aria-expanded={isOpen}
        aria-haspopup="menu"
        aria-label="选择推理强度"
        title={`推理强度：${currentLabel(effort)}`}
        className={cn(
          "inline-flex items-center gap-1.5 px-2.5 h-7 rounded-full",
          "text-xs font-medium border transition-all duration-150",
          "cursor-pointer active:scale-[0.96] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
          active
            ? "bg-[var(--color-lumen-amber)]/12 border-[var(--color-lumen-amber)]/40 text-[var(--color-lumen-amber)]"
            : "bg-white/5 border-white/10 text-neutral-300 hover:bg-white/10 hover:text-white",
        )}
      >
        <Brain className="h-3.5 w-3.5" aria-hidden />
        <span>{currentLabel(effort)}</span>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 transition-transform duration-200",
            isOpen && "rotate-180",
          )}
          aria-hidden
        />
      </button>

      <dialog
        ref={dialogRef}
        onClose={() => setIsOpen(false)}
        onClick={(e) => {
          // 点击 backdrop（事件 target 是 dialog 本身时）关闭
          if (e.target === dialogRef.current) closeDialog();
        }}
        className={cn(
          "p-1.5 w-[min(18rem,calc(100vw-1.5rem))] max-h-[min(80vh,420px)] overflow-y-auto",
          "rounded-2xl bg-neutral-900/96 backdrop-blur-xl",
          "border border-white/12 shadow-2xl shadow-black/60",
          "text-neutral-100",
          // dialog 默认有 margin:auto 居中；backdrop 通过 CSS 自定义
          "backdrop:bg-black/40 backdrop:backdrop-blur-[2px]",
        )}
        aria-label="推理强度"
      >
        {OPTIONS.map((opt) => {
          const isActive =
            (opt.id === "off" && !effort) || opt.id === effort;
          return (
            <button
              key={opt.id}
              type="button"
              role="menuitemradio"
              aria-checked={isActive}
              onClick={() => select(opt.id)}
              className={cn(
                "w-full flex items-center justify-between gap-3 px-2.5 py-2 rounded-xl",
                "text-left transition-colors duration-150 cursor-pointer",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
                isActive
                  ? "bg-[var(--color-lumen-amber)]/12 text-[var(--color-lumen-amber)]"
                  : "text-neutral-200 hover:bg-white/8",
              )}
            >
              <div className="flex flex-col">
                <span className="text-xs font-medium">{opt.label}</span>
                <span className="text-[10px] text-neutral-500">
                  {opt.hint}
                </span>
              </div>
              {isActive && (
                <span
                  className="w-1.5 h-1.5 rounded-full bg-[var(--color-lumen-amber)]"
                  aria-hidden
                />
              )}
            </button>
          );
        })}
      </dialog>
    </>
  );
}
