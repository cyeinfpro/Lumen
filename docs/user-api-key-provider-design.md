# 用户自带 API Key 供应商接入设计

## 1. 背景

当前系统已有管理员 Provider Pool：管理员在后台配置 `base_url + api_key`，Worker 统一使用管理员的供应商账号调用上游。这适合自用或少量朋友使用，但不适合 API 站直连接入场景。

新功能目标是增加一条 BYOK 路径，BYOK 即 Bring Your Own Key：

- 管理员只配置可信供应商模板，例如供应商名称、`base_url`、可用模型、用途、代理和探活参数。
- 用户无需先有账号，可以先选择供应商并输入自己的 API Key。
- 系统用该 key 发起一次真实的 `gpt-5.4` 请求，要求模型计算服务端随机生成的算式并只返回整数。
- 验证通过后，用户再输入邮箱和密码注册。
- 注册成功后，这个 key 绑定到该用户，后续用户所有功能请求默认使用自己的 key 和管理员配置好的供应商 URL。
- 用户注册后可以在账号设置里替换 key。替换前必须再次验证，验证失败不得覆盖旧 key。

本设计不替换现有管理员全局 Provider Pool，而是新增一条用户级凭证路径。管理员全局 Provider 继续服务已有场景，用户自带 key 只服务绑定该 key 的用户。管理员可以在系统设置里开启或关闭 BYOK 模式：关闭时系统行为与现在完全一致；开启后，只影响 key-first 注册和已绑定用户 key 的账号。通过邀请链接或邮箱白名单注册的用户不受 BYOK 模式影响，仍然使用管理员配置的全局供应商池。

## 2. 核心结论

1. 不要把用户 key 写进现有 `system_settings.providers` JSON。那是全局配置，不适合存用户私密凭证。
2. 新增“供应商模板”和“用户 API 凭证”两类数据。
3. Key 验证必须走真实上游请求，不能只校验格式。
4. 验证挑战必须用随机算术题，生产环境每次验证都重新生成；不得在代码、配置或测试契约里依赖固定题目。
5. 验证成功到注册之间使用短期一次性 token 传递已验证 key，服务端保存加密后的临时 key，TTL 建议 15 分钟。
6. 后续 Worker 执行任务时，应按任务创建时绑定的 `credential_id` 解析供应商和 key，避免用户修改 key 影响已入队任务。
7. BYOK 请求默认不回退到管理员全局 key，避免用户 key 失效时把成本转嫁到站长账号。
8. BYOK 是一个显式系统模式开关，默认关闭；关闭时不展示 key-first 注册入口，不开放公开 key 验证接口，现有邀请注册和管理员 Provider Pool 不变。
9. BYOK 开启后，管理员可以为用户自带 key 配置一个或多个“专用供应商 URL”。这些 URL 只用于用户 credential runtime，不进入管理员全局 Provider Pool。
10. 用户分流按注册和任务凭证决定：邀请链接或白名单注册的用户没有 `user_api_credential_id`，继续走管理员全局 Provider Pool；BYOK 注册或后续主动绑定 key 的用户，其新建任务才固定走自己的 credential。

## 3. 产品流程

### 3.1 模式开关和用户分流

管理员在系统设置中控制 BYOK 模式：

- `byok.mode_enabled = false`：默认关闭。注册页只保留现有邮箱、邀请链接、白名单流程；`/auth/api-suppliers` 不返回公开供应商；`/auth/api-key/verify`、`/auth/signup/byok` 和用户绑定接口返回 `byok_disabled`。
- `byok.mode_enabled = true`：开启 BYOK 能力。管理员可以维护用户 key 专用供应商模板，用户可以在允许的入口绑定自己的 API Key。
- `auth.byok_signup_enabled`：控制是否开放未登录 key-first 注册入口。可以开启 BYOK 但只允许已登录用户绑定 key。
- `auth.byok_signup_bypasses_allowlist`：控制 BYOK 注册是否绕过邮箱白名单或邀请链接。开启后表示“key 验证通过即可注册”；关闭时仍要求 invite 或 allowlist。

开启 BYOK 不改变已有用户和邀请用户的默认路径：

