"use client";

import {
  Camera,
  Clapperboard,
  FolderKanban,
  Images,
  User,
  type LucideIcon,
} from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useMemo } from "react";
import { useHaptic } from "@/hooks/useHaptic";
import { useUiStore } from "@/store/useUiStore";
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import {
  getActiveNavKey,
  getAppNavItems,
  isSameRoute,
  type AppNavItem,
  type AppNavKey,
} from "./navigation";

type TabDef = AppNavItem & { Icon: LucideIcon };

const TAB_ICONS: Record<AppNavKey, LucideIcon> = {
  studio: Camera,
  video: Clapperboard,
  projects: FolderKanban,
  assets: Images,
  me: User,
};

export function MobileTabBar() {
  const pathname = usePathname();
  const router = useRouter();
  const { haptic } = useHaptic();
  // spec §3.3：Lightbox 打开时 fade-out
  const lightboxOpen = useUiStore((s) => s.lightbox.open);
  const navVisibility = useUiStore((s) => s.navVisibility);
  const { isKeyboardOpen } = useKeyboardInset();
  const tabs = useMemo(
    () =>
      getAppNavItems(navVisibility).map((item) => ({
        ...item,
        Icon: TAB_ICONS[item.key],
      })),
    [navVisibility],
  );

  const activeIndex = useMemo(() => {
    const activeKey = getActiveNavKey(pathname, navVisibility) ?? "studio";
    const index = tabs.findIndex((tab) => tab.key === activeKey);
    return index >= 0 ? index : 0;
  }, [pathname, tabs, navVisibility]);

  const onTap = useCallback(
    (tab: TabDef) => {
      haptic("light");
      if (isSameRoute(pathname, tab.route)) {
        window.scrollTo({ top: 0, behavior: "smooth" });
        if (typeof window !== "undefined" && window.location.search) {
          router.replace(tab.route);
        }
        return;
      }
      router.push(tab.route);
    },
    [haptic, pathname, router],
  );

  return (
    <nav
      aria-label="主导航"
      aria-hidden={lightboxOpen || undefined}
      inert={lightboxOpen ? true : undefined}
      className={[
        "fixed inset-x-0 bottom-0 border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/96 safe-x",
        "transition-[transform,opacity] duration-[var(--dur-normal)] ease-[var(--ease-shutter)]",
        lightboxOpen ? "opacity-0 pointer-events-none" : "opacity-100",
        isKeyboardOpen ? "translate-y-full pointer-events-none" : "translate-y-0",
      ].join(" ")}
      style={{
        zIndex: "var(--z-tabbar, 20)" as unknown as number,
        paddingBottom: "env(safe-area-inset-bottom, 0px)",
      }}
    >
      <ul className="relative mx-auto flex h-14 max-w-[640px] items-stretch px-1">
        {tabs.map((tab, idx) => {
          const active = idx === activeIndex;
          const { Icon } = tab;
          return (
            <li key={tab.key} className="flex-1">
              <Pressable
                size="default"
                minHit={true}
                pressScale="soft"
                haptic={false}
                onPress={() => onTap(tab)}
                aria-label={tab.label}
                aria-current={active ? "page" : undefined}
                className="relative h-full w-full flex-col gap-0.5 rounded-[var(--radius-control)]"
              >
                {active && (
                  <span
                    aria-hidden
                    className="absolute inset-x-1 inset-y-1 rounded-[var(--radius-control)] bg-[var(--surface-selected)]"
                  />
                )}
                <span
                  className={[
                    "relative inline-flex items-center justify-center",
                    active ? "text-[var(--accent)]" : "text-[var(--fg-2)]",
                  ].join(" ")}
                >
                  <Icon className="h-[21px] w-[21px]" strokeWidth={active ? 2 : 1.7} />
                </span>
                <span
                  className={[
                    "relative mt-px text-[11px] font-medium leading-none",
                    active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)]",
                  ].join(" ")}
                >
                  {tab.label}
                </span>
              </Pressable>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
