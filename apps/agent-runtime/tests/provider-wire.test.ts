import { once } from "node:events";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";

import { CollectingEventWriter } from "../src/ndjson.js";
import { executeAgentRun } from "../src/runtime.js";
import { runtimeRequestV3 } from "./fixtures.js";

interface Capture {
  path: string;
  body: Record<string, unknown>;
  headers: Record<string, string | string[] | undefined>;
}

function sse(api: string, includeUsage = true): string {
  if (api === "openai-responses") {
    const item = {
      id: "msg-wire",
      type: "message",
      role: "assistant",
      status: "completed",
      content: [{ type: "output_text", text: "wire ok", annotations: [] }],
    };
    return [
      `data: ${JSON.stringify({ type: "response.created", response: { id: "resp-wire" } })}`,
      "",
      `data: ${JSON.stringify({ type: "response.output_item.added", output_index: 0, item: { ...item, content: [] } })}`,
      "",
      `data: ${JSON.stringify({ type: "response.output_text.delta", output_index: 0, content_index: 0, delta: "wire ok" })}`,
      "",
      `data: ${JSON.stringify({ type: "response.output_item.done", output_index: 0, item })}`,
      "",
      `data: ${JSON.stringify({
        type: "response.completed",
        response: {
          id: "resp-wire",
          status: "completed",
          model: "configured-wire-model",
          output: [item],
          ...(includeUsage
            ? { usage: { input_tokens: 1, output_tokens: 2, total_tokens: 3 } }
            : {}),
        },
      })}`,
      "",
      "",
    ].join("\n");
  }
  if (api === "openai-completions") {
    const chunk = (delta: Record<string, unknown>, finishReason: string | null) => ({
      id: "chatcmpl-wire",
      object: "chat.completion.chunk",
      created: 1,
      model: "configured-wire-model",
      choices: [{ index: 0, delta, finish_reason: finishReason }],
    });
    return [
      `data: ${JSON.stringify(chunk({ role: "assistant" }, null))}`,
      "",
      `data: ${JSON.stringify(chunk({ content: "wire ok" }, null))}`,
      "",
      `data: ${JSON.stringify(chunk({}, "stop"))}`,
      "",
      ...(includeUsage
        ? [
            `data: ${JSON.stringify({
              id: "chatcmpl-wire",
              object: "chat.completion.chunk",
              created: 1,
              model: "configured-wire-model",
              choices: [],
              usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 },
            })}`,
            "",
          ]
        : []),
      "data: [DONE]",
      "",
    ].join("\n");
  }
  return [
    `event: message_start\ndata: ${JSON.stringify({
      type: "message_start",
      message: {
        id: "msg-wire",
        type: "message",
        role: "assistant",
        model: "configured-wire-model",
        content: [],
        stop_reason: null,
        stop_sequence: null,
        ...(includeUsage ? { usage: { input_tokens: 1, output_tokens: 0 } } : {}),
      },
    })}`,
    "",
    `event: content_block_start\ndata: ${JSON.stringify({
      type: "content_block_start",
      index: 0,
      content_block: { type: "text", text: "" },
    })}`,
    "",
    `event: content_block_delta\ndata: ${JSON.stringify({
      type: "content_block_delta",
      index: 0,
      delta: { type: "text_delta", text: "wire ok" },
    })}`,
    "",
    `event: content_block_stop\ndata: ${JSON.stringify({ type: "content_block_stop", index: 0 })}`,
    "",
    `event: message_delta\ndata: ${JSON.stringify({
      type: "message_delta",
      delta: { stop_reason: "end_turn", stop_sequence: null },
      ...(includeUsage ? { usage: { output_tokens: 2 } } : {}),
    })}`,
    "",
    `event: message_stop\ndata: ${JSON.stringify({ type: "message_stop" })}`,
    "",
  ].join("\n");
}

