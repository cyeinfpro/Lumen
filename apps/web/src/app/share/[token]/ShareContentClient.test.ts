import {
  deepStrictEqual,
  match,
  ok,
  strictEqual,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import type { PublicShareImageOut, PublicShareOut } from "@/lib/types";

const clientUrl = new URL("./ShareContentClient.tsx", import.meta.url);
const galleryUrl = new URL(
  "./ShareContentClientGallery.tsx",
  import.meta.url,
);
const utilsUrl = new URL("./share-content-utils.ts", import.meta.url);
const moduleUrls = [clientUrl, galleryUrl, utilsUrl];
const {
  candidateUrls,
  normalizeShareImages,
  shareImageAlt,
  sharePrompts,
  shareSizeLabel,
} = (await import(utilsUrl.href)) as typeof import("./share-content-utils");

function shareImage(
  overrides: Partial<PublicShareImageOut> = {},
): PublicShareImageOut {
  return {
    id: "image-1",
    image_url: "/api/public/shares/token/images/image-1",
    display_url: "/api/public/shares/token/images/image-1?variant=display",
    preview_url: "/api/public/shares/token/images/image-1?variant=preview",
    thumb_url: "/api/public/shares/token/images/image-1?variant=thumb",
    width: 2048,
    height: 1024,
    mime: "image/png",
    prompt: "保留公开分享提示词",
    ...overrides,
  };
}

test("share image normalization preserves the legacy single-image contract", () => {
  const data = {
    token: "public-token",
    image_url: "/api/public/shares/public-token/image",
    width: 0,
    height: Number.NaN,
    mime: "",
    prompt: undefined,
    images: [],
  } as unknown as PublicShareOut;

  deepStrictEqual(normalizeShareImages(data), [
    {
      id: "public-token",
      image_url: "/api/public/shares/public-token/image",
      width: 1,
      height: 1,
      mime: "image/png",
      prompt: null,
    },
  ]);
});

test("share image surfaces keep their original fallback URL order", () => {
  const image = shareImage();

  deepStrictEqual(candidateUrls(image, "grid"), [
    image.preview_url,
    image.thumb_url,
    image.display_url,
    image.image_url,
  ]);
  deepStrictEqual(candidateUrls(image, "single"), [
    image.display_url,
    image.preview_url,
    image.image_url,
    image.thumb_url,
  ]);
  deepStrictEqual(candidateUrls(image, "lightbox"), [
    image.display_url,
    image.preview_url,
    image.image_url,
    image.thumb_url,
  ]);
  deepStrictEqual(candidateUrls(image, "filmstrip"), [
    image.thumb_url,
    image.preview_url,
    image.display_url,
    image.image_url,
  ]);
});

test("share labels and prompt aggregation remain unchanged", () => {
  const image = shareImage();
  const duplicatePrompt = shareImage({
    id: "image-2",
    prompt: image.prompt,
  });

  strictEqual(shareImageAlt(image), "保留公开分享提示词");
  strictEqual(shareImageAlt(shareImage({ prompt: null })), "分享图片");
  strictEqual(shareSizeLabel([image]), "2048 × 1024 · PNG 格式");
  strictEqual(shareSizeLabel([image, duplicatePrompt]), "2048 × 1024");
  strictEqual(
    shareSizeLabel([image, shareImage({ id: "image-3", width: 1024 })]),
    "多尺寸",
  );
  deepStrictEqual(sharePrompts([image, duplicatePrompt]), [
    "保留公开分享提示词",
  ]);
});

test("share controller delegates rendering without changing public contracts", () => {
  const clientSource = readFileSync(clientUrl, "utf8");
  const gallerySource = readFileSync(galleryUrl, "utf8");

  match(clientSource, /export function ShareContentClient/);
  match(clientSource, /from "\.\/ShareContentClientGallery"/);
  match(clientSource, /navigator\.share/);
  match(clientSource, /window\.location\.href/);
  match(clientSource, /href="\/"/);
  match(gallerySource, /href=\{image\.image_url\}/);
  match(gallerySource, /rel="noopener noreferrer"/);
  match(gallerySource, /elapsed > 650/);
  match(gallerySource, /Math\.abs\(dx\) > 56/);

  for (const url of moduleUrls) {
    const source = readFileSync(url, "utf8");
    const lineCount = source.trimEnd().split("\n").length;
    ok(lineCount <= 800, `${fileURLToPath(url)} is ${lineCount} lines`);
  }
});

test("share page modules compile under the web TypeScript config", () => {
  const webRoot = fileURLToPath(new URL("../../../../", import.meta.url));
  const configPath = fileURLToPath(
    new URL("../../../../tsconfig.json", import.meta.url),
  );
  const rootNames = moduleUrls.map((url) => fileURLToPath(url));
  const rootNameSet = new Set(rootNames);
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  strictEqual(config.error, undefined);
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, webRoot);
  strictEqual(parsed.errors.length, 0);
  const program = ts.createProgram({
    rootNames,
    options: { ...parsed.options, incremental: false, noEmit: true },
  });
  const diagnostics = ts
    .getPreEmitDiagnostics(program)
    .filter(
      (diagnostic) =>
        diagnostic.file == null || rootNameSet.has(diagnostic.file.fileName),
    );
  strictEqual(
    diagnostics.length,
    0,
    ts.formatDiagnostics(diagnostics, {
      getCanonicalFileName: (fileName) => fileName,
      getCurrentDirectory: () => webRoot,
      getNewLine: () => "\n",
    }),
  );
});
