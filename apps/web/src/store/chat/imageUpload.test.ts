import assert from "node:assert/strict";
import test from "node:test";

const { imageFilenameForMime, nextCompressedSide } = await import(
  new URL("./imageUpload.ts", import.meta.url).href
);

test("image upload filenames follow the encoded mime", () => {
  assert.equal(imageFilenameForMime("photo.png", "image/webp"), "photo.webp");
  assert.equal(imageFilenameForMime("photo", "image/jpeg"), "photo.jpg");
  assert.equal(imageFilenameForMime("  ", "image/png"), "image.png");
});

test("image upload shrinking is bounded and monotonic", () => {
  assert.equal(nextCompressedSide(512, 40 * 1024 * 1024), 512);
  assert.ok(nextCompressedSide(2048, 16 * 1024 * 1024) < 2048);
  assert.ok(nextCompressedSide(2048, 16 * 1024 * 1024) >= 512);
});