describe("production provider wire adapters", () => {
  for (const [api, suffix] of [
    ["openai-responses", "/responses"],
    ["openai-completions", "/chat/completions"],
    ["anthropic-messages", "/v1/messages"],
  ] as const) {
    it(`uses the configured ${api} SDK base, model, prompt, and terminal SSE`, async () => {
      const captures: Capture[] = [];
      const server = createServer((request, response) => {
        const chunks: Buffer[] = [];
        request.on("data", (chunk: Buffer) => chunks.push(chunk));
        request.on("end", () => {
          captures.push({
            path: request.url ?? "",
            body: JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>,
            headers: request.headers,
          });
          response.writeHead(200, {
            "content-type": "text/event-stream",
            connection: "close",
          });
          response.end(sse(api));
        });
      });
      server.listen(0, "127.0.0.1");
      await once(server, "listening");
      const address = server.address() as AddressInfo;
      const systemPrompt = "Exact Lumen wire prompt. Current working directory: user literal.";
      const run = async (reasoningEffort: null | "off" | "high") => {
        const request = runtimeRequestV3({
          allowed_tools: [],
          tool_gateway_url: null,
          tool_capability: null,
          reasoning_effort: reasoningEffort,
          system_prompt: systemPrompt,
          history: [
            {
              message_id: "history-assistant",
              role: "assistant",
              text: "Before tools.",
              blocks: [
                { type: "assistant_text", turn: 1, text: "Before tools." },
                {
                  type: "tool_call",
                  turn: 1,
                  id: "history-call-1",
                  name: "lumen_create_image",
                  arguments: { prompt: "first" },
                },
                {
                  type: "tool_result",
                  turn: 1,
                  tool_call_id: "history-call-1",
                  name: "lumen_create_image",
                  text: '{"status":"accepted","generation_ids":["generation-1"]}',
                  is_error: false,
                },
                {
                  type: "tool_call",
                  turn: 1,
                  id: "history-call-2",
                  name: "lumen_create_image",
                  arguments: { prompt: "second" },
                },
                {
                  type: "tool_result",
                  turn: 1,
                  tool_call_id: "history-call-2",
                  name: "lumen_create_image",
                  text: '{"status":"accepted","generation_ids":["generation-2"]}',
                  is_error: false,
                },
              ],
            },
          ],
          provider: {
            ...runtimeRequestV3().provider,
            api,
            base_url: `http://127.0.0.1:${String(address.port)}`,
            model: "configured-wire-model",
            reasoning_supported: true,
            thinking_level_map: { off: "none", high: "high" },
          },
        });
        const writer = new CollectingEventWriter(
          request.run_id,
          request.execution_epoch,
        );
        const result = await executeAgentRun(
          request,
          writer,
          new AbortController().signal,
        );
        return { result, writer };
      };
      try {
        const { result, writer } = await run(null);
        expect(result).toMatchObject({
          outcome: "succeeded",
          providerDispatchCount: 1,
          providerCompletedCount: 1,
        });
        expect(result.usage.total_tokens).toBe(3);
        expect(
          writer.events
            .filter((event) => event.type === "text.delta")
            .map((event) => (typeof event.delta === "string" ? event.delta : ""))
            .join(""),
        ).toBe("wire ok");
        await run("off");
        await run("high");
        expect(captures).toHaveLength(3);
        expect(captures.every((capture) => capture.path === suffix)).toBe(true);
        expect(captures[0]?.path).toBe(suffix);
        expect(captures[0]?.body.model).toBe("configured-wire-model");
        expect(captures[0]?.body.stream).toBe(true);
        const serialized = JSON.stringify(captures[0]?.body);
        expect(serialized).toContain(systemPrompt);
        expect(serialized).not.toContain("/tmp/lumen-agent-runtime");
        expect(serialized).not.toContain('"reasoning"');
        expect(serialized).not.toContain('"reasoning_effort"');
        expect(serialized).not.toContain('"thinking"');
        const offBody = captures[1]?.body ?? {};
        const highBody = captures[2]?.body ?? {};
        if (api !== "anthropic-messages") {
          expect(JSON.stringify(captures[0]?.body)).toContain('"role":"developer"');
          expect(JSON.stringify(offBody)).toContain('"role":"developer"');
        }
        const autoBody = captures[0]?.body ?? {};
        if (api === "openai-responses") {
          const input = autoBody.input as Array<Record<string, unknown>>;
          const firstCall = input.findIndex((value) =>
            JSON.stringify(value).includes("history-call-1"),
          );
          const secondCall = input.findIndex((value) =>
            JSON.stringify(value).includes("history-call-2"),
          );
          const firstResult = input.findIndex((value) =>
            value.type === "function_call_output" &&
            value.call_id === "history-call-1",
          );
          expect(firstCall).toBeGreaterThanOrEqual(0);
          expect(secondCall).toBeGreaterThan(firstCall);
          expect(firstResult).toBeGreaterThan(secondCall);
        } else if (api === "openai-completions") {
          const messages = autoBody.messages as Array<Record<string, unknown>>;
          const assistant = messages.find((value) => value.role === "assistant");
          expect(assistant?.tool_calls).toHaveLength(2);
          expect(messages.filter((value) => value.role === "tool")).toHaveLength(2);
        } else {
          const messages = autoBody.messages as Array<Record<string, unknown>>;
          const assistant = messages.find((value) => value.role === "assistant");
          const resultMessage = messages.find((value) =>
            Array.isArray(value.content) &&
            value.content.some((part) =>
              JSON.stringify(part).includes("tool_result"),
            ),
          );
          expect(
            (assistant?.content as Array<Record<string, unknown>>).filter(
              (value) => value.type === "tool_use",
            ),
          ).toHaveLength(2);
          expect(
            (resultMessage?.content as Array<Record<string, unknown>>).filter(
              (value) => value.type === "tool_result",
            ),
          ).toHaveLength(2);
        }
        if (api === "openai-responses") {
          expect(offBody.reasoning).toMatchObject({ effort: "none" });
          expect(highBody.reasoning).toMatchObject({ effort: "high" });
        } else if (api === "openai-completions") {
          expect(offBody.reasoning_effort).toBe("none");
          expect(highBody.reasoning_effort).toBe("high");
        } else {
          expect(offBody.thinking).toMatchObject({ type: "disabled" });
          expect(highBody.thinking).toMatchObject({ type: "enabled" });
        }
      } finally {
        server.close();
        await once(server, "close");
      }
    }, 15_000);
  }

  it.each([
    ["openai-responses", "/responses"],
    ["openai-completions", "/chat/completions"],
    ["anthropic-messages", "/v1/messages"],
  ] as const)(
    "keeps terminal %s usage unknown when the provider omits its receipt",
    async (api, suffix) => {
      const captures: Capture[] = [];
      const server = createServer((request, response) => {
        const chunks: Buffer[] = [];
        request.on("data", (chunk: Buffer) => chunks.push(chunk));
        request.on("end", () => {
          captures.push({
            path: request.url ?? "",
            body: JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<
              string,
              unknown
            >,
            headers: request.headers,
          });
          response.writeHead(200, {
            "content-type": "text/event-stream",
            connection: "close",
          });
          response.end(sse(api, false));
        });
      });
      server.listen(0, "127.0.0.1");
      await once(server, "listening");
      const address = server.address() as AddressInfo;
      const request = runtimeRequestV3({
        allowed_tools: [],
        tool_gateway_url: null,
        tool_capability: null,
        reasoning_effort: null,
        provider: {
          ...runtimeRequestV3().provider,
          api,
          base_url: `http://127.0.0.1:${String(address.port)}`,
          model: "configured-wire-model",
        },
      });
      try {
        const result = await executeAgentRun(
          request,
          new CollectingEventWriter(request.run_id, request.execution_epoch),
          new AbortController().signal,
        );
        expect(result).toMatchObject({
          outcome: api === "anthropic-messages" ? "failed" : "succeeded",
          providerDispatchCount: 1,
          providerCompletedCount: api === "anthropic-messages" ? 0 : 1,
          exactUsageCallCount: 0,
          usageEvidence: "unknown",
        });
        expect(result.usage.total_tokens).toBe(0);
        expect(captures[0]?.path).toBe(suffix);
      } finally {
        server.close();
        await once(server, "close");
      }
    },
    15_000,
  );
});
