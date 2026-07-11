import assert from "node:assert/strict";
import test from "node:test";
import type { ReasoningEffort as PublicReasoningEffort } from "../useChatStore";
import type {
  ChatState,
  ComposerState,
  ReasoningEffort,
} from "./types";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends
  <Value>() => Value extends Right ? 1 : 2
    ? true
    : false;

const reasoningEffortCompatibility: Equal<
  ReasoningEffort,
  PublicReasoningEffort
> = true;

test("chat types preserve the public reasoning effort contract", () => {
  const composerKey: keyof ComposerState = "reasoningEffort";
  const actionKey: keyof ChatState = "sendMessage";

  assert.equal(reasoningEffortCompatibility, true);
  assert.equal(composerKey, "reasoningEffort");
  assert.equal(actionKey, "sendMessage");
});