- 管理员自己和通过邀请链接注册的用户，继续使用 `/admin/providers` 里的全局 Provider Pool。
- 通过 BYOK 注册的用户，默认使用自己绑定的 key。
- 后续用户在账号设置里主动绑定 key 后，该用户的新任务才走 BYOK credential。
- 一个任务只看创建时固定的 `user_api_credential_id`；为空则走管理员 Provider Pool，非空则走用户 key。

### 3.2 管理员配置供应商模板

管理员进入后台“API 站接入”页面，新增供应商模板：

- 名称：例如 `OpenAI API 站 A`
- 用户 key 专用 Base URL：例如 `https://api.example.com`
- 协议类型：OpenAI Responses-compatible
- 验证模型：默认 `gpt-5.4`
- 默认聊天模型：默认 `gpt-5.4`
- Fast 模型：可选 `gpt-5.4-mini`
- 用途：`chat`、`image`、`embedding`
- 是否允许用于公开 key 注册
- 是否允许已登录用户绑定或替换 key
- 代理：复用现有 Provider Proxy 配置
- 超时：默认 15 秒
- 并发限制：每个用户 key 的文本和图片并发上限

供应商模板不保存管理员 key。管理员可以临时输入一个测试 key 进行模板探活，但测试 key 不落库。

供应商模板的 `base_url` 是 BYOK 专用 URL，只用于验证用户输入的 key 和执行绑定了用户 credential 的任务。它不写入 `system_settings.providers`，也不会被管理员全局 Provider Pool 轮询。管理员仍然可以在现有 `/admin/providers` 里继续配置和使用其他供应商，系统在非 BYOK 任务上按原逻辑正常调用。

### 3.3 新用户 key-first 注册

1. 用户打开注册页。
2. 页面展示管理员启用的供应商列表。
3. 用户选择供应商并输入 API Key。
4. 前端调用 `POST /auth/api-key/verify`。
5. 后端用该 key 调供应商 `POST /v1/responses`，让 `gpt-5.4` 计算一个随机算术题。
6. 输出符合预期后，后端创建短期 `verification_token`。
7. 前端进入邮箱和密码表单。
8. 用户提交邮箱和密码，前端调用 `POST /auth/signup/byok`，携带 `verification_token`。
9. 后端创建用户、创建用户 API 凭证、创建 session，用户直接进入应用。

如果 BYOK 模式关闭，注册页不展示上述步骤，用户只能走现有 `/auth/signup`、邀请链接或白名单流程。

### 3.4 邀请链接注册

邀请链接注册保持现有语义：

1. 用户通过邀请链接进入注册页。
2. 前端调用现有 `POST /auth/signup`，携带 `invite_token`。
3. 后端校验邀请链接、创建用户、创建 session。
4. 不创建 `user_api_credentials`，任务行上的 `user_api_credential_id` 为空。
5. 该用户后续聊天、生图、识图、工作流任务默认继续使用管理员全局 Provider Pool。

即使管理员开启 BYOK 模式，邀请链接用户也不强制输入 API Key。只有当该用户之后在账号设置里主动绑定 key，才切换到用户 credential 路径。

### 3.5 已登录用户替换 key

1. 用户进入账号设置的“API Key”区域。
2. 如果 BYOK 模式开启且存在 `user_bind_enabled` 的供应商，用户可以选择供应商并输入新 key。
3. 前端调用 `PUT /me/api-credentials/{supplier_id}`。
4. 后端先验证新 key。
5. 验证通过后，旧 key 标记为 `replaced`，新 key 写入为 `active`。
6. 验证失败时，旧 key 保持不变。

### 3.6 后续功能调用

用户发起聊天、生图、识图、工作流任务时：

1. API 创建 `Completion` 或 `Generation` 行前，判断该用户是否有 active credential。
2. 如果有 active credential，将 `user_api_credential_id` 和 `upstream_supplier_id` 固定写入任务行。
3. 如果没有 active credential，两个字段保持为空，Worker 继续走管理员全局 Provider Pool。
4. Worker 执行 BYOK 任务时读取 credential，解密 key，组装临时 Provider。
5. BYOK 请求打到供应商模板的专用 `base_url`，Authorization 使用用户自己的 key。
6. 上游返回 401/403 时，将该 credential 标记为 `invalid`，任务失败并提示用户更新 key。
7. 上游返回 429 或 quota 错误时，不切管理员全局 key，向用户显示 key 限流或额度不足。

