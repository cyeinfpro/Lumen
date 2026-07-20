// 独立的 Inpaint（局部修改）打开/关闭状态。
//
// 设计：浏览态（Lightbox / Gallery / 生成卡片 / 对话气泡）的"局部修改"入口都走这里 —
// 调用 openInpaint(source) 打开全局 InpaintModal，编辑器内自带 prompt 输入与提交按钮。
// 不污染 Composer state（Composer 内的旧入口仍然走 useMaskInpaint，保持原行为）。
//
// 提交链路落到 useChatStore.submitInpaintTask，在那里做 mask 上传 + 临时占位 composer + sendMessage。

import { create } from "zustand";

import type { Stroke } from "@/lib/inpaint/types";

export interface InpaintSource {
  /** 后端 image_id（必须，sendMessage 走 attachment_image_ids 传给后端） */
  imageId: string;
  /** 显示用 src（data: 或 http(s) URL 都可，仅 MaskBoard 加载用） */
  src: string;
  /** 描述用 alt（可选，默认"局部修改"） */
  alt?: string;
  /** 像素宽高（可选，让画板能更早算出显示尺寸） */
  width?: number;
  height?: number;
}

// 草稿缓存的 LRU 容量：内存安全兜底，正常一次会话不会到这个量。
const MAX_DRAFTS = 30;

interface InpaintState {
  open: boolean;
  source: InpaintSource | null;
  /** 提交中：禁用关闭与重复提交 */
  submitting: boolean;
  /** prompt 草稿：同一张图重新打开时回填（仅内存，刷新即清） */
  drafts: Record<string, string>;
  /** mask 草稿：strokes 数组；和 prompt drafts 一一对应（同图下次打开还能涂回） */
  maskDrafts: Record<string, Stroke[]>;
  openInpaint: (source: InpaintSource) => void;
  close: () => void;
  setSubmitting: (v: boolean) => void;
  setDraft: (imageId: string, prompt: string) => void;
  clearDraft: (imageId: string) => void;
  setMaskDraft: (imageId: string, strokes: Stroke[]) => void;
  clearMaskDraft: (imageId: string) => void;
}

// 简单 LRU：超过 cap 时移除最早 key
function trimRecord<T>(rec: Record<string, T>, cap: number): Record<string, T> {
  const keys = Object.keys(rec);
  if (keys.length <= cap) return rec;
  const next = { ...rec };
  for (const k of keys.slice(0, keys.length - cap)) {
    delete next[k];
  }
  return next;
}

export const useInpaintStore = create<InpaintState>((set, get) => ({
  open: false,
  source: null,
  submitting: false,
  drafts: {},
  maskDrafts: {},
  openInpaint: (source) => {
    if (get().submitting) return;
    set({ open: true, source });
  },
  close: () => {
    if (get().submitting) return;
    set({ open: false, source: null });
  },
  setSubmitting: (v) => set({ submitting: v }),
  setDraft: (imageId, prompt) =>
    set((s) => {
      if (!prompt) {
        if (!(imageId in s.drafts)) return s;
        const next = { ...s.drafts };
        delete next[imageId];
        return { drafts: next };
      }
      // 重新插入 key 让它"最新"，保持 LRU 顺序
      const next = { ...s.drafts };
      delete next[imageId];
      next[imageId] = prompt;
      return { drafts: trimRecord(next, MAX_DRAFTS) };
    }),
  clearDraft: (imageId) =>
    set((s) => {
      if (!(imageId in s.drafts)) return s;
      const next = { ...s.drafts };
      delete next[imageId];
      return { drafts: next };
    }),
  setMaskDraft: (imageId, strokes) =>
    set((s) => {
      if (!strokes || strokes.length === 0) {
        if (!(imageId in s.maskDrafts)) return s;
        const next = { ...s.maskDrafts };
        delete next[imageId];
        return { maskDrafts: next };
      }
      const next = { ...s.maskDrafts };
      delete next[imageId];
      next[imageId] = strokes;
      return { maskDrafts: trimRecord(next, MAX_DRAFTS) };
    }),
  clearMaskDraft: (imageId) =>
    set((s) => {
      if (!(imageId in s.maskDrafts)) return s;
      const next = { ...s.maskDrafts };
      delete next[imageId];
      return { maskDrafts: next };
    }),
}));
