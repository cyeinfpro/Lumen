export interface NotificationRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface NotificationPlacement {
  left: number;
  bottom: number;
  width: number;
  maxHeight: number;
}

// Inputs are rendered rectangles, including visualViewport offsets. A modal
// bounds the stack to its surface so transformed/overflow-hidden parents do not
// clip a viewport-positioned notification out of reach.
export function calculateNotificationPlacement({
  viewport,
  owner,
  obstacles,
  gap = 12,
  preferredWidth = 320,
}: {
  viewport: NotificationRect;
  owner?: NotificationRect | null;
  obstacles: readonly NotificationRect[];
  gap?: number;
  preferredWidth?: number;
}): NotificationPlacement {
  const bounds = owner ? {
    left: Math.max(viewport.left, owner.left),
    top: Math.max(viewport.top, owner.top),
    right: Math.min(viewport.right, owner.right),
    bottom: Math.min(viewport.bottom, owner.bottom),
  } : viewport;
  const width = Math.max(0, Math.min(preferredWidth, bounds.right - bounds.left - gap * 2));
  const left = Math.max(bounds.left + gap, bounds.right - gap - width);
  let bottom = bounds.bottom - gap;
  for (const obstacle of obstacles) {
    if (
      obstacle.right > left && obstacle.left < left + width &&
      obstacle.bottom > bounds.top && obstacle.top < bottom &&
      obstacle.bottom > obstacle.top
    ) {
      bottom = Math.min(bottom, obstacle.top - gap);
    }
  }
  const top = bounds.top + gap;
  return { left, bottom: Math.max(top, bottom), width, maxHeight: Math.max(0, bottom - top) };
}
