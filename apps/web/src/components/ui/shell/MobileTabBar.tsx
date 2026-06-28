"use client";

import { motion } from "framer-motion";
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
import { SPRING, DURATION, EASE } from "@/lib/motion";
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
        "fixed inset-x-0 bottom-0 border-t border-[var(--border-amber)]/20 bg-[var(--bg-1)] backdrop-blur-2xl mobile-perf-surface safe-x",
        "transition-[transform,opacity] duration-[var(--dur-normal)] ease-[var(--ease-shutter)]",
        lightboxOpen ? "opacity-0 pointer-events-none" : "opacity-100",
        isKeyboardOpen ? "translate-y-full pointer-events-none" : "translate-y-0",
      ].join(" ")}
      style={{
        zIndex: "var(--z-tabbar, 20)" as unknown as number,
        paddingBottom: "env(safe-area-inset-bottom, 0px)",
      }}
    >
      <ul className="relative flex items-stretch h-12 max-w-[640px] mx-auto">
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
                className="relative w-full h-full flex-col gap-0.5 rounded-none"
              >
                {active && (
                  <motion.span
                    layoutId="mtab-dot"
                    aria-hidden
                    className="absolute top-0 left-1/2 -translate-x-1/2 h-[2px] w-6 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)]"
                    transition={SPRING.snap}
                  />
                )}
                <motion.span
                  animate={active ? { scale: [0.9, 1] } : { scale: 1 }}
                  transition={{ duration: DURATION.normal, ease: EASE.develop }}
                  className={[
                    "inline-flex items-center justify-center",
                    active ? "text-[var(--amber-400)]" : "text-[var(--fg-2)]",
                  ].join(" ")}
                  style={active ? { filter: "drop-shadow(0 0 8px var(--amber-glow))" } : undefined}
                >
                  <Icon className="w-5 h-5" strokeWidth={active ? 2.2 : 1.7} />
                </motion.span>
                <span
                  className={[
                    "text-[10px] leading-none font-medium mt-px",
                    active ? "text-[var(--amber-300)]" : "text-[var(--fg-2)]",
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