## 4. Key 验证设计

### 4.1 请求形态

默认验证走 OpenAI Responses-compatible 请求。每次验证先生成一次随机挑战：

- 使用安全随机数选择操作数、运算符和题面模板。运算符至少支持乘法，也可以扩展为加法、减法或混合表达式；所有题目都必须有唯一整数答案。
- 服务端本地计算 `expected`，不要把期望答案发给模型。
- 将 `operands`、`operator`、`expression`、`expected`、`created_at` 写入 `challenge_jsonb`，便于审计和排查，但不需要返回给前端。
- 请求模型时只暴露算式，不暴露期望答案。

请求体示例用占位符表示运行时生成的值：

```json
{
  "model": "gpt-5.4",
  "instructions": "You are a precise calculator. Return only the final integer. No words, no punctuation, no explanation.",
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "Calculate {expression}. Return only the integer."
        }
      ]
    }
  ],
  "stream": false,
  "store": false,
  "max_output_tokens": 16
}
```

固定题目不应用于生产验证，因为可能被网关缓存、被错误实现硬编码，或被攻击者利用重放规律。测试应验证“挑战生成器每次产出新题、校验器使用本次生成的 `expected` 判定答案”，不要把某一道具体题目写成接口契约。

### 4.2 判定规则

验证通过必须同时满足：

- HTTP 状态是 2xx。
- 响应能解析出文本输出。解析逻辑应复用 Provider Probe 中已有的 Responses 输出解析能力。
- 输出归一化后等于期望整数。
- 验证耗时未超过供应商模板配置的 timeout。

输出归一化规则：

- `trim()` 去掉首尾空白。
- 允许数字中出现英文逗号或普通空格，归一化时只去掉这些分组分隔符。
- 不允许解释性文字。如果输出是 `The answer is {expected}`，应判定失败。
- 不允许多个数字。如果输出包含多段数字，应判定失败。

### 4.3 错误分类

| 上游表现 | 对用户错误码 | 含义 |
|---|---|---|
| 401 / 403 | `invalid_api_key` | key 无效、权限不足或被供应商拒绝 |
| 404 / 405 | `supplier_unsupported` | 供应商 URL 或 `/v1/responses` 协议不兼容 |
| model not found | `model_not_available` | 该 key 或供应商不可用 `gpt-5.4` |
| 429 | `key_rate_limited` | key 可疑似有效，但当前被限流或额度不足，不允许注册通过 |
| 5xx | `supplier_transient_error` | 供应商临时错误，可稍后重试 |
| timeout | `validation_timeout` | 验证超时 |
| 2xx 但答错 | `validation_wrong_answer` | 供应商返回不可信，或模型不符合要求 |
| JSON/SSE 解析失败 | `invalid_supplier_response` | 协议不兼容或响应异常 |

429 是否允许通过需要明确：本设计建议不允许。原因是注册后立即使用也大概率失败，会制造更差的体验。可以在错误文案里提示“key 可能有效，但当前无法验证可用性”。

## 5. 数据模型

### 5.1 `api_supplier_templates`

管理员配置的用户 key 供应商模板。

字段建议：

- `id`
- `name`
- `slug`
- `base_url`
- `enabled`
- `public_signup_enabled`
- `user_bind_enabled`
- `purposes`，JSONB 或数组，取值 `chat`、`image`、`embedding`
- `validation_model`，默认 `gpt-5.4`
- `default_chat_model`，默认 `gpt-5.4`
- `fast_chat_model`，默认 `gpt-5.4-mini`
- `validation_timeout_ms`
- `proxy_name`
- `text_concurrency_per_key`
- `image_concurrency_per_key`
- `capabilities_jsonb`
- `created_by`
- `created_at`
- `updated_at`
- `deleted_at`

约束：

