import type { CanvasNodeType } from "./types";

type CanvasConfigRecord = Record<string, unknown>;
export function validateCanvasNodeUi(value: unknown): boolean {
  return (
    isCanvasConfigRecord(value) &&
    hasOnlyKeys(value, NODE_UI_KEYS) &&
    optionalBoolean(value, "collapsed") &&
    optionalNullableString(value, "color_tag", 32) &&
    optionalNullableString(value, "preset_id", 128)
  );
}

export function validateCanvasNodeConfig(
  type: CanvasNodeType,
  value: unknown,
): boolean {
  return isCanvasConfigRecord(value) && CANVAS_CONFIG_VALIDATORS[type](value);
}


type CanvasConfigValidator = (config: CanvasConfigRecord) => boolean;

const NODE_UI_KEYS = new Set(["collapsed", "color_tag", "preset_id"]);
// Accepted only so locally cached V1 graphs can load and be re-saved without it.
const IMAGE_CONFIG_KEYS = new Set([
  "model",
  "aspect_ratio",
  "size",
  "quality",
  "size_mode",
  "fixed_size",
  "render_quality",
  "count",
  "fast",
  "output_format",
  "output_compression",
  "background",
  "moderation",
]);
const VIDEO_CONFIG_KEYS = new Set([
  "mode",
  "model",
  "duration_s",
  "resolution",
  "aspect_ratio",
  "generate_audio",
  "seed",
  "watermark",
]);

const CANVAS_CONFIG_VALIDATORS: Record<
  CanvasNodeType,
  CanvasConfigValidator
> = {
  prompt: promptConfigIsValid,
  prompt_merge: promptMergeConfigIsValid,
  image_asset: imageAssetConfigIsValid,
  mask_asset: imageAssetConfigIsValid,
  video_asset: videoAssetConfigIsValid,
  image_generate: imageConfigIsValid,
  image_edit: imageConfigIsValid,
  image_inpaint: imageConfigIsValid,
  image_upscale: imageConfigIsValid,
  video_generate: (config) => videoConfigIsValid("video_generate", config),
  video_text_generate: (config) =>
    videoConfigIsValid("video_text_generate", config),
  video_image_generate: (config) =>
    videoConfigIsValid("video_image_generate", config),
  video_reference_generate: (config) =>
    videoConfigIsValid("video_reference_generate", config),
  note: noteConfigIsValid,
  frame: frameConfigIsValid,
  delivery: deliveryConfigIsValid,
};

function promptConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, new Set(["text", "locked"])) &&
    optionalString(config, "text", 10_000) &&
    optionalBoolean(config, "locked")
  );
}

function promptMergeConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(
      config,
      new Set(["separator", "prefix", "suffix", "trim", "dedupe"]),
    ) &&
    optionalString(config, "separator", 32) &&
    optionalString(config, "prefix", 2_000) &&
    optionalString(config, "suffix", 2_000) &&
    optionalBoolean(config, "trim") &&
    optionalBoolean(config, "dedupe")
  );
}

function imageAssetConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, new Set(["image_id", "display_name", "crop"])) &&
    optionalString(config, "image_id", 36) &&
    optionalNullableString(config, "display_name", 255) &&
    optionalCrop(config.crop)
  );
}

function videoAssetConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, new Set(["video_id", "display_name"])) &&
    optionalString(config, "video_id", 36) &&
    optionalNullableString(config, "display_name", 255)
  );
}

function imageConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, IMAGE_CONFIG_KEYS) &&
    optionalNullableString(config, "model", 128) &&
    optionalString(config, "aspect_ratio", 16) &&
    optionalSetValue(config, "size", new Set(["1K", "2K", "4K", "1k", "2k", "4k"])) &&
    optionalSetValue(
      config,
      "quality",
      new Set(["standard", "high", "1k", "2k", "4k"]),
    ) &&
    optionalSetValue(config, "size_mode", new Set(["auto", "fixed"])) &&
    optionalNullableString(config, "fixed_size", 32) &&
    optionalSetValue(
      config,
      "render_quality",
      new Set(["auto", "low", "medium", "high"]),
    ) &&
    optionalInteger(config, "count", 1, 10) &&
    optionalLegacyImageFast(config) &&
    optionalNullableSetValue(
      config,
      "output_format",
      new Set(["png", "jpeg", "webp"]),
    ) &&
    optionalNullableInteger(config, "output_compression", 0, 100) &&
    optionalSetValue(
      config,
      "background",
      new Set(["auto", "opaque", "transparent"]),
    ) &&
    optionalSetValue(config, "moderation", new Set(["auto", "low"]))
  );
}

