import assert from "node:assert/strict";
import test from "node:test";

const { exportMaskCanvas } = await import(new URL("./canvasRendering.ts", import.meta.url).href);

test("markup exports native-size transparent PNG with ink, never the original image", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "document");
  const operations: string[] = [];
  const context = {
    globalCompositeOperation: "", strokeStyle: "", lineWidth: 0, lineCap: "", lineJoin: "",
    beginPath() {}, moveTo() {}, lineTo() {},
    stroke() { operations.push(this.globalCompositeOperation + ":" + this.strokeStyle); },
    fillRect() { operations.push("fill"); }, drawImage() { operations.push("original-image"); },
    getImageData() { return { data: new Uint8ClampedArray([229, 57, 53, 128, 0, 0, 0, 0]) }; },
    putImageData() { operations.push("threshold"); },
  };
  const canvas = {
    width: 0, height: 0, getContext: () => context,
    toBlob(callback: (blob: Blob) => void, mime: string) { callback(new Blob(["png"], { type: mime })); },
    toDataURL: () => "data:image/png;base64,cG5n",
  };
  Object.defineProperty(globalThis, "document", { configurable: true, value: { createElement: () => canvas } });
  try {
    const result = await exportMaskCanvas({ naturalWidth: 2, naturalHeight: 1 }, 1, [
      { tool: "brush", points: [0, 0, 1, 1], radius: 1 },
      { tool: "eraser", points: [1, 0, 1, 1], radius: 1 },
    ], "markup");
    assert.equal(result.width, 2);
    assert.equal(result.height, 1);
    assert.equal(result.blob.type, "image/png");
    assert.equal(result.coverage, 0.5);
    assert.deepEqual(operations, ["source-over:#e53935", "destination-out:#e53935"]);
    await assert.rejects(exportMaskCanvas({ naturalWidth: 4097, naturalHeight: 4096 }, 1, [{ tool: "brush", points: [0, 0], radius: 1 }], "markup"), /超限/);
    await assert.rejects(exportMaskCanvas({ naturalWidth: 2, naturalHeight: 1 }, 1, [], "markup"), /笔画/);
  } finally {
    if (original) Object.defineProperty(globalThis, "document", original);
    else Reflect.deleteProperty(globalThis, "document");
  }
});
