// GPT-6 Responses contract: https://developers.openai.com/api/docs/guides/latest-model
// Model identification must not alter the exact upstream ID or enable a provider.
export function gpt6ModelFamily(modelId: string): "astra" | "sol" | "luna" | null {
  const canonical = modelId.trim().toLowerCase().split(/[/:]/u).at(-1) ?? "";
  const match = /^gpt-6-(astra|sol|luna)(?:-\d{4}-\d{2}-\d{2})?$/u.exec(canonical);
  return (match?.[1] as "astra" | "sol" | "luna" | undefined) ?? null;
}

export function normalizeGpt6Payload(modelId: string, payload: unknown): unknown {
  const family = gpt6ModelFamily(modelId);
  if (!family || payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return payload;
  }
  const output = { ...(payload as Record<string, unknown>) };
  const raw = output.reasoning;
  const reasoning = raw !== null && typeof raw === "object" && !Array.isArray(raw)
    ? raw as Record<string, unknown> : null;
  const effort = reasoning?.effort ?? output.reasoning_effort;
  const effective = effort === "minimal" || (family === "astra" && (effort === "none" || effort === "off"))
    ? "low" : effort;
  if (effective !== effort) {
    if (reasoning) output.reasoning = { ...reasoning, effort: effective };
    if ("reasoning_effort" in output) output.reasoning_effort = effective;
  }
  if (effective !== "none") {
    delete output.temperature;
    delete output.top_p;
    delete output.top_logprobs;
    delete output.logprobs;
    if (Array.isArray(output.include)) {
      output.include = output.include.filter((value) => value !== "message.output_text.logprobs");
    }
  }
  return output;
}
