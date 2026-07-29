import type { Stroke } from "../types";
import type { DisplayDimensions, MaskExport } from "./types";

const COVERAGE_SAMPLE_STRIDE = 3;
const MASK_PREVIEW_MAX_EDGE = 512;

export function estimateLuminance(image: HTMLImageElement): number {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 32;
    canvas.height = 32;
    const context = canvas.getContext("2d");
    if (!context) return 0.6;
    context.drawImage(image, 0, 0, 32, 32);
    const data = context.getImageData(0, 0, 32, 32).data;
    let sum = 0;
    for (let index = 0; index < data.length; index += 4) {
      sum +=
        data[index] * 0.299 +
        data[index + 1] * 0.587 +
        data[index + 2] * 0.114;
    }
    return sum / (32 * 32 * 255);
  } catch {
    return 0.6;
  }
}

function drawStrokes(
  context: CanvasRenderingContext2D,
  strokes: Stroke[],
  coordinateScale: number,
) {
  context.lineCap = "round";
  context.lineJoin = "round";
  for (const stroke of strokes) {
    if (stroke.points.length < 2) continue;
    context.beginPath();
    context.moveTo(
      stroke.points[0] * coordinateScale,
      stroke.points[1] * coordinateScale,
    );
    for (let index = 2; index < stroke.points.length; index += 2) {
      context.lineTo(
        stroke.points[index] * coordinateScale,
        stroke.points[index + 1] * coordinateScale,
      );
    }
    if (stroke.points.length === 2) {
      context.lineTo(
        stroke.points[0] * coordinateScale + 0.01,
        stroke.points[1] * coordinateScale,
      );
    }
    context.lineWidth = stroke.radius * 2 * coordinateScale;
    if (stroke.tool === "brush") {
      context.globalCompositeOperation = "destination-out";
      context.strokeStyle = "rgba(0,0,0,1)";
    } else {
      context.globalCompositeOperation = "source-over";
      context.strokeStyle = "#ffffff";
    }
    context.stroke();
  }
  context.globalCompositeOperation = "source-over";
}

export function estimateLiveCoverage(
  dimensions: DisplayDimensions,
  strokes: Stroke[],
): number {
  if (!dimensions.width || !dimensions.height || strokes.length === 0) return 0;
  const canvas = document.createElement("canvas");
  canvas.width = dimensions.width;
  canvas.height = dimensions.height;
  const context = canvas.getContext("2d");
  if (!context) return 0;
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  drawStrokes(context, strokes, 1);
  const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
  let transparent = 0;
  let count = 0;
  for (let y = 0; y < canvas.height; y += COVERAGE_SAMPLE_STRIDE) {
    for (let x = 0; x < canvas.width; x += COVERAGE_SAMPLE_STRIDE) {
      const alphaIndex = (y * canvas.width + x) * 4 + 3;
      if (data[alphaIndex] === 0) transparent += 1;
      count += 1;
    }
  }
  return count === 0 ? 0 : transparent / count;
}

export function thresholdMaskAlpha(data: Uint8ClampedArray): number {
  let transparentCount = 0;
  const total = data.length / 4;
  for (let index = 3; index < data.length; index += 4) {
    if (data[index] < 128) {
      data[index] = 0;
      transparentCount += 1;
    } else {
      data[index] = 255;
    }
  }
  return total === 0 ? 0 : transparentCount / total;
}

export function previewDimensions(
  width: number,
  height: number,
): { width: number; height: number } {
  const longest = Math.max(width, height);
  if (longest <= MASK_PREVIEW_MAX_EDGE) return { width, height };
  const scale = MASK_PREVIEW_MAX_EDGE / longest;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

function renderMaskPreviewDataUrl(
  source: HTMLCanvasElement,
  width: number,
  height: number,
): string {
  const dimensions = previewDimensions(width, height);
  if (dimensions.width === width && dimensions.height === height) {
    return source.toDataURL("image/png");
  }
  const preview = document.createElement("canvas");
  preview.width = dimensions.width;
  preview.height = dimensions.height;
  const context = preview.getContext("2d");
  if (!context) return source.toDataURL("image/png");
  context.drawImage(source, 0, 0, preview.width, preview.height);
  return preview.toDataURL("image/png");
}

export async function exportMaskCanvas(
  image: HTMLImageElement | null,
  displayScale: number,
  strokes: Stroke[],
): Promise<MaskExport | null> {
  if (!image) return null;
  const { naturalWidth: width, naturalHeight: height } = image;
  if (!width || !height) return null;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, width, height);
  drawStrokes(context, strokes, displayScale === 0 ? 1 : 1 / displayScale);

  const imageData = context.getImageData(0, 0, width, height);
  const coverage = thresholdMaskAlpha(imageData.data);
  context.putImageData(imageData, 0, 0);
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/png"),
  );
  if (!blob) return null;
  return {
    blob,
    preview_data_url: renderMaskPreviewDataUrl(canvas, width, height),
    width,
    height,
    coverage,
  };
}
