import { createHash } from "node:crypto";
import {
  deepEqual,
  doesNotMatch,
  equal,
  match,
  ok,
} from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const source = readFileSync(new URL("./Onboarding.tsx", import.meta.url), "utf8");
const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

type PresetContract = {
  title: string;
  text: string;
  mode: string;
  previewSrc: string;
  previewAlt: string;
};

function stringProperty(
  object: ts.ObjectLiteralExpression,
  name: keyof PresetContract,
): string {
  const property = object.properties.find(
    (candidate): candidate is ts.PropertyAssignment =>
      ts.isPropertyAssignment(candidate) &&
      ts.isIdentifier(candidate.name) &&
      candidate.name.text === name,
  );
  ok(property && ts.isStringLiteral(property.initializer), `missing preset ${name}`);
  return property.initializer.text;
}

function presetContracts(): PresetContract[] {
  const file = ts.createSourceFile(
    "Onboarding.tsx",
    source,
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TSX,
  );
  let presets: ts.ArrayLiteralExpression | undefined;

  function visit(node: ts.Node): void {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === "PRESETS" &&
      node.initializer &&
      ts.isArrayLiteralExpression(node.initializer)
    ) {
      presets = node.initializer;
      return;
    }
    ts.forEachChild(node, visit);
  }

  visit(file);
  ok(presets, "missing PRESETS array");
  return presets.elements.map((element) => {
    ok(ts.isObjectLiteralExpression(element), "preset must be an object literal");
    return {
      title: stringProperty(element, "title"),
      text: stringProperty(element, "text"),
      mode: stringProperty(element, "mode"),
      previewSrc: stringProperty(element, "previewSrc"),
      previewAlt: stringProperty(element, "previewAlt"),
    };
  });
}

function lossyWebpDimensions(buffer: Buffer): { width: number; height: number } {
  equal(buffer.toString("ascii", 0, 4), "RIFF");
  equal(buffer.toString("ascii", 8, 12), "WEBP");

  let offset = 12;
  while (offset + 8 <= buffer.length) {
    const chunkType = buffer.toString("ascii", offset, offset + 4);
    const chunkSize = buffer.readUInt32LE(offset + 4);
    const dataOffset = offset + 8;
    if (chunkType === "VP8 ") {
      equal(buffer.toString("hex", dataOffset + 3, dataOffset + 6), "9d012a");
      return {
        width: buffer.readUInt16LE(dataOffset + 6) & 0x3fff,
        height: buffer.readUInt16LE(dataOffset + 8) & 0x3fff,
      };
    }
    offset = dataOffset + chunkSize + (chunkSize % 2);
  }
  throw new Error("missing lossy WebP frame");
}

test("studio presets keep four distinct image-mode prompt injections", () => {
  const presets = presetContracts();

  equal(presets.length, 4);
  deepEqual(
    presets.map((preset) => preset.title),
    [
      "电影级雨夜街角",
      "极简数码静物海报",
      "高端时尚肖像特写",
      "未来感建筑构图",
    ],
  );
  ok(presets.every((preset) => preset.mode === "image"));
  ok(presets.every((preset) => preset.text.length >= 30));
  equal(new Set(presets.map((preset) => preset.text)).size, 4);
  match(source, /onPick\(preset\.text,\s*preset\.mode\)/);
  match(source, /if \(loading\) return/);
});

test("studio presets use inspectable local raster assets", () => {
  const presets = presetContracts();

  for (const preset of presets) {
    match(preset.previewSrc, /^\/inspiration\/[a-z0-9-]+\.webp$/);
    doesNotMatch(preset.previewSrc, /^https?:/);
    ok(preset.previewAlt.length >= 12, `${preset.title} needs descriptive alt text`);

    const assetPath = resolve(webRoot, "public", preset.previewSrc.slice(1));
    ok(statSync(assetPath).size >= 20_000, `${preset.previewSrc} is not inspectable`);
    const dimensions = lossyWebpDimensions(readFileSync(assetPath));
    ok(dimensions.width >= 1200, `${preset.previewSrc} is too narrow`);
    ok(dimensions.height >= 750, `${preset.previewSrc} is too short`);
  }

  equal(new Set(presets.map((preset) => preset.previewSrc)).size, 4);
  match(source, /import Image from "next\/image"/);
  match(source, /aspect-\[8\/5\]/);
  doesNotMatch(source, /previewKind|radial-gradient|bg-gradient|text-\[(?:10|11)px\]/);
});

test("studio raster content matches the manually approved visual snapshots", () => {
  const approvedSha256: Record<string, string> = {
    "/inspiration/rainy-cinematic-street.webp":
      "11f709b54817dc18e1179f2f92468b429431e3d60c9a95dfd931fc69774e468d",
    "/inspiration/minimal-product-still-life.webp":
      "603127c87b3b32fb62cac7c1c8232b6cac1e6a043a14b1f659534d1cbb520245",
    "/inspiration/editorial-fashion-portrait.webp":
      "7dd430eb1f8401f8ed4877f85c7a90e6dad76360d8b586428544379f0a2e1ba5",
    "/inspiration/coastal-concept-architecture.webp":
      "e77faf74648945abcdba38c92c0314fd1339a0f9c9b067b0a5743ed60260d871",
  };

  for (const preset of presetContracts()) {
    const assetPath = resolve(webRoot, "public", preset.previewSrc.slice(1));
    const digest = createHash("sha256")
      .update(readFileSync(assetPath))
      .digest("hex");
    equal(digest, approvedSha256[preset.previewSrc], preset.previewSrc);
  }
});

test("studio preset cards use the shared motion-safe V2 surface", () => {
  match(source, /group surface-card-v2/);
  doesNotMatch(source, /whileHover|hover:-translate-y/);
  match(source, /group-hover:scale-\[1\.02\] motion-reduce:transform-none/);
});
