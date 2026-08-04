import { RouteLoadingSkeleton } from "@/components/RouteLoadingSkeleton";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";

export default function Loading() {
  return (
    <SettingsShell title="设置加载中" subtitle="SETTINGS">
      <div
        className="page-frame"
        data-width="settings"
        aria-busy="true"
        aria-live="polite"
      >
        <RouteLoadingSkeleton title="设置加载中" />
      </div>
    </SettingsShell>
  );
}
