import assert from "node:assert/strict";
import test from "node:test";

const {
  billingMetaFromPayload,
  coerceAspectRatio,
  isoToMs,
  optionalRecord,
  optionalString,
  parseSizeString,
  ssePayloadRecord,
  stringArray,
  structuredAttachmentsFromComposer,
  structuredAttachmentsFromUnknown,
} = await import(
  new URL("./payload.ts", import.meta.url).href
);

test("payload primitives preserve established coercion semantics", () => {
  assert.deepEqual(stringArray(["", "ok", 1]), ["", "ok"]);
  assert.equal(optionalString("  value  "), "value");
  assert.equal(optionalString("   "), undefined);
  assert.equal(coerceAspectRatio("16:9", "7:10"), "16:9");
  assert.equal(coerceAspectRatio("invalid", "7:10"), "7:10");
  assert.deepEqual(parseSizeString("1024x768"), {
    width: 1024,
    height: 768,
  });
  assert.deepEqual(parseSizeString("1024×768"), { width: 0, height: 0 });
});

test("payload record validation reports arrays and invalid SSE shapes", () => {
  const warnings: string[] = [];
  const warn = (message: string) => warnings.push(message);

  assert.equal(optionalRecord([], warn), undefined);
  assert.equal(ssePayloadRecord("generation.progress", [], warn), null);
  assert.deepEqual(ssePayloadRecord("generation.progress", { id: "g1" }), {
    id: "g1",
  });
  assert.deepEqual(warnings, [
    "optional record payload dropped an array",
    "dropped SSE event with invalid payload",
  ]);
});

test("structured attachments keep role defaults and validated backend roles", () => {
  assert.deepEqual(
    structuredAttachmentsFromComposer(
      [
        { id: "first", kind: "upload", data_url: "data:first" },
        { id: "second", kind: "upload", data_url: "data:second" },
      ],
      "image_to_image",
      true,
    ),
    [
      { image_id: "first", role: "edit_target" },
      { image_id: "second", role: "reference" },
    ],
  );
  assert.deepEqual(
    structuredAttachmentsFromUnknown([
      { image_id: "product", role: "product", weight: 0.8 },
      { image_id: "fallback", role: "invalid" },
      { role: "style" },
    ]),
    [
      { image_id: "product", role: "product", weight: 0.8 },
      { image_id: "fallback", role: "reference" },
    ],
  );
});

test("billing metadata honors free and dual-race precedence", () => {
  assert.deepEqual(
    billingMetaFromPayload(
      { billing_label: " paid " },
      {
        is_dual_race_bonus: true,
        billing_exempt_reason: "bonus",
      },
    ),
    {
      is_dual_race_bonus: true,
      billing_free: true,
      billing_label: "paid",
      billing_exempt_reason: "bonus",
    },
  );
});

test("ISO timestamps parse deterministically", () => {
  assert.equal(isoToMs("2026-07-11T00:00:00Z"), 1_783_728_000_000);
  assert.equal(isoToMs("invalid"), 0);
});
