import { create } from "zustand";
import {
  OPEN_EVENT,
  type LightboxItem,
  type OpenLightboxDetail,
} from "@/components/ui/lightbox/types";
import {
  DEFAULT_NAV_VISIBILITY,
  normalizeNavVisibility,
  type NavVisibility,
} from "@/components/ui/shell/navigation";

interface UiLightboxGalleryItem {
  imageId: string;
  imageSrc: string;
  imagePreviewSrc: string | null;
  imageAlt: string | null;
}

/**
 * Lightbox 内可挂载的额外动作（如"设为当前模特"）。
 * - label：按钮文本
 * - onClick：点击回调，参数是 lightbox 当前展示的 item
 * - pending：按钮 loading 态（mutation 期间）
 */
export interface LightboxAction {
  label: string;
  onClick: (item: LightboxItem) => void;
  pending?: boolean;
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
  /** 可选 action（dialog 模式下展示"设为当前模特"等）。 */
  action: LightboxAction | null;
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
    action: null,
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
  navVisibility: Required<NavVisibility>;
  setNavVisibility: (visibility: NavVisibility | undefined | null) => void;
  canvasEnabled: boolean;
  setCanvasEnabled: (enabled: boolean) => void;
  lightbox: UiLightboxState;
  openLightbox: (id: string, src: string, alt: string, previewSrc?: string) => void;
  /**
   * 统一入口：从 LightboxItem[] 打开灯箱（含多图翻页）。Canvas / Gallery 等调用此方法。
   * 第三参 action 为可选「附加按钮」（如「设为当前模特」），关闭时自动重置。
   *
   * 双轨设计：DesktopLightbox 订阅 store + OPEN_EVENT；MobileLightbox 仅 OPEN_EVENT。
   * store 是唯一真相源，event 仅用于跨组件同步。
   */
  openLightboxFromItems: (
    items: LightboxItem[],
    initialId: string,
    action?: LightboxAction | null,
  ) => void;
  /** 在 lightbox 打开期间临时切换 action 的 pending 状态。 */
  setLightboxActionPending: (pending: boolean) => void;
  closeLightbox: () => void;
  taskTray: {
    minimized: boolean;
  };
  taskIslandMounted: boolean;
  setTaskTrayMinimized: (minimized: boolean) => void;
  toggleTaskTray: () => void;
  setTaskIslandMounted: (mounted: boolean) => void;
}

export const useUiStore = create<UiState>((set) => ({
  sidebarOpen: true,
  toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  studioView: "chat",
  setStudioView: (view) => set({ studioView: view }),
  sidebarSearch: "",
  setSidebarSearch: (q) => set({ sidebarSearch: q }),
  navVisibility: DEFAULT_NAV_VISIBILITY,
  setNavVisibility: (visibility) =>
    set({ navVisibility: normalizeNavVisibility(visibility) }),
  canvasEnabled: false,
  setCanvasEnabled: (enabled) => set({ canvasEnabled: enabled }),
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
        action: null,
      },
    }),
  openLightboxFromItems: (items, initialId, action) => {
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
        action: action ?? null,
      },
    });
    // 派发 OPEN_EVENT 让 MobileLightbox（订阅事件，不订阅 store）也能响应
    // 同一个统一入口；source=store 让 DesktopLightbox 跳过镜像写库，保留 action。
    if (typeof window !== "undefined") {
      const detail: OpenLightboxDetail = {
        items,
        initialId: target.id,
        source: "store",
      };
      window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail }));
    }
  },
  setLightboxActionPending: (pending) =>
    set((state) => {
      if (!state.lightbox.action) return state;
      return {
        lightbox: {
          ...state.lightbox,
          action: { ...state.lightbox.action, pending },
        },
      };
    }),
  closeLightbox: () => set({ lightbox: createClosedLightbox() }),
  taskTray: {
    minimized: true,
  },
  taskIslandMounted: false,
  setTaskTrayMinimized: (minimized) =>
    set({
      taskTray: { minimized },
    }),
  toggleTaskTray: () =>
    set((state) => ({
      taskTray: { minimized: !state.taskTray.minimized },
    })),
  setTaskIslandMounted: (mounted) => set({ taskIslandMounted: mounted }),
}));