- `slug` 唯一。
- `base_url` 是用户 key 专用供应商 URL，必须是 `http` 或 `https`，不能包含 username/password。
- `base_url` 不写入 `system_settings.providers`，也不能被管理员 Provider Pool 当作全局 provider 轮询。
- 生产环境默认禁止配置内网地址，除非显式打开 admin-only 的开发开关。

### 5.2 `user_api_credentials`

用户绑定的 API Key。

字段建议：

- `id`
- `user_id`
- `supplier_id`
- `key_ciphertext`
- `key_hash`
- `key_hint`
- `encryption_key_version`
- `status`，取值 `active`、`invalid`、`replaced`、`revoked`
- `last_verified_at`
- `last_failed_at`
- `last_error_code`
- `rate_limited_until`
- `capabilities_jsonb`
- `created_at`
- `updated_at`
- `deleted_at`

约束：

- 同一个用户同一时刻只允许一个 active credential。
- `key_hash` 使用 HMAC-SHA256，不用普通 SHA256，避免离线枚举常见 key。
- `key_hint` 只显示前 4 后 4 或后 4，例如 `sk-...Ab12`。不保存可逆的明文 hint。
- `key_ciphertext` 使用 AES-GCM 或 KMS 加密，生产环境缺少加密主密钥时服务拒绝启动。

### 5.3 `pending_api_key_verifications`

新用户注册前的短期验证票据。

字段建议：

- `id`
- `token_hash`
- `supplier_id`
- `key_ciphertext`
- `key_hash`
- `key_hint`
- `challenge_jsonb`
- `verified_at`
- `expires_at`
- `consumed_at`
- `ip_hash`
- `ua_hash`

规则：

- TTL 15 分钟。
- token 只返回一次，服务端只存 hash。
- signup 成功后设置 `consumed_at`。
- 过期或已消费 token 不能复用。

### 5.4 任务表字段

建议在 `completions` 和 `generations` 上新增：

- `user_api_credential_id nullable`
- `upstream_supplier_id nullable`

创建任务时固定这两个字段。Worker 不应在执行时重新选择用户当前 key，否则排队中的任务会被用户后续修改 key 的操作影响。

### 5.5 系统设置字段

BYOK 模式开关继续走现有 `system_settings` 机制，不混入 `system_settings.providers`：

- `byok.mode_enabled`：总开关，默认 `0`。
- `auth.byok_signup_enabled`：是否开放未登录 key-first 注册，默认 `0`。
- `auth.byok_signup_bypasses_allowlist`：BYOK signup 是否绕过 allowlist / invite，默认 `0`。
- `byok.fallback_to_admin_provider`：用户 key 失败时是否允许站长 key 兜底，默认 `0`。
- `byok.validation_model`：默认验证模型，默认 `gpt-5.4`。
- `byok.validation_timeout_ms`：默认 15000。
- `byok.pending_token_ttl_seconds`：默认 900。

关闭 `byok.mode_enabled` 不删除已保存的用户 credential。关闭期间不允许新增注册、验证或替换 key；已有 BYOK credential 不应被静默改成管理员 Provider Pool 兜底，避免用户成本边界突然变化。

## 6. API 设计

### 6.1 管理员接口

`GET /admin/byok-settings`

返回 BYOK 总开关、注册开关、fallback 策略和默认验证参数。

`PATCH /admin/byok-settings`

更新 BYOK 设置。需要 admin session 和 CSRF。关闭 `byok.mode_enabled` 时，前端应隐藏 key-first 注册和用户绑定入口。

`GET /admin/api-suppliers`

返回供应商模板列表。

`POST /admin/api-suppliers`

创建供应商模板。需要 admin session 和 CSRF。

`PATCH /admin/api-suppliers/{id}`

更新名称、base_url、模型、用途、启停状态等。需要 CSRF。

`POST /admin/api-suppliers/{id}/probe`

管理员临时输入一个 test key 做模板探活。test key 不落库。

`GET /admin/api-suppliers/{id}/stats`

返回 active 用户 key 数、最近验证成功率、运行时错误分布。

### 6.2 公开注册接口

`GET /auth/api-suppliers`

