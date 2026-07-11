export type AppNavKey = "studio" | "video" | "projects" | "assets" | "me";
export type HideableAppNavKey = Exclude<AppNavKey, "me">;
export type NavVisibility = Partial<Record<HideableAppNavKey, boolean>>;

export interface AppNavItem {
  key: AppNavKey;
  label: string;
  route: string;
  detail: string;
  keywords: string[];
  matchPrefixes: readonly string[];
}

const APP_NAV_ITEMS: readonly AppNavItem[] = [
  {
    key: "studio",
    label: "创作",
    route: "/",
    detail: "自由聊天、生图、图生图、修图",
    keywords: ["new", "studio", "home", "创作", "首页", "工作台", "聊天", "生图"],
    matchPrefixes: ["/"],
  },
  {
    key: "video",
    label: "视频",
    route: "/video",
    detail: "文生视频、首帧生成和视频历史",
    keywords: ["video", "seedance", "视频", "文生视频", "图生视频", "首帧"],
    matchPrefixes: ["/video"],
  },
  {
    key: "projects",
    label: "项目",
    route: "/projects",
    detail: "服饰模特图、海报、分镜等流程型工作",
    keywords: ["projects", "workflow", "项目", "工作流", "服饰", "海报"],
    matchPrefixes: ["/projects"],
  },
  {
    key: "assets",
    label: "素材",
    route: "/assets",
    detail: "生成图、上传图、模型库、风格库和分享记录",
    keywords: ["assets", "stream", "library", "feed", "素材", "资产", "图库", "图片", "模型库", "风格库"],
    matchPrefixes: ["/assets", "/stream", "/library"],
  },
  {
    key: "me",
    label: "我的",
    route: "/me",
    detail: "账户、记忆、系统提示词、API Key、账单和设置",
    keywords: ["me", "profile", "account", "settings", "我的", "账号", "设置", "记忆", "账单", "api key"],
    matchPrefixes: ["/me", "/settings"],
  },
];

export const DEFAULT_NAV_VISIBILITY: Required<NavVisibility> = {
  studio: true,
  video: true,
  projects: true,
  assets: true,
};

export function normalizeNavVisibility(
  value: NavVisibility | undefined | null,
): Required<NavVisibility> {
  return {
    studio: value?.studio !== false,
    video: value?.video !== false,
    projects: value?.projects !== false,
    assets: value?.assets !== false,
  };
}

function isNavItemVisible(
  item: AppNavItem,
  visibility: NavVisibility | undefined | null,
): boolean {
  if (item.key === "me") return true;
  return normalizeNavVisibility(visibility)[item.key] !== false;
}

export function getAppNavItems(
  visibility?: NavVisibility | null,
): readonly AppNavItem[] {
  return APP_NAV_ITEMS.filter((item) => isNavItemVisible(item, visibility));
}

function matchesPathPrefix(pathname: string, prefix: string): boolean {
  if (prefix === "/") return pathname === "/" || pathname === "";
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

function getActiveNavKeyFromItems(
  pathname: string,
  items: readonly AppNavItem[],
): AppNavKey | null {
  const normalized = pathname || "/";
  for (const item of items) {
    if (item.matchPrefixes.some((prefix) => matchesPathPrefix(normalized, prefix))) {
      return item.key;
    }
  }
  return null;
}

export function getActiveNavKey(
  pathname: string,
  visibility?: NavVisibility | null,
): AppNavKey | null {
  return getActiveNavKeyFromItems(pathname, getAppNavItems(visibility));
}

function getRouteNavKey(pathname: string): AppNavKey | null {
  return getActiveNavKeyFromItems(pathname, APP_NAV_ITEMS);
}

export function getFirstVisibleNavRoute(
  visibility?: NavVisibility | null,
): string {
  return getAppNavItems(visibility)[0]?.route ?? "/me";
}

export function getRedirectForHiddenNavPath(
  pathname: string,
  visibility?: NavVisibility | null,
): string | null {
  const key = getRouteNavKey(pathname);
  if (!key || key === "me") return null;
  if (normalizeNavVisibility(visibility)[key] !== false) return null;
  return getFirstVisibleNavRoute(visibility);
}

export function isSameRoute(pathname: string, route: string): boolean {
  return matchesPathPrefix(pathname || "/", route);
}
