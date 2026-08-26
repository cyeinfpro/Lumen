import { expect, type Page, type Route } from "@playwright/test";

const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==",
  "base64",
);
const NOW = "2026-08-20T08:00:00Z";

export type AgentFixtureMode =
  | "text"
  | "image"
  | "active-image"
  | "partial-image"
  | "cancel"
  | "error";

export interface AgentFixtureOptions {
  mode?: AgentFixtureMode;
  errorCode?: string;
  userId?: string;
  archived?: boolean;
  omitSessionFromList?: boolean;
  sessionCount?: number;
  generationCount?: number;
}

function imageDefaults() {
  return {
    count: 1,
    aspect_ratio: "1:1",
    quality: "2k",
    render_quality: "high",
    background: "auto",
    output_format: "webp",
  };
}

function tool(status: "running" | "succeeded" = "succeeded") {
  return {
    id: "tool-1",
    agent_run_id: "run-1",
    ordinal: 0,
    name: "lumen_create_image",
    mode: "text_to_image",
    status,
    generation_ids: ["generation-1"],
    generation_count: 1,
    error_code: null,
    started_at: NOW,
    finished_at: status === "succeeded" ? NOW : null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function run(
  status: "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelled",
  withTool = false,
  sessionId = "session-1",
) {
  const suffix = sessionId.replace(/^session-/u, "") || "1";
  return {
    id: `run-${suffix}`,
    agent_session_id: sessionId,
    user_message_id: `user-${suffix}`,
    assistant_message_id: `assistant-${suffix}`,
    status,
    execution_epoch: 1,
    last_event_seq: status === "running" ? 3 : 8,
    idempotency_key: "fixture-message-1",
    model: "fixture-model",
    reasoning_effort: null,
    turn_count: 1,
    tool_call_count: withTool ? 1 : 0,
    usage: {},
    error_code: status === "partial" ? "agent_runtime_unavailable" : null,
    error_message:
      status === "partial" ? "Agent runtime is unavailable" : null,
    continuable: status === "partial" || status === "failed",
    started_at: NOW,
    finished_at: status === "running" || status === "queued" ? null : NOW,
    cancel_requested_at: null,
    created_at: NOW,
    updated_at: NOW,
    references: [],
    tool_calls: withTool ? [tool(status === "running" ? "running" : "succeeded")] : [],
  };
}

function messagePair(
  text: string,
  status: ReturnType<typeof run>["status"],
  withTool = false,
  sessionId = "session-1",
) {
  const suffix = sessionId.replace(/^session-/u, "") || "1";
  return [
    {
      id: `user-${suffix}`,
      conversation_id: `conversation-${suffix}`,
      role: "user",
      content: { source: "agent", text: "创建产品视觉" },
      intent: "agent",
      status: null,
      parent_message_id: null,
      created_at: NOW,
    },
    {
      id: `assistant-${suffix}`,
      conversation_id: `conversation-${suffix}`,
      role: "assistant",
      content: {
        source: "agent",
        agent_run_id: `run-${suffix}`,
        text,
        tool_calls: withTool
          ? [
              {
                id: "tool-1",
                name: "lumen_create_image",
                label: "生成图片",
                mode: "text_to_image",
                status: status === "running" ? "running" : "succeeded",
                generation_ids: ["generation-1"],
                generation_count: 1,
              },
            ]
          : [],
        generation_ids: withTool ? ["generation-1"] : [],
      },
      intent: "agent",
      status,
      parent_message_id: `user-${suffix}`,
      created_at: NOW,
    },
  ];
}

function generation(
  status: "running" | "succeeded",
  index = 1,
  sessionId = "session-1",
) {
  const suffix = sessionId.replace(/^session-/u, "") || "1";
  return {
    id: `generation-${index}`,
    message_id: `assistant-${suffix}`,
    agent_session_id: sessionId,
    agent_run_id: `run-${suffix}`,
    agent_tool_call_id: "tool-1",
    action: "generate",
    prompt: "明亮产品主图",
    size_requested: "2048x2048",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status,
    progress_stage: status === "running" ? "rendering" : "finalizing",
    attempt: 1,
    error_code: null,
    error_message: null,
    started_at: NOW,
    finished_at: status === "succeeded" ? NOW : null,
    source: "agent",
    action_source: "agent.create_image",
  };
}

function image() {
  return {
    id: "image-1",
    source: "generated",
    parent_image_id: null,
    owner_generation_id: "generation-1",
    width: 4,
    height: 4,
    mime: "image/png",
    blurhash: null,
    url: "/api/images/image-1/binary",
    display_url: "/api/images/image-1/binary",
    preview_url: "/api/images/image-1/binary",
    thumb_url: "/api/images/image-1/binary",
    metadata_jsonb: {},
  };
}

function studioConversation() {
  return {
    id: "studio-conversation-1",
    title: "Fixture conversation",
    pinned: false,
    archived: false,
    memory_disabled: false,
    active_scope_id: null,
    last_activity_at: NOW,
    default_params: {},
    default_system: null,
    default_system_prompt_id: null,
    created_at: NOW,
  };
}

function session(
  activeRun: ReturnType<typeof run> | null = null,
  archived = false,
  sessionId = "session-1",
) {
  const suffix = sessionId.replace(/^session-/u, "") || "1";
  return {
    id: sessionId,
    conversation_id: `conversation-${suffix}`,
    title: suffix === "1" ? "产品视觉" : `产品视觉 ${suffix}`,
    pinned: false,
    archived,
    memory_disabled: false,
    active_scope_id: null,
    default_system: null,
    default_system_prompt_id: null,
    image_defaults: imageDefaults(),
    allow_image: true,
    runtime_version: "0.84.2",
    last_activity_at: NOW,
    created_at: NOW,
    updated_at: NOW,
    active_run: activeRun,
  };
}

function json(route: Route, payload: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

export async function installAgentFixture(
  page: Page,
  options: AgentFixtureOptions = {},
) {
  const mode = options.mode ?? "text";
  const userId = options.userId ?? "user-a";
  const sessionIds = Array.from(
    { length: Math.max(1, options.sessionCount ?? 1) },
    (_value, index) => `session-${index + 1}`,
  );
  let currentRun: ReturnType<typeof run> | null = null;
  let messages: ReturnType<typeof messagePair> = [];
  let generations: ReturnType<typeof generation>[] = [];
  let images: ReturnType<typeof image>[] = [];
  let lastMessageBody: Record<string, unknown> | null = null;
  let cancelCalls = 0;
  let snapshotCalls = 0;
  let continuationCalls = 0;
  let lastContinuationBody: Record<string, unknown> | null = null;

  if (mode === "active-image" || mode === "cancel") {
    currentRun = run("running", true);
    messages = messagePair("图片任务已提交。", "running", true);
    generations = Array.from(
      { length: Math.max(1, options.generationCount ?? 1) },
      (_value, index) => generation("running", index + 1),
    );
  }
  if (mode === "partial-image") {
    messages = messagePair("图片已提交，但最终回复中断。", "partial", true);
    generations = [generation("succeeded")];
    images = [image()];
  }

  const feedItems = [1, 2].map((index) => ({
    id: `feed-generation-${index}`,
    created_at: NOW,
    prompt: `素材参考 ${index}`,
    aspect_ratio: "1:1",
    has_ref: false,
    size_actual: "4x4",
    image: {
      id: `feed-image-${index}`,
      url: `/api/images/feed-image-${index}/binary`,
      mime: "image/png",
      display_url: `/api/images/feed-image-${index}/binary`,
      preview_url: `/api/images/feed-image-${index}/binary`,
      thumb_url: `/api/images/feed-image-${index}/binary`,
      width: 4,
      height: 4,
    },
    message_id: `feed-message-${index}`,
    conversation_id: `feed-conversation-${index}`,
  }));

  await page.context().addCookies([
    {
      name: "lumen_runtime_defaults_v1",
      value: encodeURIComponent(
        JSON.stringify({
          agent_enabled: true,
          nav_visibility: {
            studio: true,
            agent: true,
            video: true,
            projects: true,
            assets: true,
          },
        }),
      ),
      domain: "127.0.0.1",
      path: "/",
    },
    { name: "csrf", value: "fixture-csrf", domain: "127.0.0.1", path: "/" },
  ]);

  await page.route("**/events?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: "event: heartbeat\ndata: {\"schema_version\":1}\n\n",
    }),
  );
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (path === "/api/events") {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "event: heartbeat\ndata: {\"schema_version\":1}\n\n",
      });
    }
    if (path.includes("/images/") && path.endsWith("/binary")) {
      return route.fulfill({ status: 200, contentType: "image/png", body: PNG });
    }
    if (path === "/api/auth/me") {
      return json(route, {
        id: userId,
        email: `${userId}@example.com`,
        role: "member",
        account_mode: "wallet",
        runtime_defaults: {
          fast: true,
          canvas_enabled: false,
          agent_enabled: true,
          nav_visibility: {
            studio: true,
            agent: true,
            video: true,
            projects: true,
            assets: true,
          },
        },
      });
    }
    if (path === "/api/auth/csrf") return json(route, { csrf_token: "fixture-csrf" });
    if (path === "/api/conversations" && method === "GET") {
      return json(route, { items: [studioConversation()], next_cursor: null });
    }
    if (path === "/api/conversations/studio-conversation-1" && method === "GET") {
      return json(route, studioConversation());
    }
    if (
      path === "/api/conversations/studio-conversation-1/messages" &&
      method === "GET"
    ) {
      return json(route, {
        items: [],
        generations: [],
        completions: [],
        images: [],
        next_cursor: null,
      });
    }
    if (path === "/api/agent/status") {
      return json(route, { enabled: true, tool_gateway_configured: true });
    }
    if (path === "/api/system-prompts") return json(route, { items: [], default_id: null });
    if (path === "/api/generations/feed") {
      return json(route, { items: feedItems, next_cursor: null, total: feedItems.length });
    }
    if (path === "/api/agent/sessions" && method === "GET") {
      return json(route, {
        items: options.omitSessionFromList
          ? []
          : sessionIds.map((sessionId) =>
              session(
                sessionId === "session-1" ? currentRun : null,
                options.archived,
                sessionId,
              ),
            ),
        next_cursor: null,
      });
    }
    if (path === "/api/agent/sessions" && method === "POST") {
      return json(route, session(null, options.archived));
    }
    const sessionMatch = path.match(/^\/api\/agent\/sessions\/(session-\d+)$/u);
    if (sessionMatch && method === "GET") {
      const sessionId = sessionMatch[1];
      return json(
        route,
        session(
          sessionId === "session-1" ? currentRun : null,
          options.archived,
          sessionId,
        ),
      );
    }
    if (sessionMatch && method === "PATCH") {
      const sessionId = sessionMatch[1];
      const patch = request.postDataJSON() as Record<string, unknown>;
      return json(route, {
        ...session(
          sessionId === "session-1" ? currentRun : null,
          options.archived,
          sessionId,
        ),
        ...patch,
      });
    }
    if (sessionMatch && method === "DELETE") {
      return json(route, { ok: true });
    }
    if (path.endsWith("/active-run")) {
      snapshotCalls += 1;
      return json(route, path.includes("/session-1/") ? currentRun : null);
    }
    if (path.endsWith("/messages") && method === "GET") {
      snapshotCalls += 1;
      if (!path.includes("/session-1/")) {
        return json(route, {
          items: [],
          runs: [],
          next_cursor: null,
          generations: [],
          completions: [],
          images: [],
        });
      }
      return json(route, {
        items: messages,
        runs: messages.length ? [currentRun ?? run(mode === "partial-image" ? "partial" : "succeeded", generations.length > 0)] : [],
        next_cursor: null,
        generations,
        completions: [],
        images,
      });
    }
    if (path.endsWith("/messages") && method === "POST") {
      lastMessageBody = request.postDataJSON() as Record<string, unknown>;
      if (mode === "error") {
        const code = options.errorCode ?? "INSUFFICIENT_BALANCE";
        return json(route, { error: { code, message: code } }, code === "INSUFFICIENT_BALANCE" ? 402 : 503);
      }
      const queuedRun = run("queued", false);
      const queuedMessages = messagePair("", "queued", false);
      if (mode === "image") {
        messages = messagePair("图片任务已提交。", "succeeded", true);
        generations = [generation("succeeded")];
        images = [image()];
      } else {
        messages = messagePair("已完成产品视觉方向。", "succeeded", false);
      }
      currentRun = null;
      return json(route, {
        user_message: queuedMessages[0],
        assistant_message: queuedMessages[1],
        agent_run: queuedRun,
      });
    }
    if (path === "/api/agent/runs/run-1/cancel" && method === "POST") {
      cancelCalls += 1;
      currentRun = null;
      messages = messagePair("图片任务已提交。", "cancelled", true);
      return json(route, run("cancelled", true));
    }
    if (path === "/api/agent/runs/run-1/continue" && method === "POST") {
      continuationCalls += 1;
      lastContinuationBody = request.postDataJSON() as Record<string, unknown>;
      return json(route, {
        ...run("queued", false),
        id: "run-continue",
        user_message_id: "user-continue",
        assistant_message_id: "assistant-continue",
        idempotency_key: lastContinuationBody.idempotency_key,
        continuable: false,
      });
    }
    if (path === "/api/tasks" || path === "/api/tasks/mine/active") {
      const task = {
        kind: "generation",
        id: "generation-1",
        message_id: "assistant-1",
        status: generations[0]?.status ?? "succeeded",
        progress_stage: generations[0]?.progress_stage ?? "finalizing",
        started_at: NOW,
        source: "agent",
        agent_session_id: "session-1",
        agent_run_id: "run-1",
        conversation_id: "conversation-1",
        title: "Agent 图片",
      };
      if (path.endsWith("/mine/active")) {
        return json(route, { generations: generations.filter((item) => item.status === "running"), completions: [] });
      }
      return json(route, { items: generations.length ? [task] : [], next_cursor: null });
    }
    return json(route, {});
  });

  return {
    get lastMessageBody() {
      return lastMessageBody;
    },
    get cancelCalls() {
      return cancelCalls;
    },
    get snapshotCalls() {
      return snapshotCalls;
    },
    get continuationCalls() {
      return continuationCalls;
    },
    get lastContinuationBody() {
      return lastContinuationBody;
    },
  };
}

export async function openAgent(page: Page) {
  await page.goto("/agent?session=session-1");
  await expect(page.locator("[data-agent-workspace]")).toBeVisible();
}

export async function assertImagePixels(page: Page, selector: string) {
  const pixels = await page.locator(selector).evaluate((node) => {
    const image = node as HTMLImageElement;
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d");
    if (!context || canvas.width === 0 || canvas.height === 0) return 0;
    context.drawImage(image, 0, 0);
    return Array.from(context.getImageData(0, 0, canvas.width, canvas.height).data)
      .filter((value, index) => index % 4 !== 3 && value !== 0).length;
  });
  expect(pixels).toBeGreaterThan(0);
}
