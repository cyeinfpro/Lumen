type DialogEase = readonly [number, number, number, number];

const FALLBACK_DIALOG_DURATION_SECONDS = 0.18;
const FALLBACK_DIALOG_EASE: DialogEase = [0.22, 1, 0.36, 1];

let cachedDialogTransition:
  | { duration: number; ease: DialogEase }
  | undefined;

function parseDurationSeconds(value: string): number {
  const duration = Number.parseFloat(value);
  if (!Number.isFinite(duration)) return FALLBACK_DIALOG_DURATION_SECONDS;
  return value.trim().endsWith("ms") ? duration / 1000 : duration;
}

function parseCubicBezier(value: string): DialogEase {
  const match = value.match(/cubic-bezier\(([^)]+)\)/);
  if (!match) return FALLBACK_DIALOG_EASE;
  const points = match[1].split(",").map((point) => Number(point.trim()));
  if (points.length !== 4 || points.some((point) => !Number.isFinite(point))) {
    return FALLBACK_DIALOG_EASE;
  }
  return [points[0], points[1], points[2], points[3]];
}

export function getCanvasDialogTransition() {
  if (cachedDialogTransition) return cachedDialogTransition;
  if (typeof document === "undefined") {
    return {
      duration: FALLBACK_DIALOG_DURATION_SECONDS,
      ease: FALLBACK_DIALOG_EASE,
    };
  }

  const styles = window.getComputedStyle(document.documentElement);
  cachedDialogTransition = {
    duration: parseDurationSeconds(styles.getPropertyValue("--dur-dialog")),
    ease: parseCubicBezier(styles.getPropertyValue("--ease-develop")),
  };
  return cachedDialogTransition;
}
