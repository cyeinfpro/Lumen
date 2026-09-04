import type { LightboxItem } from "@/components/ui/lightbox/types";
import type { ApparelModelLibraryItem } from "@/lib/apiClient";
import type { LightboxAction } from "@/store/useUiStore";

export function createModelLibrarySelectionAction(
  items: readonly ApparelModelLibraryItem[],
  label: string,
  onSelectItem: (item: ApparelModelLibraryItem) => void,
  pending = false,
): LightboxAction {
  const itemMap = new Map(items.map((item) => [item.id, item]));
  return {
    label,
    pending,
    onClick: (lightboxItem: LightboxItem) => {
      const libraryItem = itemMap.get(lightboxItem.id);
      if (libraryItem) onSelectItem(libraryItem);
    },
  };
}