当 `byok.mode_enabled = true` 且 `auth.byok_signup_enabled = true` 时，返回允许公开注册的供应商模板，只包含可展示字段：

```json
{
  "items": [
    {
      "id": "supplier_id",
      "name": "OpenAI API 站 A",
      "purposes": ["chat", "image"],
      "validation_model": "gpt-5.4"
    }
  ]
}
```

当 BYOK 模式关闭或公开注册关闭时，返回空列表或 `byok_disabled`。推荐返回空列表给注册页用于静默隐藏入口，写接口仍返回明确错误码。

`POST /auth/api-key/verify`

未登录用户验证 key。

请求：

```json
{
  "supplier_id": "supplier_id",
  "api_key": "sk-..."
}
```

成功响应：

```json
{
  "ok": true,
  "verification_token": "one_time_token",
  "supplier_id": "supplier_id",
  "key_hint": "sk-...Ab12",
  "verified_at": "2026-05-08T00:00:00Z"
}
```

`POST /auth/signup/byok`

请求：

```json
{
  "email": "user@example.com",
  "password": "password",
  "display_name": "",
  "verification_token": "one_time_token"
}
```

行为：

- 校验 token 未过期、未消费。
- 校验邮箱未注册。
- 创建 user。
- 将 pending key 迁移到 `user_api_credentials`。
- 设置 token consumed。
- 创建 session 和 CSRF cookie。

该接口只服务 key-first 注册，不替代现有 `/auth/signup`。是否绕过现有邮箱白名单要由管理员开关控制。本功能的目标是“key 通过即可注册”，所以建议新增设置：

- `auth.byok_signup_enabled`
- `auth.byok_signup_bypasses_allowlist`

默认值建议：

- 内部部署：`byok_signup_enabled = false`
- 启用该功能时：管理员明确打开 bypass，否则仍要求 invite 或 allowlist

邀请链接注册继续走 `/auth/signup`，不会要求 `verification_token`，也不会创建 `user_api_credentials`。

### 6.3 用户接口

`GET /me/api-credentials`

返回当前用户 key 状态，不返回明文。

`PUT /me/api-credentials/{supplier_id}`

已登录用户绑定或替换 key。需要 CSRF。

请求：

```json
{
  "api_key": "sk-..."
}
```

行为：

- `byok.mode_enabled = false` 时返回 `byok_disabled`，不修改旧 credential。
- supplier 必须 `enabled = true` 且 `user_bind_enabled = true`。
- 先验证新 key。
- 成功后事务性写入新 credential。
- 将旧 active credential 标记为 `replaced`。
- 失败不修改旧 credential。

`DELETE /me/api-credentials/{credential_id}`

撤销本地保存的 key。撤销后，如果站点不允许管理员 Provider fallback，则用户无法继续发起需要上游的功能请求，直到重新绑定 key。

## 7. Worker 路由设计

### 7.1 Provider 解析

新增运行时解析函数：

```python
async def resolve_task_runtime(task) -> ResolvedRuntime:
    if task.user_api_credential_id:
        return await resolve_user_credential_runtime(task.user_api_credential_id)
    return await resolve_admin_provider_runtime()
```

用户 credential runtime 组装为临时 provider：

- `name = "user:{supplier_slug}:{credential_id_prefix}"`
- `base_url = supplier.base_url`
- `api_key = decrypt(credential.key_ciphertext)`
- `proxy = supplier.proxy`
- `purposes = supplier.purposes`
- `image_concurrency = supplier.image_concurrency_per_key`

### 7.2 Failover 规则

BYOK 模式下默认只使用用户自己的 key：

- 401/403：标记 credential `invalid`，任务失败。
- 429/quota：写 `rate_limited_until`，任务失败或按 retry-after 延迟重试。
- 5xx/timeout：可按现有 retry 策略重试同一个 key，但不切换到其他用户或管理员 key。
- `fallback_to_admin_provider` 默认 false，除非管理员明确打开并在 UI 提示成本会由站点承担。

### 7.3 队列并发

图片队列现在按 provider name 做并发锁。用户 key provider name 必须包含 credential ID，避免所有用户共享一个供应商模板锁。

建议 Redis key：

