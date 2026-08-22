import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

function compile(
  relativePath: string,
  overrides: Record<string, unknown> = {},
) {
  const url = new URL(relativePath, import.meta.url);
  const source = readFileSync(url, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: url.pathname,
  }).outputText;
  const compiledModule = { exports: {} as Record<string, unknown> };
  new Function("require", "module", "exports", output)(
    (id: string) => {
      if (id in overrides) return overrides[id];
      throw new Error(`missing test dependency: ${id}`);
    },
    compiledModule,
    compiledModule.exports,
  );
  return compiledModule.exports;
}

class TestApiError extends Error {
  code: string;
  status: number;

  constructor(options: {
    code: string;
    message: string;
    status: number;
  }) {
    super(options.message);
    this.code = options.code;
    this.status = options.status;
  }
}

const {
  validateActiveTasksResponse,
  validateAuthUser,
  validateByokSettings,
  validateMemorySettings,
  validateShare,
  validateSystemSettings,
  validateUploadedImage,
} = compile("./responseValidators.ts", {
  "./errors": { ApiError: TestApiError },
}) as typeof import("./responseValidators");

function rejectsSchema(
  validate: (value: unknown) => unknown,
  value: unknown,
): void {
  assert.throws(
    () => validate(value),
    (error: unknown) =>
      error instanceof TestApiError &&
      error.code === "response_schema_error" &&
      error.status === 502,
  );
}

test("auth and active-task validators reject valid JSON with wrong fields", () => {
  rejectsSchema(validateAuthUser, { id: 42 });
  rejectsSchema(validateAuthUser, {
    id: "user-1",
    runtime_defaults: {
      agent_enabled: "yes",
      nav_visibility: { agent: true },
    },
  });
  rejectsSchema(validateActiveTasksResponse, {
    generations: {},
    completions: [],
  });
  rejectsSchema(validateActiveTasksResponse, {
    generations: [
      {
        id: "generation-1",
        message_id: "message-1",
        action: "generate",
        prompt: "prompt",
        size_requested: "1024x1024",
        aspect_ratio: "1:1",
        input_image_ids: [],
        primary_input_image_id: null,
        status: "not-a-status",
        progress_stage: "queued",
        attempt: 0,
        error_code: null,
        error_message: null,
        started_at: null,
        finished_at: null,
      },
    ],
    completions: [],
  });
});

test("settings validators reject missing and mistyped required fields", () => {
  rejectsSchema(validateSystemSettings, {
    items: [
      {
        key: "canvas.enabled",
        value: "1",
        has_value: "yes",
        is_sensitive: false,
        description: "Canvas",
      },
    ],
  });
  rejectsSchema(validateMemorySettings, {
    paused: false,
    disabled: false,
    extraction_threshold: 0.7,
    onboarding_seen: 1,
    confirmation_enabled: false,
    embedding_available: "yes",
  });
  rejectsSchema(validateByokSettings, {
    mode_enabled: false,
    byok_signup_enabled: false,
  });
});

test("share and upload validators reject incomplete successful payloads", () => {
  rejectsSchema(validateShare, {
    id: "share-1",
    image_id: "image-1",
    image_ids: ["image-1"],
    token: "token",
    url: "/share/token",
    image_url: "/share/token/image",
    show_prompt: false,
    expires_at: null,
    revoked_at: null,
  });
  rejectsSchema(validateUploadedImage, {
    id: "image-1",
    width: "1024",
    height: 1024,
    url: "/images/image-1",
  });
});

test("critical validators preserve valid server payloads without defaults", () => {
  const auth = {
    id: "user-1",
    runtime_defaults: {
      agent_enabled: true,
      nav_visibility: { agent: true },
    },
  };
  const active = {
    generations: [],
    completions: [
      {
        id: "completion-1",
        message_id: "message-1",
        model: "",
        input_image_ids: [],
        text: "",
        tokens_in: 0,
        tokens_out: 0,
        status: "streaming",
        progress_stage: "",
        attempt: 0,
        error_code: null,
        error_message: null,
        started_at: null,
        finished_at: null,
      },
    ],
  };
  const memory = {
    paused: false,
    disabled: false,
    extraction_threshold: 0.7,
    onboarding_seen: 1,
    confirmation_enabled: true,
    embedding_available: true,
  };
  const byok = {
    mode_enabled: true,
    byok_signup_enabled: false,
    byok_signup_bypasses_allowlist: false,
    fallback_to_admin_provider: false,
    validation_model: "gpt-5.4",
    validation_timeout_ms: 15_000,
    pending_token_ttl_seconds: 900,
    retention_hide_enabled: true,
    retention_delete_enabled: false,
    retention_hide_days: 3,
    retention_delete_days: 7,
  };
  const system = {
    items: [
      {
        key: "canvas.enabled",
        value: "1",
        has_value: true,
        is_sensitive: false,
        description: "Canvas",
      },
    ],
  };
  const share = {
    id: "share-1",
    image_id: "image-1",
    image_ids: ["image-1"],
    token: "token",
    url: "/share/token",
    image_url: "/share/token/image",
    show_prompt: false,
    expires_at: null,
    revoked_at: null,
    created_at: "2026-08-06T00:00:00Z",
  };
  const upload = {
    id: "image-1",
    width: 1024,
    height: 1024,
    url: "/images/image-1",
  };

  assert.equal(validateAuthUser(auth), auth);
  assert.equal(validateActiveTasksResponse(active), active);
  assert.equal(validateMemorySettings(memory), memory);
  assert.equal(validateByokSettings(byok), byok);
  assert.equal(validateSystemSettings(system), system);
  assert.equal(validateShare(share), share);
  assert.equal(validateUploadedImage(upload), upload);
  assert.deepEqual(auth, {
    id: "user-1",
    runtime_defaults: {
      agent_enabled: true,
      nav_visibility: { agent: true },
    },
  });
  assert.deepEqual(upload, {
    id: "image-1",
    width: 1024,
    height: 1024,
    url: "/images/image-1",
  });
});
