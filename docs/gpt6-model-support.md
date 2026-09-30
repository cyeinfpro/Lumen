# GPT-6 model support

## Configuration and availability

Lumen recognizes `gpt-6-astra`, `gpt-6-sol`, and `gpt-6-luna`, including gateway-prefixed IDs and dated snapshots. Recognition does not enable a provider, grant upstream access, or change the existing default chat model. Existing API keys, endpoint URLs, model allowlists, and operator-provided capability metadata remain authoritative.

Use the provider administration model discovery flow to apply a profile and explicitly select the desired default or allowed Agent models. GPT-6 Agent providers must use `openai-responses`; unsupported Agent API combinations are excluded from wallet model choices and rejected before dispatch. BYOK connections use the same protocol guard. Conservative gateway context/output budgets remain in place until the operator supplies verified provider metadata.

Astra is a text/vision reasoning model. Image generation still uses Lumen's separately configured image tools and image providers; this change does not register Astra as an image-generation model.

## Compatibility

Astra supports low, medium, high, xhigh, and max. Old explicit none/off/minimal settings migrate to low. Sol/Luna retain none, and minimal migrates to low. Auto stays omitted rather than being rewritten as off. Reasoning-enabled GPT-6 requests omit incompatible sampling and log-probability controls. Custom/older models retain their previous behavior.

The Agent UI exposes model-aware effort options, quick low/medium/high presets, the effective model/effort in the composer summary, and actual tool phases, turn/tool counts, and elapsed execution time. Timers do not announce every second through the status live region. Completion/cancellation and uncertain-submission states take precedence over tool progress.

## Prompt enhancement

Desktop and mobile composer execution settings have a separate **提示词优化模型** selector. Default retains the original model/failover policy. Choosing a GPT-6 model uses that exact model with low reasoning and standard service tier; provider failover must not silently change it to a different model. Candidates do not overwrite the input until explicitly applied.

`POST /prompts/enhance` and `POST /prompts/video/enhance` accept optional `enhancement_model` (`gpt-6-astra`, `gpt-6-sol`, or `gpt-6-luna`). The video's existing `model` field still identifies the video-generation model, not the enhancer. Older requests omit the new field from idempotency fingerprints; explicit choices participate in the fingerprint, billing snapshot selection, and durable stream dispatch. Only the chosen policy's pricing snapshots are prepared, so default requests do not depend on new model pricing being enabled. When the upstream omits usage or its response model, settlement derives the explicit GPT-6 selection from the frozen single-model snapshot rather than the current default. Existing holds, failure recovery, and no-fail-open settlement behavior are preserved.

## Pricing and validation boundary

The standard-price catalog and fallback pricing contain the verified GPT-6 rates. Long-context fallback settings use a 272,000-token threshold, 2x input multiplier, and 1.5x output multiplier. Operator pricing rules and existing persisted billing snapshots are not rewritten.

Official contract references checked on 2026-09-29:
- https://developers.openai.com/api/docs/guides/latest-model
- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://developers.openai.com/api/docs/models/gpt-6-sol
- https://developers.openai.com/api/docs/models/gpt-6-luna

Regression coverage includes Python capability/pricing/prompt routing tests, the real Agent SDK adapter against a local mock Responses server, frontend pure-function tests, and a fixture-driven Agent browser flow. These do not constitute a paid live-model probe or prove that a particular gateway account has GPT-6 access. Deployment and provider activation remain separate operations.

## Validation record — 2026-09-29

The frontend full unit run passed 1,016 tests; the final model/presentation subset passed 11 tests after the elapsed-time extraction. Frontend type checking, layout/UI governance, architecture, complexity, ESLint, and the complete webpack production build passed. ESLint reported one navigation warning in the unchanged privacy settings page, with zero errors.

Agent Runtime passed all 170 tests, type checking, ESLint, and build. The final prompt/model/billing regression subset passed 72 tests. Worker routing/runtime subsets and the three cross-language contract tests passed; earlier unrelated timing failures passed when rerun in isolation. Root architecture/complexity checks, changed-Python Ruff checks, and diff whitespace checks passed. This record does not claim a fresh, single all-green full API/Worker suite after every final edit.

Both Astra browser flows passed against the production build: desktop light (1440×900) and phone dark (375×812). They verified model switching, off-to-low migration, valid effort choices, the high preset, preserved drafts, the actual submitted model/effort, and no horizontal page overflow. Settings screenshots were captured and reviewed. The earlier development-server attempt timed out during page loading; the production rerun retained the original browser timeouts and assertions. The temporary validation server was stopped.

No live paid-model probe, commit, push, or deployment was performed. Existing upload/identity work in the working tree was preserved.
