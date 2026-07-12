import { RouteLoadingSkeleton } from "@/components/RouteLoadingSkeleton";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";

export default function Loading() {
  return (
    <SettingsShell title="设置加载中" subtitle="SETTINGS">
      <div aria-busy="true" aria-live="polite">
        <RouteLoadingSkeleton title="设置加载中" />
      </div>
    </SettingsShell>
  );
}
