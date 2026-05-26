export type AppNavKey = "studio" | "projects" | "assets" | "me";

export interface AppNavItem {
  key: AppNavKey;
  label: string;
  route: string;
  detail: string;
  keywords: string[];
  matchPrefixes: readonly string[];
}

const IS_DESKTOP_RUNTIME = process.env.NEXT_PUBLIC_LUMEN_RUNTIME === "desktop";

const DOCKER_NAV_ITEMS: readonly AppNavItem[] = [
  {
    key: "studio",
    label: "创作",
    route: "/",
    detail: "自由聊天、生图、图生图、修图",
    keywords: ["new", "studio", "home", "创作", "首页", "工作台", "聊天", "生图"],
    matchPrefixes: ["/"],
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
    label: "资产",
    route: "/stream",
    detail: "生成图、上传图、模型库、风格库和分享记录",
    keywords: ["assets", "stream", "library", "feed", "资产", "图库", "图片", "模型库", "风格库"],
    matchPrefixes: ["/stream", "/library"],
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

const DESKTOP_NAV_ITEMS: readonly AppNavItem[] = [
  {
    key: "studio",
    label: "创作",
    route: "/",
    detail: "本机对话、生图、图生图、修图",
    keywords: ["new", "studio", "home", "创作", "首页", "工作台", "聊天", "生图"],
    matchPrefixes: ["/"],
  },
  {
    key: "assets",
    label: "资产",
    route: "/assets",
    detail: "本机生成图、上传图和会话素材",
    keywords: ["assets", "stream", "feed", "资产", "图库", "图片"],
    matchPrefixes: ["/assets", "/stream"],
  },
  {
    key: "me",
    label: "设置",
    route: "/me",
    detail: "供应商池、记忆、数据目录、诊断和更新",
    keywords: ["me", "profile", "account", "settings", "我的", "账号", "设置", "记忆"],
    matchPrefixes: ["/me", "/settings"],
  },
];

export const APP_NAV_ITEMS: readonly AppNavItem[] = IS_DESKTOP_RUNTIME
  ? DESKTOP_NAV_ITEMS
  : DOCKER_NAV_ITEMS;

export function matchesPathPrefix(pathname: string, prefix: string): boolean {
  if (prefix === "/") return pathname === "/" || pathname === "";
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

export function getActiveNavKey(pathname: string): AppNavKey | null {
  const normalized = pathname || "/";
  for (const item of APP_NAV_ITEMS) {
    if (item.matchPrefixes.some((prefix) => matchesPathPrefix(normalized, prefix))) {
      return item.key;
    }
  }
  return null;
}

export function isSameRoute(pathname: string, route: string): boolean {
  return matchesPathPrefix(pathname || "/", route);
}
