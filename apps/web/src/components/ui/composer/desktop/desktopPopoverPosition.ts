export type DesktopPopoverAlign = "left" | "center" | "right";

export interface DesktopPopoverAnchorRect {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
}

export interface DesktopPopoverPosition {
  left: number;
  top: number;
}

export function calculateDesktopPopoverPosition({
  anchorRect,
  panelWidth,
  panelHeight,
  viewportWidth,
  viewportHeight,
  align,
  gutter = 12,
  gap = 8,
}: {
  anchorRect: DesktopPopoverAnchorRect;
  panelWidth: number;
  panelHeight: number;
  viewportWidth: number;
  viewportHeight: number;
  align: DesktopPopoverAlign;
  gutter?: number;
  gap?: number;
}): DesktopPopoverPosition {
  let preferredLeft = anchorRect.left;
  if (align === "center") {
    preferredLeft = anchorRect.left + anchorRect.width / 2 - panelWidth / 2;
  } else if (align === "right") {
    preferredLeft = anchorRect.right - panelWidth;
  }

  const maxLeft = Math.max(gutter, viewportWidth - panelWidth - gutter);
  const left = Math.min(Math.max(preferredLeft, gutter), maxLeft);

  const topAbove = anchorRect.top - panelHeight - gap;
  const preferredTop =
    topAbove >= gutter ? topAbove : anchorRect.bottom + gap;
  const maxTop = Math.max(gutter, viewportHeight - panelHeight - gutter);
  const top = Math.min(Math.max(preferredTop, gutter), maxTop);

  return { left, top };
}