function videoConfigIsValid(
  type: CanvasNodeType,
  config: CanvasConfigRecord,
): boolean {
  const fixedMode =
    type === "video_text_generate"
      ? "t2v"
      : type === "video_image_generate"
        ? "i2v"
        : type === "video_reference_generate"
          ? "reference"
          : null;
  return (
    hasOnlyKeys(config, VIDEO_CONFIG_KEYS) &&
    optionalSetValue(config, "mode", new Set(["t2v", "i2v", "reference"])) &&
    (fixedMode === null || config.mode === undefined || config.mode === fixedMode) &&
    optionalNullableString(config, "model", 64) &&
    optionalSmartDuration(config, "duration_s") &&
    optionalString(config, "resolution", 16) &&
    optionalString(config, "aspect_ratio", 16) &&
    optionalBoolean(config, "generate_audio") &&
    optionalNullableInteger(
      config,
      "seed",
      Number.MIN_SAFE_INTEGER,
      Number.MAX_SAFE_INTEGER,
    ) &&
    optionalBoolean(config, "watermark")
  );
}

function noteConfigIsValid(config: CanvasConfigRecord): boolean {
  if (
    !hasOnlyKeys(config, new Set(["text", "tags"])) ||
    !optionalString(config, "text", 20_000)
  ) {
    return false;
  }
  if (config.tags === undefined) return true;
  return (
    Array.isArray(config.tags) &&
    config.tags.length <= 12 &&
    config.tags.every(
      (tag) => typeof tag === "string" && tag.trim().length > 0 && tag.trim().length <= 32,
    )
  );
}

function frameConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(
      config,
      new Set(["label", "collapsed", "hidden_in_run", "runnable_scope"]),
    ) &&
    optionalString(config, "label", 255) &&
    optionalBoolean(config, "collapsed") &&
    optionalBoolean(config, "hidden_in_run") &&
    optionalBoolean(config, "runnable_scope")
  );
}

function deliveryConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(
      config,
      new Set(["set_as_thumbnail", "thumbnail_source_node_id"]),
    ) &&
    optionalBoolean(config, "set_as_thumbnail") &&
    optionalNullableEntityId(config, "thumbnail_source_node_id")
  );
}

export function isCanvasConfigRecord(
  value: unknown,
): value is CanvasConfigRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasOnlyKeys(
  value: CanvasConfigRecord,
  allowed: ReadonlySet<string>,
): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function optionalString(
  config: CanvasConfigRecord,
  key: string,
  maxLength: number,
): boolean {
  return (
    config[key] === undefined ||
    (typeof config[key] === "string" &&
      (config[key] as string).length <= maxLength)
  );
}

function optionalNullableString(
  config: CanvasConfigRecord,
  key: string,
  maxLength: number,
): boolean {
  return config[key] === null || optionalString(config, key, maxLength);
}

function optionalBoolean(config: CanvasConfigRecord, key: string): boolean {
  return config[key] === undefined || typeof config[key] === "boolean";
}

function optionalLegacyImageFast(config: CanvasConfigRecord): boolean {
  return config.fast === null || optionalBoolean(config, "fast");
}

function optionalSetValue(
  config: CanvasConfigRecord,
  key: string,
  values: ReadonlySet<string>,
): boolean {
  return config[key] === undefined || values.has(String(config[key]));
}

function optionalNullableSetValue(
  config: CanvasConfigRecord,
  key: string,
  values: ReadonlySet<string>,
): boolean {
  return config[key] === null || optionalSetValue(config, key, values);
}

function optionalInteger(
  config: CanvasConfigRecord,
  key: string,
  minimum: number,
  maximum: number,
): boolean {
  const value = config[key];
  return (
    value === undefined ||
    (Number.isInteger(value) &&
      Number(value) >= minimum &&
      Number(value) <= maximum)
  );
}

function optionalNullableInteger(
  config: CanvasConfigRecord,
  key: string,
  minimum: number,
  maximum: number,
): boolean {
  return config[key] === null || optionalInteger(config, key, minimum, maximum);
}

function optionalSmartDuration(
  config: CanvasConfigRecord,
  key: string,
): boolean {
  const value = config[key];
  return (
    value === undefined ||
    value === -1 ||
    (Number.isInteger(value) && Number(value) >= 3 && Number(value) <= 15)
  );
}

function optionalCrop(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (
    !isCanvasConfigRecord(value) ||
    !hasOnlyKeys(value, new Set(["x", "y", "width", "height"]))
  ) {
    return false;
  }
  const x = Number(value.x);
  const y = Number(value.y);
  const width = Number(value.width);
  const height = Number(value.height);
  return (
    [x, y, width, height].every(Number.isFinite) &&
    x >= 0 &&
    y >= 0 &&
    width > 0 &&
    height > 0 &&
    x + width <= 1 &&
    y + height <= 1
  );
}

function optionalNullableEntityId(
  config: CanvasConfigRecord,
  key: string,
): boolean {
  const value = config[key];
  return (
    value === undefined ||
    value === null ||
    (typeof value === "string" &&
      /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/.test(value))
  );
}
