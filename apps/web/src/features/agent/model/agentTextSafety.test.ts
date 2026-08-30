import assert from "node:assert/strict";
import test from "node:test";

import { neutralizeAgentPseudoProtocol } from "./agentTextSafety.ts";


test("legacy pseudo protocol is neutralized only outside Markdown code", () => {
  const source = [
    "Normal <tool_call> marker.",
    "",
    "`<function=exec_command>`",
    "",
    "```xml",
    "<tool_call>{\"name\":\"bash\"}</tool_call>",
    "```",
    "",
    "<section>ordinary HTML</section>",
  ].join("\n");

  const projected = neutralizeAgentPseudoProtocol(source);

  assert.match(projected, /Normal \u2039tool_call\u203a marker\./u);
  assert.match(projected, /`<function=exec_command>`/u);
  assert.match(
    projected,
    /```xml\n<tool_call>\{"name":"bash"\}<\/tool_call>\n```/u,
  );
  assert.match(projected, /<section>ordinary HTML<\/section>/u);
});

test("ordinary Markdown remains byte-identical", () => {
  const source = "# Heading\n\nA [link](https://example.com) and <section>HTML</section>.";
  assert.equal(neutralizeAgentPseudoProtocol(source), source);
});
