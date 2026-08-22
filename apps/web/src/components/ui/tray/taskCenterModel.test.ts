import assert from "node:assert/strict";
import test from "node:test";
import "../../../store/chat/moduleResolution.test-helper.mjs";

const { resolveGenerationRoute, resolveTaskRoute } = await import(
  new URL("./taskCenterModel.ts", import.meta.url).href
);

test("task route resolver returns Agent assistant messages to their session", () => {
  assert.equal(
    resolveTaskRoute({
      source: "agent",
      agent_session_id: "session one",
      conversation_id: "hidden-conversation",
      message_id: "assistant/1",
    }),
    "/agent?session=session+one&scrollTo=assistant%2F1",
  );
});

test("task route resolver preserves Studio routes and rejects incomplete rows", () => {
  assert.equal(
    resolveTaskRoute({
      source: "chat",
      agent_session_id: null,
      conversation_id: "conversation-1",
      message_id: "message-1",
    }),
    "/?conversationId=conversation-1&scrollTo=message-1",
  );
  assert.equal(
    resolveGenerationRoute({
      source: "chat",
      conversation_id: "conversation-2",
      message_id: "message-2",
    }),
    "/?conversationId=conversation-2&scrollTo=message-2",
  );
  assert.equal(
    resolveTaskRoute({
      source: "agent",
      agent_session_id: null,
      conversation_id: null,
      message_id: "message-3",
    }),
    null,
  );
});