- `generation:image_queue:provider:user:{credential_id}`
- `generation:image_queue:task_provider:{task_id} = user:{credential_id}`

这样每个用户 key 都有独立并发上限，供应商模板还可以设置全局保护上限，例如同一个 supplier 同时最多 100 个 BYOK 图片任务。

## 8. 安全设计

### 8.1 密钥存储

- 明文 key 只在请求生命周期内存在内存中。
- 数据库存储 `key_ciphertext`。
- 加密算法建议 AES-256-GCM，随机 nonce，每条 key 独立 nonce。
- 主密钥从环境变量或 KMS 获取，不写入数据库。
- 支持 `encryption_key_version`，为后续轮换预留。

### 8.2 日志和审计

禁止记录：

- 明文 API Key
- Authorization header
- 上游完整请求体中的敏感字段

允许记录：

- `supplier_id`
- `credential_id`
- `key_hash` 的短前缀，仅限内部排查
- `key_hint`
- 错误码
- HTTP status
- latency

新增审计事件：

- `admin.api_supplier.create`
- `admin.api_supplier.update`
- `auth.api_key.verify.success`
- `auth.api_key.verify.fail`
- `auth.signup.byok.success`
- `me.api_credential.create`
- `me.api_credential.replace`
- `me.api_credential.revoke`
- `runtime.user_api_key.invalid`

### 8.3 滥用防护

公开验证接口必须限流：

- IP 维度：例如 5 次/分钟，30 次/小时。
- supplier 维度：避免打爆单个供应商。
- key_hash 维度：同一个 key 验证失败过多，短期冻结。

其他防护：

- `api_key` 最大长度限制，例如 512。
- 只接受请求 body，不接受 query string。
- 错误响应不回显 key。
- 验证失败也做固定下限耗时，降低枚举侧信道。
- 供应商 `base_url` 只允许管理员配置，用户不能提交任意 URL。

### 8.4 用户告知

账号设置页需要明确展示：

- 你的 API Key 会加密保存，用于代表你向所选供应商发起请求。
- 删除本地 key 不会撤销供应商侧 key，如需彻底停用请去供应商后台撤销。
- 站点管理员配置供应商 URL，用户 key 只发送给该供应商。

## 9. 前端设计

### 9.1 注册页

建议做成两步：

第一步：连接 API Key。

- 供应商选择器
- API Key 输入框
- “验证 Key”按钮
- 验证中状态
- 错误提示按错误码展示

第二步：创建账号。

- 邮箱
- 密码
- 确认密码
- “创建账号”按钮

如果第一步 token 过期，回到 key 验证步骤。

### 9.2 账号设置

在账号中心增加“API Key”区域：

- 当前供应商名称
- 当前 key hint
- 状态：active、invalid、rate limited
- 最近验证时间
- 替换 key 按钮
- 删除 key 按钮

替换流程必须先验证新 key。验证失败时显示错误，不关闭弹窗，不影响旧 key。

### 9.3 管理员后台

在 Admin 页面增加“API 站接入”面板，或在现有 Providers 面板内增加一个 tab：

- 全局 Provider：现有能力，管理员提供 key。
- 用户自带 Key 供应商：新增能力，管理员只提供供应商模板。

供应商模板卡片展示：

- 名称
- Base URL host
- 验证模型
- 用途
- 是否允许注册
- active 用户 key 数
- 最近 24 小时验证成功率
- 最近运行错误分布

## 10. 与现有系统的关系

### 10.1 Auth

现有 `/auth/signup` 仍保留邮箱白名单和邀请链接逻辑。BYOK 注册建议走新 endpoint `/auth/signup/byok`，避免影响原有注册语义。

### 10.2 Provider Pool

现有 `/admin/providers` 继续管理站长全局 key。新增供应商模板不进入 `providers` 配置，避免 Worker 把用户 key 当作全局 provider 轮询。

### 10.3 Probe

现有 Provider Probe 的 `_probe_one` 已经是算术探活思路。应把“构造请求、解析 Responses 输出、判定答案”的公共逻辑下沉到共享模块，供：

