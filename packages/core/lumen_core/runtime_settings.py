"""可调系统设置元数据。

API 与 Worker 都消费它：
- API：管理员通过 /admin/settings 读写
- Worker：每次构造上游请求前 resolve（DB 优先，env fallback）

DB 中只持久化 SUPPORTED_SETTINGS 列表里的 key；其它 key 视为非法。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from .providers import normalize_provider_purposes, parse_provider_bool
from .video_providers import validate_video_providers


@dataclass(frozen=True)
class SettingSpec:
    key: str
    description: str
    sensitive: bool
    parser: type
    env_fallback: str
    # 可选数值范围（仅对 int/float 生效）；None 表示不限制。
    min_value: int | float | None = None
    max_value: int | float | None = None
    # 可选字符串枚举；None 表示不限制。
    allowed_values: tuple[str, ...] | None = None


SUPPORTED_SETTINGS: list[SettingSpec] = [
    SettingSpec(
        key="site.public_base_url",
        description=(
            "站点对外访问域名，用于生成邀请链接和分享链接。填写 web 根地址，"
            "例如 https://your-domain.example.com；不要带 /api、/invite 或其它路径。"
            "留空时会优先按当前请求的访问域名自动生成。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="PUBLIC_BASE_URL",
    ),
    SettingSpec(
        key="site.share_expiration_days",
        description=(
            "新生成图片分享链接的默认有效期，单位天。0 表示永久有效；"
            "大于 0 时，分享链接会在创建后 N 天自动失效。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="SHARE_EXPIRATION_DAYS",
        min_value=0,
        max_value=3650,
    ),
    SettingSpec(
        key="upstream.pixel_budget",
        description=(
            "默认像素预算（默认 1572864 ≈ 1.57M，仅用于 size_mode=auto 的预设推导）；"
            "4K 等显式 fixed_size 按上游真实能力校验，不受此值限制。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="UPSTREAM_PIXEL_BUDGET",
        # 自动尺寸只需要覆盖现实 UI 预设；显式 fixed_size 另有独立 4K 校验。
        min_value=65_536,
        max_value=16_777_216,
    ),
    SettingSpec(
        key="upstream.global_concurrency",
        description="全局上游并发（默认 4）",
        sensitive=False,
        parser=int,
        env_fallback="UPSTREAM_GLOBAL_CONCURRENCY",
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        key="upstream.connect_timeout_s",
        description="上游 HTTP 连接超时秒数（默认 10）",
        sensitive=False,
        parser=float,
        env_fallback="UPSTREAM_CONNECT_TIMEOUT_S",
        min_value=1,
        max_value=60,
    ),
    SettingSpec(
        key="upstream.read_timeout_s",
        description="上游 HTTP 读取超时秒数（默认 180）",
        sensitive=False,
        parser=float,
        env_fallback="UPSTREAM_READ_TIMEOUT_S",
        min_value=5,
        max_value=1800,
    ),
    SettingSpec(
        key="upstream.write_timeout_s",
        description="上游 HTTP 写入超时秒数（默认 30）",
        sensitive=False,
        parser=float,
        env_fallback="UPSTREAM_WRITE_TIMEOUT_S",
        min_value=1,
        max_value=120,
    ),
    SettingSpec(
        key="upstream.default_model",
        description="默认模型 id",
        sensitive=False,
        parser=str,
        env_fallback="UPSTREAM_DEFAULT_MODEL",
    ),
    SettingSpec(
        key="generation.fast_default",
        description=(
            "Fast 模式全站默认开关。0=默认关闭，1=默认开启；同时影响对话和生图的初始 "
            "Fast 状态，但不锁死，用户仍可在对话框里临时切换。"
            "示例：0 表示默认走完整 reasoning（更准更慢）；1 表示默认走 mini 路径（更快）。"
            "用户在 composer 里手动切换后会被记住，不会被这里覆盖。"
            "未配置时回退到 GENERATION_FAST_DEFAULT 环境变量；都没配则按 V1 默认 = 1（开启）。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="GENERATION_FAST_DEFAULT",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="chat.file_search_vector_store_ids",
        description=(
            "对话 file_search 默认 vector store id，多个用英文逗号分隔。"
            "请求侧未传 vector_store_ids 时使用这里的默认值。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="CHAT_FILE_SEARCH_VECTOR_STORE_IDS",
    ),
    SettingSpec(
        key="chat.max_tool_invocations",
        description="单轮对话最多允许的 Responses 内建工具调用次数，超过后终止本轮。",
        sensitive=False,
        parser=int,
        env_fallback="CHAT_MAX_TOOL_INVOCATIONS",
        min_value=1,
        max_value=64,
    ),
    SettingSpec(
        key="chat.tool_status_idle_timeout_s",
        description="工具状态无更新后判定为 timed_out 的秒数。",
        sensitive=False,
        parser=int,
        env_fallback="CHAT_TOOL_STATUS_IDLE_TIMEOUT_S",
        min_value=5,
        max_value=600,
    ),
    SettingSpec(
        key="chat.cancel_poll_interval_ms",
        description="对话 completion 主动取消轮询间隔，单位毫秒。",
        sensitive=False,
        parser=int,
        env_fallback="CHAT_CANCEL_POLL_INTERVAL_MS",
        min_value=50,
        max_value=5000,
    ),
    SettingSpec(
        key="chat.tool_web_search_micro",
        description="对话 web_search 工具预授权预算，单位微人民币。",
        sensitive=False,
        parser=int,
        env_fallback="CHAT_TOOL_WEB_SEARCH_MICRO",
        min_value=0,
        max_value=10_000_000,
    ),
    SettingSpec(
        key="chat.tool_file_search_micro",
        description="对话 file_search 工具预授权预算，单位微人民币。",
        sensitive=False,
        parser=int,
        env_fallback="CHAT_TOOL_FILE_SEARCH_MICRO",
        min_value=0,
        max_value=10_000_000,
    ),
    SettingSpec(
        key="chat.tool_code_interpreter_micro",
        description="对话 code_interpreter 工具预授权预算，单位微人民币。",
        sensitive=False,
        parser=int,
        env_fallback="CHAT_TOOL_CODE_INTERPRETER_MICRO",
        min_value=0,
        max_value=100_000_000,
    ),
    SettingSpec(
        key="chat.tool_image_generation_micro",
        description=(
            "对话 image_generation 工具预授权预算，单位微人民币。上限高于"
            " web/file_search，因为一次图片工具调用可接近独立图片生成任务成本。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="CHAT_TOOL_IMAGE_GENERATION_MICRO",
        min_value=0,
        max_value=100_000_000,
    ),
    SettingSpec(
        key="providers",
        description=(
            "上游 provider pool（JSON array）；唯一上游配置来源。旧 "
            "upstream.base_url/upstream.api_key 会由迁移写入此字段。"
        ),
        sensitive=True,
        parser=str,
        env_fallback="PROVIDERS",
    ),
    SettingSpec(
        key="providers.auto_probe_interval",
        description=(
            "文本算术 probe 间隔（秒）。0 = 关闭自动探活。默认 120。"
            "探活内容：让 gpt-5.4-mini 算 99×99，必须答 9801 才算 healthy。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="PROVIDERS_AUTO_PROBE_INTERVAL",
        min_value=0,
        max_value=3600,
    ),
    SettingSpec(
        key="providers.auto_image_probe_interval",
        description=(
            "Image probe 间隔（秒）。0 = 关闭。默认 0（生产先关，灰度后再开）。"
            "开启后每 N 秒发一张 1024x1024 低质量生图，必须真返回 base64 才算 healthy；"
            "每次 probe 都会消耗一次账号 OpenAI 配额，频率不要 < 1800（30 分钟）。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="PROVIDERS_AUTO_IMAGE_PROBE_INTERVAL",
        min_value=0,
        max_value=86400,
    ),
    SettingSpec(
        key="byok.mode_enabled",
        description="BYOK 总开关。0=关闭，1=开启。关闭时隐藏 key-first 注册和用户绑定入口。",
        sensitive=False,
        parser=int,
        env_fallback="BYOK_MODE_ENABLED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="auth.byok_signup_enabled",
        description="是否开放未登录 key-first 注册入口。0=关闭，1=开启。",
        sensitive=False,
        parser=int,
        env_fallback="AUTH_BYOK_SIGNUP_ENABLED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="auth.byok_signup_bypasses_allowlist",
        description="BYOK 注册是否绕过邮箱白名单或邀请链接。0=仍要求白名单/邀请，1=key 验证通过即可注册。",
        sensitive=False,
        parser=int,
        env_fallback="AUTH_BYOK_SIGNUP_BYPASSES_ALLOWLIST",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="byok.fallback_to_admin_provider",
        description="用户 key 失败或删除时是否允许回退站长全局 Provider Pool。默认 0。",
        sensitive=False,
        parser=int,
        env_fallback="BYOK_FALLBACK_TO_ADMIN_PROVIDER",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="byok.validation_model",
        # review #33：SettingSpec 当前不支持 min_string_length；admin 把空白
        # 提交进来时由 byok_service.read_byok_settings 在读取阶段 strip + 回退到
        # BYOK_DEFAULT_VALIDATION_MODEL（"gpt-5.4"），不会让 worker 拿到空字符串
        # 去构造上游请求。如果以后 SettingSpec 加上 min_string_length，就把这条
        # 校验下沉到 parse_value 走早失败更直接。
        description="用户 API Key 验证模型。默认 gpt-5.4。空白会在 byok_service 读取时回退到默认值。",
        sensitive=False,
        parser=str,
        env_fallback="BYOK_VALIDATION_MODEL",
    ),
    SettingSpec(
        key="byok.validation_timeout_ms",
        description="用户 API Key 验证超时毫秒。默认 15000。",
        sensitive=False,
        parser=int,
        env_fallback="BYOK_VALIDATION_TIMEOUT_MS",
        min_value=1000,
        max_value=120000,
    ),
    SettingSpec(
        key="byok.pending_token_ttl_seconds",
        description="key 验证通过到 BYOK 注册之间的一次性 token TTL。默认 900 秒。",
        sensitive=False,
        parser=int,
        env_fallback="BYOK_PENDING_TOKEN_TTL_SECONDS",
        min_value=60,
        max_value=3600,
    ),
    SettingSpec(
        key="billing.enabled",
        description="计费总开关。0=免费路径，1=启用钱包扣费。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_ENABLED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="billing.usd_to_rmb_rate",
        description="OpenAI USD 价折算到 RMB 的倍率。V1 默认 1.0。",
        sensitive=False,
        parser=float,
        env_fallback="BILLING_USD_TO_RMB_RATE",
        # Why: 0 would silently make all chat calls free (price_micro = 0). Force
        # admin to use `billing.enabled=0` instead if they want free chat.
        min_value=0.0001,
        max_value=100,
    ),
    SettingSpec(
        key="billing.allow_negative_balance",
        description="是否允许钱包扣到负数。生产应保持 0。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_ALLOW_NEGATIVE_BALANCE",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="billing.image_size_thresholds",
        description='像素数到生图价格档位的 JSON 映射，例如 {"1k":1572864,"2k":3686400,"4k":8294400}。',
        sensitive=False,
        parser=str,
        env_fallback="BILLING_IMAGE_SIZE_THRESHOLDS",
    ),
    SettingSpec(
        key="billing.redemption_code_secret",
        description="兑换码 HMAC 盐。创建/兑换兑换码前必须配置。",
        sensitive=True,
        parser=str,
        env_fallback="BILLING_REDEMPTION_CODE_SECRET",
    ),
    SettingSpec(
        key="billing.low_balance_warn_micro",
        description="低余额提示阈值，单位 micro-RMB。默认 2000000。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_LOW_BALANCE_WARN_MICRO",
        min_value=0,
        max_value=1_000_000_000_000,
    ),
    SettingSpec(
        key="billing.bootstrap_completed",
        description="计费首次初始化是否已完成。0=未完成，1=已完成。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_BOOTSTRAP_COMPLETED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="billing.show_estimate_in_composer",
        description="是否在发送框展示本次预计扣费。0=隐藏，1=显示。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_SHOW_ESTIMATE_IN_COMPOSER",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="billing.cache_aware",
        description="是否启用 prompt-cache 感知计费。0=旧两档，1=拆分 cache/read/create/image/reasoning。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_CACHE_AWARE",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="billing.use_redis_cache",
        description="是否启用计费余额/价格 Redis 镜像缓存。0=全走 DB，1=启用缓存。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_USE_REDIS_CACHE",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="video.enabled",
        description="视频生成总开关。0=关闭，1=开启。默认关闭，避免未配置 provider/价格时误提交昂贵任务。",
        sensitive=False,
        parser=int,
        env_fallback="VIDEO_ENABLED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="video.providers",
        description="视频 provider pool（JSON）；第一期用于火山 Seedance 2 异步任务 API。",
        sensitive=True,
        parser=str,
        env_fallback="VIDEO_PROVIDERS",
    ),
    SettingSpec(
        key="video.token_hold_estimates",
        description="视频 token 预扣上界表 JSON。缺项时 API 会拒绝创建任务，避免低估成本。",
        sensitive=False,
        parser=str,
        env_fallback="VIDEO_TOKEN_HOLD_ESTIMATES",
    ),
    SettingSpec(
        key="billing.fingerprint_required",
        description="是否强制校验 usage billing fingerprint 一致。迁移完成后再开启。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_FINGERPRINT_REQUIRED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="billing.window_rate_limit",
        description="是否启用 5h/1d/7d 消费窗口限额。",
        sensitive=False,
        parser=int,
        env_fallback="BILLING_WINDOW_RATE_LIMIT",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="billing.usd_to_cny",
        description="fallback 模型价格的 USD→CNY 汇率。默认 7.2。",
        sensitive=False,
        parser=float,
        env_fallback="BILLING_USD_TO_CNY",
        min_value=0.0001,
        max_value=100,
    ),
    SettingSpec(
        key="image.primary_route",
        description=(
            "(DEPRECATED) 旧图像主路径；已迁移为 image.channel + image.engine。"
            "responses = /v1/responses + image_generation tool（5.4 reasoning → gpt-image-2）；"
            "image2 = /v1/images/generations 或 /v1/images/edits direct（gpt-image-2）；"
            "image_jobs = sub2api 异步图片任务服务（/v1/image-jobs，当前仅文生图走此路，图生图保持私有图兼容路径）；"
            "dual_race = image2 + responses 两路并发，谁先完成谁赢，败方自动取消（每次任务消耗双倍上游配额）。"
            "默认 responses。image2 模式下 i2i 4K 历史上易触发上游 502，失败会自动 fallback 到 responses。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="IMAGE_PRIMARY_ROUTE",
        allowed_values=("responses", "image2", "image_jobs", "dual_race"),
    ),
    SettingSpec(
        key="image.channel",
        description=(
            "异步任务通道策略：auto 按 Provider 能力混合分发，stream_only 强制流式，"
            "image_jobs_only 强制异步任务。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="IMAGE_CHANNEL",
        allowed_values=("auto", "stream_only", "image_jobs_only"),
    ),
    SettingSpec(
        key="image.generation_concurrency",
        description=(
            "图片生成 FIFO 队列总并发。所有 1K/2K/4K、文生图和图生图共用此上限；"
            "实际并发还会受每个 provider/key 的 image_concurrency 限制。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="IMAGE_GENERATION_CONCURRENCY",
        min_value=1,
        max_value=32,
    ),
    SettingSpec(
        key="image.engine",
        description="生图引擎：responses（Codex 原生）/ image2（直调）/ dual_race（双路竞速）。",
        sensitive=False,
        parser=str,
        env_fallback="IMAGE_ENGINE",
        allowed_values=("responses", "image2", "dual_race"),
    ),
    SettingSpec(
        key="image.output_format",
        description="默认生图输出格式。jpeg 体积更小；png 更接近无损画质。透明背景请求仍会强制使用 png。",
        sensitive=False,
        parser=str,
        env_fallback="IMAGE_OUTPUT_FORMAT",
        allowed_values=("jpeg", "png"),
    ),
    SettingSpec(
        key="image.job_base_url",
        description=(
            "sub2api 图片异步任务服务地址。仅 image.channel 不是 stream_only 时使用；"
            "默认 https://image-job.example.com。可填服务根地址或 /v1 地址。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="IMAGE_JOB_BASE_URL",
    ),
    # DEPRECATED 2026-04-28：旧键。worker resolve 会先查 image.primary_route 再回落到这里；
    # SettingsPanel UI 已隐藏。保留 SettingSpec 是为了让现存 DB 行仍能被 resolve 读出（避免悄悄回落 default）。
    # 一次性迁移命令：UPDATE system_settings SET key='image.primary_route' WHERE key='image.text_to_image_primary_route';
    SettingSpec(
        key="image.text_to_image_primary_route",
        description="(DEPRECATED) 旧键，已被 image.primary_route 替代。",
        sensitive=False,
        parser=str,
        env_fallback="IMAGE_TEXT_TO_IMAGE_PRIMARY_ROUTE",
        allowed_values=("responses", "image2", "image_jobs", "dual_race"),
    ),
    SettingSpec(
        key="context.compression_enabled",
        description="上下文自动压缩开关。0=关闭，1=开启。默认 0，灰度后改为 1。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_COMPRESSION_ENABLED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="context.compression_trigger_percent",
        description="上下文使用率达到该百分比时允许生成摘要。默认 80。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_COMPRESSION_TRIGGER_PERCENT",
        min_value=50,
        max_value=98,
    ),
    SettingSpec(
        key="context.summary_target_tokens",
        description="摘要目标 token。默认 1200。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_SUMMARY_TARGET_TOKENS",
        min_value=300,
        max_value=8000,
    ),
    SettingSpec(
        key="context.summary_model",
        description="上下文摘要模型。默认 gpt-5.4。",
        sensitive=False,
        parser=str,
        env_fallback="CONTEXT_SUMMARY_MODEL",
    ),
    SettingSpec(
        key="context.summary_min_recent_messages",
        description="无论摘要怎么压缩，最近原文区至少保留这么多条消息。默认 16。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_SUMMARY_MIN_RECENT_MESSAGES",
        min_value=4,
        max_value=64,
    ),
    SettingSpec(
        key="context.summary_min_interval_seconds",
        description="同一会话两次自动压缩的最小间隔秒数，避免阈值附近来回压缩。默认 30。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_SUMMARY_MIN_INTERVAL_SECONDS",
        min_value=0,
        max_value=3600,
    ),
    SettingSpec(
        key="context.summary_input_budget",
        description="单次摘要 LLM 调用允许的输入 token 上限，超出则分段汇总。默认 80000。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_SUMMARY_INPUT_BUDGET",
        min_value=8000,
        max_value=200000,
    ),
    SettingSpec(
        key="context.summary_http_timeout_s",
        description="单个摘要上游请求的读取超时秒数。默认 120。",
        sensitive=False,
        parser=float,
        env_fallback="CONTEXT_SUMMARY_HTTP_TIMEOUT_S",
        min_value=10,
        max_value=600,
    ),
    SettingSpec(
        key="context.image_caption_enabled",
        description="出窗图片自动生成 caption。0=关闭，1=开启。默认 1。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_IMAGE_CAPTION_ENABLED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="context.image_caption_model",
        description="出窗图片 caption 模型。默认 gpt-5.4-mini。",
        sensitive=False,
        parser=str,
        env_fallback="CONTEXT_IMAGE_CAPTION_MODEL",
    ),
    SettingSpec(
        key="context.compression_circuit_breaker_threshold",
        description="最近 N 次摘要调用失败比例超此值则熔断 10 分钟，回退截断。默认 60（百分比）。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_COMPRESSION_CIRCUIT_BREAKER_THRESHOLD",
        min_value=10,
        max_value=100,
    ),
    SettingSpec(
        key="context.manual_compact_min_input_tokens",
        description="手动压缩起始 token。会话估算输入 token 达到该值后才允许手动压缩。默认 4000。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_MANUAL_COMPACT_MIN_INPUT_TOKENS",
        min_value=0,
        max_value=200000,
    ),
    SettingSpec(
        key="context.manual_compact_cooldown_seconds",
        description="同一会话两次手动压缩的冷却秒数。默认 600（10 分钟）。",
        sensitive=False,
        parser=int,
        env_fallback="CONTEXT_MANUAL_COMPACT_COOLDOWN_SECONDS",
        min_value=0,
        max_value=86400,
    ),
    # ----- 代理池（提供商 / Telegram 机器人 共用） -----
    SettingSpec(
        key="proxies.test_target",
        description=(
            "在管理后台点「测试」时要 ping 的网址，用来量代理快不快。"
            "默认填 https://api.telegram.org（Telegram 官网，国内直连不通，能反映出代理是否真的能走出墙）。"
            "如果代理主要是给上游 API 用的，可以填 https://api.example.com。"
            "测试只发一个空请求，不会真生图、也不消耗配额。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="PROXY_TEST_TARGET",
    ),
    SettingSpec(
        key="proxies.failure_threshold",
        description=(
            "同一个代理连续失败几次后被自动暂停。默认 3 次。"
            "数字越小越敏感（容易把偶尔抖动的代理误踢），越大越宽容（坏代理可能拖慢更多请求）。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="PROXY_FAILURE_THRESHOLD",
        min_value=1,
        max_value=20,
    ),
    SettingSpec(
        key="proxies.cooldown_seconds",
        description=(
            "代理被暂停后多少秒再重新启用。默认 60 秒（1 分钟）。"
            "设大了恢复慢，设小了可能反复试坏代理。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="PROXY_COOLDOWN_SECONDS",
        min_value=5,
        max_value=3600,
    ),
    # ----- Lumen 更新 -----
    SettingSpec(
        key="update.channel",
        description="一键更新 channel：stable/main/pinned/minor/major。",
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_UPDATE_CHANNEL",
        allowed_values=("stable", "main", "pinned", "minor", "major"),
    ),
    SettingSpec(
        key="update.check_ttl_sec",
        description="GitHub release 检查缓存 TTL，单位秒。0 表示禁用缓存。",
        sensitive=False,
        parser=int,
        env_fallback="LUMEN_UPDATE_CHECK_TTL_SEC",
        min_value=0,
        max_value=86_400,
    ),
    SettingSpec(
        key="update.allow_prerelease",
        description="检查更新时是否允许 prerelease 作为目标版本。",
        sensitive=False,
        parser=int,
        env_fallback="LUMEN_UPDATE_ALLOW_PRERELEASE",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="update.use_proxy_pool",
        description=(
            "在管理后台点「一键更新」时是否使用代理池。0=直连，1=使用代理池中选中的代理。"
            "只影响后台触发更新脚本时的 git、uv、npm 等出站请求。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="LUMEN_UPDATE_USE_PROXY_POOL",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="update.proxy_name",
        description=(
            "一键更新使用的代理名称。需要和代理池里的名称一致；"
            "留空时使用代理池中第一个启用代理。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_UPDATE_PROXY_NAME",
    ),
    # ----- 模特库同步 -----
    SettingSpec(
        key="model_library.sync_use_proxy_pool",
        description=(
            "同步模特库预设时是否使用代理池拉取 GitHub 文件。0=直连，1=使用代理池。"
            "只影响“模特库 -> 同步”按钮触发的 GitHub Contents API 和图片下载请求。"
        ),
        sensitive=False,
        parser=int,
        env_fallback="APPAREL_MODEL_LIBRARY_SYNC_USE_PROXY_POOL",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="model_library.sync_proxy_name",
        description=(
            "模特库同步使用的代理名称。需要和代理池里的名称一致；"
            "留空时使用代理池中第一个启用代理。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="APPAREL_MODEL_LIBRARY_SYNC_PROXY_NAME",
    ),
    # ----- Telegram 机器人 -----
    SettingSpec(
        key="telegram.bot_enabled",
        description=(
            "Telegram 机器人总开关。0=关，机器人启动后会立刻退出；1=开。"
            "想临时停掉机器人不用 ssh，把这里改 0 重启即可。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="TELEGRAM_BOT_ENABLED",
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="telegram.bot_token",
        description=(
            "你在 @BotFather 申请机器人时拿到的那串密钥（形如 123456789:REPLACE_WITH_BOT_TOKEN...）。"
            "留空时会回退到部署时 .env 里的旧值。修改后需要让机器人重启一次才生效。"
        ),
        sensitive=True,
        parser=str,
        env_fallback="TELEGRAM_BOT_TOKEN",
    ),
    SettingSpec(
        key="telegram.bot_username",
        description=(
            "机器人在 Telegram 上的用户名，**不带 @**，比如 lumenimagebot。"
            "网页生成绑定码时会拼成「https://t.me/<这里>?start=xxx」让你点开直跳。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="TELEGRAM_BOT_USERNAME",
    ),
    SettingSpec(
        key="telegram.allowed_user_ids",
        description=(
            "只允许哪些 Telegram 账号用机器人。填账号 ID（一串纯数字），多个用英文逗号分开。"
            "留空表示不卡这一道，仅靠「绑定码」流程兜底。"
            "建议填上自己的 ID 当作双重保险，万一 token 泄漏陌生人也用不了。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="TELEGRAM_ALLOWED_USER_IDS",
    ),
    SettingSpec(
        key="telegram.proxy_names",
        description=(
            "机器人和 Telegram 服务器通信走哪些代理。填代理池里的代理名字，多个用英文逗号分开。"
            "比如填 RFC,IX 表示这两个轮换；留空表示用所有启用的代理。"
            "国内服务器必须有可用代理，否则机器人发不了消息。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="TELEGRAM_PROXY_NAMES",
    ),
    SettingSpec(
        key="telegram.proxy_strategy",
        description=(
            "多个代理时怎么挑用哪个：\n"
            "• random = 每次请求都随机选一个（最稳，推荐）\n"
            "• latency = 在测过的最快的几条里随机选（追求速度）\n"
            "• failover = 主备模式，固定用第一个，挂了才切到下一个\n"
            "• round_robin = 轮流用，每次按顺序换下一个\n"
            "默认 random。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="TELEGRAM_PROXY_STRATEGY",
        allowed_values=("random", "latency", "failover", "round_robin"),
    ),
    # ----- 存储后端（/opt/lumendata 挂载来源） -----
    SettingSpec(
        key="storage.backend",
        description=(
            "Lumen 数据目录（/opt/lumendata）从哪里来。"
            "local = 本机磁盘上的目录；smb = 远端 SMB/CIFS 共享。"
            "切换会触发容器短暂重启（约 30 秒），且不会自动迁移已有数据，"
            "请先确认目标位置上的内容是你需要的。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_STORAGE_BACKEND",
        allowed_values=("local", "smb"),
    ),
    SettingSpec(
        key="storage.local.root",
        description=(
            "本机模式下，宿主机上要 bind 到 /opt/lumendata 的真实目录。"
            "例如 /var/lib/lumen-data。必须是绝对路径；目录会在挂载时自动创建。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_STORAGE_LOCAL_ROOT",
    ),
    SettingSpec(
        key="storage.smb.host",
        description=(
            "SMB 服务器地址（IP 或域名）。例如 10.10.10.40 或 nas.local。"
            "不要带 // 前缀。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_STORAGE_SMB_HOST",
    ),
    SettingSpec(
        key="storage.smb.share",
        description=(
            "SMB 共享名（NAS 上对外公开的那个名字）。例如 Lumen。不要带斜杠。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_STORAGE_SMB_SHARE",
    ),
    SettingSpec(
        key="storage.smb.subpath",
        description=(
            "共享内的子路径（可选）。例如 / 或 /lumen-prod。"
            "默认 /，表示直接用共享根目录。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_STORAGE_SMB_SUBPATH",
    ),
    SettingSpec(
        key="storage.smb.port",
        description=(
            "SMB 服务器端口（可选）。默认 445（标准 SMB-over-TCP）。"
            "NAS 端口被改过或走特殊网关时填这里；否则留空走默认。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_STORAGE_SMB_PORT",
    ),
    SettingSpec(
        key="storage.smb.username",
        description="SMB 登录用户名。",
        sensitive=False,
        parser=str,
        env_fallback="LUMEN_STORAGE_SMB_USERNAME",
    ),
    SettingSpec(
        key="storage.smb.password",
        description=(
            "SMB 登录密码。提交时留空表示保留旧值；首次配置必填。"
            "存储在 host 上的 conf 文件里（0600 权限），mount 时读取。"
        ),
        sensitive=True,
        parser=str,
        env_fallback="LUMEN_STORAGE_SMB_PASSWORD",
    ),
]


def get_spec(key: str) -> SettingSpec | None:
    for s in SUPPORTED_SETTINGS:
        if s.key == key:
            return s
    return None


def _provider_config_items(value: object) -> tuple[list[object], list[object]]:
    if isinstance(value, list):
        return value, []
    if not isinstance(value, dict):
        raise ValueError("providers must be a non-empty JSON array or object")
    provider_items = value.get("providers")
    if not isinstance(provider_items, list) or not provider_items:
        raise ValueError("providers.providers must be a non-empty JSON array")
    proxy_items = value.get("proxies", [])
    if proxy_items is None:
        proxy_items = []
    if not isinstance(proxy_items, list):
        raise ValueError("providers.proxies must be a JSON array")
    return provider_items, proxy_items


def _validate_proxy_item(item: object, index: int) -> str:
    if not isinstance(item, dict):
        raise ValueError(f"proxies[{index}] must be an object")
    name = item.get("name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"proxies[{index}].name is required")
    protocol = item.get("type", item.get("protocol", "socks5"))
    if not isinstance(protocol, str) or protocol.strip().lower() not in {
        "s5",
        "socks",
        "socks5",
        "socks5h",
        "ssh",
    }:
        raise ValueError(f"proxies[{index}].type must be socks5 or ssh")
    host = item.get("host", "")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(f"proxies[{index}].host is required")
    port = item.get("port", 22 if protocol.strip().lower() == "ssh" else 1080)
    try:
        port_int = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"proxies[{index}].port must be an integer") from exc
    if port_int < 1 or port_int > 65535:
        raise ValueError(f"proxies[{index}].port must be between 1 and 65535")
    return name.strip()


def validate_providers(raw: str) -> str:
    """Validate provider-pool JSON. Returns raw string if valid.

    Backward compatible formats:
    - old: `[{"name": "...", "base_url": "...", "api_key": "..."}]`
    - new: `{"providers": [...], "proxies": [...]}`
    """
    value = raw.strip()
    if not value:
        raise ValueError("providers must not be empty")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"providers is not valid JSON: {exc}") from exc
    items, proxies = _provider_config_items(parsed)
    if not items:
        raise ValueError("providers must be a non-empty JSON array")
    proxy_names: set[str] = set()
    for i, item in enumerate(proxies):
        proxy_name = _validate_proxy_item(item, i)
        if proxy_name in proxy_names:
            raise ValueError(f"proxies[{i}].name is duplicated: {proxy_name}")
        proxy_names.add(proxy_name)

    provider_names: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"providers[{i}] must be an object")
        name = item.get("name", f"provider-{i}")
        if isinstance(name, str) and name.strip():
            provider_name = name.strip()
            if provider_name in provider_names:
                raise ValueError(f"providers[{i}].name is duplicated: {provider_name}")
            provider_names.add(provider_name)
        base_url = item.get("base_url", "")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(f"providers[{i}].base_url is required")
        try:
            enabled = parse_provider_bool(item.get("enabled"), default=True)
        except ValueError as exc:
            raise ValueError(f"providers[{i}].enabled must be a boolean") from exc
        api_key = item.get("api_key", "")
        if not isinstance(api_key, str):
            raise ValueError(f"providers[{i}].api_key must be a string")
        if enabled and not api_key.strip():
            raise ValueError(f"providers[{i}].api_key is required")
        parts = urlsplit(base_url.strip())
        if not parts.scheme:
            raise ValueError(
                f"providers[{i}].base_url has no scheme (must be http:// or https://)"
            )
        if parts.scheme.lower() not in {"http", "https"}:
            raise ValueError(f"providers[{i}].base_url must use http or https")
        if not parts.hostname:
            raise ValueError(f"providers[{i}].base_url must include a hostname")
        if parts.username or parts.password:
            raise ValueError(f"providers[{i}].base_url must not include credentials")
        proxy_name = item.get("proxy", item.get("proxy_name"))
        if isinstance(proxy_name, str) and proxy_name.strip():
            name_clean = proxy_name.strip()
            if name_clean not in proxy_names:
                raise ValueError(
                    f"providers[{i}].proxy references unknown proxy: {name_clean}"
                )
        try:
            normalize_provider_purposes(item.get("purposes"))
        except ValueError as exc:
            raise ValueError(f"providers[{i}].purposes invalid: {exc}") from exc
    return value


def validate_public_base_url(raw: str) -> str:
    """Validate and normalize the public web origin used in copied links."""
    value = raw.strip().rstrip("/")
    if not value:
        raise ValueError("site.public_base_url must not be empty")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("site.public_base_url must use http or https")
    if not parts.hostname:
        raise ValueError("site.public_base_url must include a hostname")
    if parts.username or parts.password:
        raise ValueError("site.public_base_url must not include credentials")
    if parts.query or parts.fragment:
        raise ValueError("site.public_base_url must not include query or fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("site.public_base_url must be the web root, without a path")
    return value


def validate_image_size_thresholds(raw: str) -> str:
    """Validate the JSON shape of billing.image_size_thresholds.

    Expect an object whose values are positive integers (pixel counts).
    """
    value = raw.strip()
    if not value:
        raise ValueError("billing.image_size_thresholds must not be empty")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"billing.image_size_thresholds is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError(
            "billing.image_size_thresholds must be a non-empty JSON object"
        )
    for key, item in parsed.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                "billing.image_size_thresholds keys must be non-empty strings"
            )
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(
                f"billing.image_size_thresholds[{key!r}] must be a non-negative integer"
            )
    return value


def validate_video_token_hold_estimates(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("video.token_hold_estimates must not be empty")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"video.token_hold_estimates is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("video.token_hold_estimates must be a non-empty JSON object")
    for model, model_value in parsed.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError("video.token_hold_estimates model keys must be strings")
        if not isinstance(model_value, dict) or not model_value:
            raise ValueError(
                f"video.token_hold_estimates[{model!r}] must be a non-empty object"
            )
        for action, action_value in model_value.items():
            if action not in {
                "t2v",
                "i2v",
                "reference",
                "reference_image",
                "reference_video",
            }:
                raise ValueError(
                    f"video.token_hold_estimates[{model!r}] action must be t2v, i2v, reference, reference_image or reference_video"
                )
            if not isinstance(action_value, dict) or not action_value:
                raise ValueError(
                    f"video.token_hold_estimates[{model!r}][{action!r}] must be a non-empty object"
                )
            for key, estimate in action_value.items():
                if not isinstance(key, str) or ":" not in key:
                    raise ValueError(
                        f"video.token_hold_estimates[{model!r}][{action!r}] keys must look like resolution:duration"
                    )
                if (
                    not isinstance(estimate, int)
                    or isinstance(estimate, bool)
                    or estimate <= 0
                ):
                    raise ValueError(
                        f"video.token_hold_estimates[{model!r}][{action!r}][{key!r}] must be a positive integer"
                    )
    return value


def validate_redemption_code_secret(raw: str) -> str:
    value = raw.strip()
    if len(value) < 16:
        raise ValueError(
            "billing.redemption_code_secret must be at least 16 characters"
        )
    return value


def validate_image_job_base_url(raw: str) -> str:
    """Validate and normalize the async image job service base URL."""
    value = raw.strip().rstrip("/")
    if not value:
        raise ValueError("image.job_base_url must not be empty")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("image.job_base_url must use http or https")
    if not parts.hostname:
        raise ValueError("image.job_base_url must include a hostname")
    if parts.username or parts.password:
        raise ValueError("image.job_base_url must not include credentials")
    if parts.query or parts.fragment:
        raise ValueError("image.job_base_url must not include query or fragment")
    return value


def parse_value(spec: SettingSpec, raw: str) -> object:
    """根据 spec.parser 把字符串解析成正确类型；失败抛 ValueError。

    数值类型同时校验 min_value / max_value（若 spec 中已配置）。
    """
    if spec.key == "providers":
        return validate_providers(raw)
    if spec.key == "video.providers":
        return validate_video_providers(raw)
    if spec.key == "video.token_hold_estimates":
        return validate_video_token_hold_estimates(raw)
    if spec.key == "site.public_base_url":
        return validate_public_base_url(raw)
    if spec.key == "image.job_base_url":
        return validate_image_job_base_url(raw)
    if spec.key == "billing.image_size_thresholds":
        return validate_image_size_thresholds(raw)
    if spec.key == "billing.redemption_code_secret":
        return validate_redemption_code_secret(raw)
    if spec.parser is str:
        if spec.allowed_values is not None and raw not in spec.allowed_values:
            allowed = ", ".join(spec.allowed_values)
            raise ValueError(f"{spec.key} must be one of: {allowed}")
        return raw
    if spec.parser is int:
        value: int | float = int(raw)
    elif spec.parser is float:
        value = float(raw)
    else:
        raise ValueError(f"unsupported parser {spec.parser!r}")

    if spec.allowed_values is not None:
        # 严格按字符串字面比对：把 raw 和 allowed_values 都做 strip 后比 string，
        # 不要先 int(raw) → set(int(allowed)) 再比。后者会让 "00" / " 0 " / "+0"
        # 等变体被误判为合法（都 parse 成 0），而管理员配置 ("0", "1") 时
        # 期望的是布尔开关字面值。
        normalized = raw.strip()
        if normalized not in {av.strip() for av in spec.allowed_values}:
            allowed = ", ".join(spec.allowed_values)
            raise ValueError(f"{spec.key} must be one of: {allowed}")

    if spec.min_value is not None and value < spec.min_value:
        raise ValueError(f"{spec.key}={value} below min ({spec.min_value})")
    if spec.max_value is not None and value > spec.max_value:
        raise ValueError(f"{spec.key}={value} above max ({spec.max_value})")
    return value


__all__ = [
    "SettingSpec",
    "SUPPORTED_SETTINGS",
    "get_spec",
    "parse_value",
    "validate_image_job_base_url",
    "validate_image_size_thresholds",
    "validate_providers",
    "validate_public_base_url",
    "validate_redemption_code_secret",
    "validate_video_token_hold_estimates",
    "validate_video_providers",
]
