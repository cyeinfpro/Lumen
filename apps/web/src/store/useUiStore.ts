import { create } from "zustand";
import type { LightboxItem } from "@/components/ui/lightbox/types";

interface UiLightboxGalleryItem {
  imageId: string;
  imageSrc: string;
  imagePreviewSrc: string | null;
  imageAlt: string | null;
}

interface UiLightboxState {
  open: boolean;
  imageId: string | null;
  imageSrc: string | null;
  imagePreviewSrc: string | null;
  imageAlt: string | null;
  gallery: UiLightboxGalleryItem[];
  /** 多图模式 items（DesktopLightbox 的 eventGallery 数据源）。 */
  eventItems: LightboxItem[] | null;
}

function createClosedLightbox(): UiLightboxState {
  return {
    open: false,
    imageId: null,
    imageSrc: null,
    imagePreviewSrc: null,
    imageAlt: null,
    gallery: [],
    eventItems: null,
  };
}

interface UiState {
  sidebarOpen: boolean;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  studioView: "chat" | "images";
  setStudioView: (view: "chat" | "images") => void;
  /** Sidebar 搜索查询（持久化到 URL 由消费侧负责，store 仅做单一真相源） */
  sidebarSearch: string;
  setSidebarSearch: (q: string) => void;
  lightbox: UiLightboxState;
  openLightbox: (id: string, src: string, alt: string, previewSrc?: string) => void;
  /** 统一入口：从 LightboxItem[] 打开灯箱（含多图翻页）。Canvas / Gallery 等调用此方法。 */
  openLightboxFromItems: (items: LightboxItem[], initialId: string) => void;
  closeLightbox: () => void;
  taskTray: {
    minimized: boolean;
  };
  setTaskTrayMinimized: (minimized: boolean) => void;
  toggleTaskTray: () => void;
}

export const useUiStore = create<UiState>((set) => ({
  sidebarOpen: true,
  toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  studioView: "chat",
  setStudioView: (view) => set({ studioView: view }),
  sidebarSearch: "",
  setSidebarSearch: (q) => set({ sidebarSearch: q }),
  lightbox: createClosedLightbox(),
  openLightbox: (id, src, alt, previewSrc) =>
    set({
      lightbox: {
        open: true,
        imageId: id,
        imageSrc: src,
        imagePreviewSrc: previewSrc ?? null,
        imageAlt: alt,
        gallery: [],
        eventItems: null,
      },
    }),
  openLightboxFromItems: (items, initialId) => {
    if (items.length === 0) return;
    const target = items.find((item) => item.id === initialId) ?? items[0];
    set({
      lightbox: {
        open: true,
        imageId: target.id,
        imageSrc: target.url,
        imagePreviewSrc: target.previewUrl ?? null,
        imageAlt: target.prompt ?? null,
        gallery: [],
        eventItems: items,
      },
    });
  },
  closeLightbox: () =>
    set({ lightbox: createClosedLightbox() }),
  taskTray: {
    minimized: true,
  },
  setTaskTrayMinimized: (minimized) =>
    set({
      taskTray: { minimized },
    }),
  toggleTaskTray: () =>
    set((state) => ({
      taskTray: { minimized: !state.taskTray.minimized },
    })),
}));