- 管理员全局 provider probe
- 管理员 supplier template probe
- 用户 API key verify
- 后续自动健康检查

### 10.4 Worker

Worker 需要支持两类 runtime：

- admin provider runtime：现有 Provider Pool。
- user credential runtime：由任务上的 `user_api_credential_id` 指定。

两者共享上游 HTTP 调用代码，但选择、熔断、统计和 failover 规则不同。

## 11. 测试计划

### 11.1 API 单元测试

- 创建供应商模板成功。
- 非 admin 不能创建供应商模板。
- `base_url` 非法时拒绝。
- 未启用 public signup 的 supplier 不出现在公开列表。
- key 验证成功返回 verification token。
- key 验证 401 返回 `invalid_api_key`。
- key 验证 429 返回 `key_rate_limited`。
- key 验证 2xx 但答案错误返回 `validation_wrong_answer`。
- verification token 过期后不能注册。
- verification token 被消费后不能复用。
- BYOK signup 成功后创建 user 和 active credential。
- 已注册邮箱不能通过 BYOK signup 重复创建。
- 替换 key 成功后旧 credential 变 `replaced`。
- 替换 key 失败时旧 credential 仍 active。

### 11.2 Worker 测试

- task 有 `user_api_credential_id` 时使用用户 key。
- task 无 credential 时继续走现有 admin provider。
- 用户 key 401 时任务失败并标记 credential invalid。
- 用户 key 429 时不回退管理员 provider。
- 图片任务 Redis provider lock 包含 credential ID。
- 用户替换 key 不影响已创建任务的 credential pinning。

### 11.3 前端测试

- 注册页 key 验证成功后进入账号表单。
- token 过期时提示重新验证。
- 替换 key 验证失败时不清空旧 key 状态。
- 管理员模板启停后公开供应商列表实时变化。

## 12. 分阶段落地

### P0：后端基础能力

- 新增 DB migration 和 ORM model。
- 新增供应商模板 admin CRUD。
- 新增 key 验证 service。
- 新增 BYOK signup endpoint。
- 新增用户 credential 查询、替换、撤销接口。

### P1：Worker 接入

- 任务创建时写入 `user_api_credential_id`。
- Worker 支持 user credential runtime。
- BYOK 错误分类和 credential 状态回写。
- 图片队列按 credential ID 隔离并发。

### P2：前端接入

- 注册页 key-first 流程。
- 账号设置 key 管理。
- 管理员 API 站接入面板。

### P3：可观测性和运营

- Admin stats。
- Prometheus 指标。
- 审计日志。
- 密钥加密轮换脚本。

## 13. 验收标准

功能验收：

- 管理员能配置一个不带 key 的供应商模板。
- 未登录用户能输入 key 并通过真实 `gpt-5.4` 算术请求验证。
- 验证通过后用户能用邮箱密码注册。
- 注册后的用户请求使用自己的 key 和管理员配置的供应商 URL。
- 用户能替换 key，且替换前必须验证。
- 验证失败不会覆盖旧 key。

安全验收：

- 数据库没有明文 key。
- 日志和审计没有明文 key。
- 公开验证接口有限流。
- 用户不能提交任意 base_url。
- BYOK key 失败时默认不使用管理员全局 key 兜底。

兼容验收：

- 未启用 BYOK 时，现有注册、登录、Provider Pool、Worker 任务不受影响。
- 现有管理员全局 provider 仍可正常探活和调用。

## 14. 需要明确的产品开关

上线前建议在后台暴露这些开关：

- `auth.byok_signup_enabled`：是否启用 key-first 注册。
- `auth.byok_signup_bypasses_allowlist`：key 验证通过是否绕过邮箱白名单。
- `byok.fallback_to_admin_provider`：用户 key 失败时是否允许站长 key 兜底，默认 false。
- `byok.validation_model`：默认验证模型，默认 `gpt-5.4`。
- `byok.validation_timeout_ms`：默认 15000。
- `byok.pending_token_ttl_seconds`：默认 900。

推荐初始配置：

- 先只对管理员手动给出的测试用户开放。
- 观察验证成功率、运行时 401/429 比例。
- 稳定后再打开公开 key-first 注册。
