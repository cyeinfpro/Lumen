# 账号会话记忆系统设计

## 1. 背景与目标

Lumen 现有 `conversations` + `messages` + `generations`,以及单会话内的 `compact` 摘要机制,但缺少**跨 conversation、跟用户账号绑定的长时记忆**。本设计补齐这一层,使后续对话能复用用户已经透露过的身份、偏好、禁忌、在做的事 —— 接近 ChatGPT / Claude 那种"它真的懂我"的体感。

**范围**

- 仅 **web 端文本对话**;不涉及生图偏好
- 仅 **账号级记忆**(跨 conversation 共享)
- 不做 tgbot 端 UI(当前 tgbot 只生图);数据层面跟 `user_id` 走,未来 tgbot 加文本对话直接复用

**V1 非目标**

- 不替代 `system_prompts`(那是用户主动配置的指令)
- 不替代 `conversation.compact`(那是单会话内压缩)
- 不做图像 reference / 风格库 / 模板沉淀(交由海报工作流另议)

**V2 计划**(本设计预留接口,V1 数据底座一步到位避免 V2 时 ALTER 大表;落地见 §13 的 P4 / P5,详细见 §16 / §17):

- **主动确认**:对高频 reinforce 的强偏好,模型偶尔主动反问"按你之前说的 X 来吗?",让用户感到"它真的记得"。低打扰,多重 AND 触发条件控频
- **记忆作用域(scopes / collections)**:工作 / 个人 / 项目 多组记忆隔离,避免上下文相互污染。default scope 全局,命名 scope 仅在 active conversation 注入

## 2. 与现有机制的边界

| 概念 | 范围 | 持久化 | 来源 |
|---|---|---|---|
| `system_prompts` | 用户/管理员显式指令 | `system_prompts` 表 | 手动配置 |
| `conversation.compact` | 单会话内摘要 | `conversations` 字段 | 自动压缩 |
| `user_memory`(新) | 跨会话账号级记忆 | 独立表 | 显式 + 自动抽取 |

三者注入到 prompt 时的顺序(从上到下):

```
[system_prompts]                    管理员/用户指令(最权威)
[user_profile + user_constraints]   账号长时记忆: 身份 + 禁忌
[conversation.compact]              本会话历史摘要
[user_context]                      账号长时记忆: 相关偏好/项目 top-K
[recent messages]                   本会话最近 N 轮原文
```

profile/constraints 放在 compact 之前是为了让"我是谁、我不要什么"在本会话历史中始终生效;相关 context 放在 compact 之后是为了让它更靠近当前回合,被模型优先 attend。

## 3. 前置依赖:Provider purposes 字段改造

记忆系统会引入两类新调用:**embedding**(高频小调用) 和 **抽取**(中频小调用)。如果让它们和主对话共享同一个 provider 池,会互相挤占额度;反过来也不希望某个便宜的 embedding 中转被误用去跑对话。

解决方案:**给每个 provider 加一个用途标签字段,选号时按用途过滤**。这是记忆系统能落地的硬前置。

### 3.1 字段定义

每个 provider 配置加一个 `purposes` 字段:

```jsonc
{
  "name": "openai-main",
  "api_key": "sk-...",
  "base_url": "https://api.openai.com/v1",
  "proxy": "us-1",
  "enabled": true,

  "purposes": ["chat", "image", "embedding"]   // 新增,数组,至少 1 项
}
```

**枚举值只有 3 个:**

- `chat` — 主对话 + 自动标题 + 上下文摘要 + **记忆抽取**(GPT-5.4 mini 走这里)
- `image` — 生图
- `embedding` — 记忆向量化

**抽取归在 chat,不单列**。理由:抽取本质是一次轻量对话调用(输入文本、输出 JSON),共用 chat 资源池更简单;若未来需要硬隔离,可在 chat provider 之间用 model 名分流(高频抽取走 mini 那个号,主对话走主号)。

### 3.2 与现有探活字段的关系

`lumen_core/providers` 已有 `responses_supported / image_generations_supported / image_responses_supported` 等三态 bool —— 这是**探活检测**得到的"端点能不能用",跟本设计的 `purposes` 是两层概念,不冲突:

- `purposes` = 管理员**声明**该 provider 用于哪些用途(进入哪类选号池)
- `*_supported` = 系统**探活**得到的端点可用性(同一 purpose 池内挑健康的)

选号顺序: 先按 purposes 过滤候选 → 再按探活结果挑健康的 → 再走现有 strategy/cooldown 逻辑。

### 3.3 现存数据迁移

`SystemSetting["providers"]` JSON 中所有老 provider 默认补:

```json
"purposes": ["chat", "image"]
```

等价当前行为(老 provider 既能跑对话也能跑生图),不破坏任何现有调用。embedding 必须由管理员显式勾选才会进入 embedding 池。

### 3.4 ProviderPool.select 改动

```
旧: pool.select(route="text" | "image", avoid=...)
新: pool.select(purpose="chat" | "image" | "embedding", avoid=...)
```

兼容期保留旧 `route` 参数,内部映射:

- `route="text"` → `purpose="chat"`
- `route="image"` → `purpose="image"`

新代码直接用 `purpose=`,旧调用点逐步迁移,不一次性改完。

**选号过程:**

1. 候选集 = `enabled=true ∧ purpose ∈ purposes`
2. 排除调用方 `avoid` set + 在 cooldown 中的
3. 按现有 strategy(random / latency / failover / round_robin)挑一个
4. 全挂时降级:cooldown 不阻塞,enabled + 用途匹配即可

### 3.5 Admin UI 改动(`apps/web` 的 admin providers 页)

**provider 卡片直接展示并支持快速操作:**

1. **Purposes 三选框**(展示在卡片上,点击直接保存):
   ```
   ☑ 对话    ☑ 生图    ☐ embedding
   ```
   至少勾 1 项,前后端都校验。

2. **启用/停用 toggle**(右上角,直接切换无二次确认):
   ```
   PATCH /admin/providers/{name}/enabled  body: {"enabled": false}
   ```
   单独接口、不重提整张 providers 表,避开大对象更新和噪声 audit。

   **没有二次确认**(用户明确选择 A 方案,符合"不要繁琐"的诉求)。误操作可立即点回。

3. 其余字段(api_key / base_url / proxy / models)仍走现有编辑面板。

### 3.6 调用方迁移

| Capability | 调用点 | 现状 → 改后 |
|---|---|---|
| `chat` | `apps/worker/app/upstream.py` 主对话 / `auto_title.py` / `context_summary.py` | 现用 `route="text"`,沿用别名,不动 |
| `image` | `apps/worker/app/tasks/generation.py` | 现用 `route="image"`,沿用别名,不动 |
| `embedding` | **新增** —— 记忆入库 + query 检索 | 直接用 `purpose="embedding"` |
| 抽取 | **新增** —— 记忆 worker | 直接用 `purpose="chat"` + `model="gpt-5.4-mini"` |

S0 阶段不强制把 chat/image 调用切换到 `purpose=`,等 S2/S3 稳定后再批量替换。

---

## 4. 数据模型

### 4.1 user_memories 主表

```
user_memories
─────────────────────────────────────────────────────────────────
  id                  uuid pk
  user_id             uuid fk → users  (cascade delete)
  type                enum('profile' | 'preference' | 'avoid' | 'project')
  content             text                -- 抽取后的 fact,简短一句(< 200 字)
  source_message_id   uuid fk → messages  -- 溯源(nullable: 手动新增时为 null)
  source_excerpt      text(160)           -- 原文片段,UI 显示"从这句话学到的"
  source             enum('explicit' | 'auto' | 'manual')  -- 写入路径
  embedding           vector(3072)        -- pgvector,text-embedding-3-large
  confidence          float               -- 0..1,manual=1.0, explicit=1.0
  pinned              bool default false  -- 用户钉选,永远注入
  disabled            bool default false  -- 软关闭,不注入但保留
  positive_signal     int  default 0      -- 用户 pin/edit 等正反馈累计
  negative_signal     int  default 0      -- 用户 disable/forget 等负反馈累计
  superseded_by       uuid nullable       -- 被新版覆盖时指向新条 id (audit 链)
  last_used_at        timestamp           -- 最近一次被注入到 prompt
  scope_id            uuid fk → user_memory_scopes  -- V2 作用域;V1 全部指向该用户的 default scope
  last_confirmed_at   timestamp nullable  -- V2 主动确认上次时间;V1 暂不写入
  created_at          timestamp default now()
  updated_at          timestamp default now()
─────────────────────────────────────────────────────────────────
索引:
  idx_user_alive  (user_id, scope_id) WHERE disabled = false AND superseded_by IS NULL
  idx_user_type   (user_id, type)
  idx_embedding   USING hnsw (embedding vector_cosine_ops)   -- pgvector HNSW
```

**type 含义:**

- `profile` — 身份 / 角色 / 持久属性。例:"小红书运营"、"前端工程师"、"在做母婴品牌"
- `preference` — 正向偏好。例:"喜欢简洁文案"、"偏好 200 字以内的回答"
- `avoid` — 负向禁忌。例:"不要使用感叹号"、"不接受口号式标语"
- `project` — 在做的具体事(短期)。例:"3 月内要交付一个母婴品牌的小红书账号包装"

衰减规则不一样(见 §11),所以分开。

### 4.2 user_memory_staging 候选表

`confidence < 0.85` 的自动抽取结果先进 staging,等用户确认。

```
user_memory_staging
─────────────────────────────────────────────────────────────────
  id, user_id, type, content, source_message_id, source_excerpt,
  source='auto', embedding, confidence              -- 同主表

  decision    enum('pending' | 'accepted' | 'rejected') default 'pending'
  decided_at  timestamp nullable
  expires_at  timestamp                             -- 默认 created_at + 7d
  created_at  timestamp
─────────────────────────────────────────────────────────────────
索引: (user_id, decision)
```

7 天未决策自动 reject(后台清理 job)。accept 时复制到主表,reject 时直接删除 staging 行。

### 4.3 pgvector 扩展

api 端 db migration 启用扩展:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

向量列用 `vector(3072)`(text-embedding-3-large 维度)。HNSW 索引在数据规模 < 100 万行时性能/内存均可控。

### 4.4 user_memory_scopes(V1 仅建 default 行,V2 启用 UI)

V2 主体功能在 §17,但 V1 P0 migration 即建表 + 给每个 user 创建一条 default scope,
避免 V2 时再 ALTER user_memories 加 fk 触发大表锁。

```
user_memory_scopes
─────────────────────────────────────────────────────────────────
  id          uuid pk
  user_id     uuid fk → users  (cascade delete)
  name        text(40)            -- "default" / "工作" / "小红书运营"
  emoji       text(8) nullable
  is_default  bool default false  -- 每用户必须且仅有一条 is_default=true
  created_at  timestamp default now()
─────────────────────────────────────────────────────────────────
索引: (user_id, name) unique
约束: 每 user 仅一条 is_default=true (partial unique)
```

V1 行为:
- bootstrap 给每个 user 创建一条 `name='default', is_default=true` scope
- 所有 `user_memories.scope_id` 默认指向该 user 的 default scope id
- `conversations.active_scope_id` 列默认 null (退化为只取 default)

V2 落地后:
- 用户可创建命名 scope("工作" / "个人" / etc.)
- conversation 顶部选 active scope,注入和抽取按 scope 归类

---

## 5. 写入路径

### 5.1 显式写入(intent 识别)

**两阶段判定**:关键词只做"候选门槛",最终是否走同步入主表由 mini 二级判定。避免"我以后都要超过他""我从此再也不喝牛奶了"这类表态/抱怨被关键词误命中后直接进主表。

**第一阶段(本地正则,快速过滤)**:

```
^|[^\w](记住|永远(记得)?|总是|不要(再)?|从此|以后(都)?(不|要)|remember|always|never|don'?t|stop)
```

英文常见 directive 关键词一并入。V1 不做完整多语言支持,但中英双语命中能覆盖账号实际使用面。

**第二阶段(GPT-5.4 mini 同步抽取 + 意图分类)**:命中后,在 message handler 流程内同步追加一次 mini 抽取,prompt 中要求每条候选输出 `intent_kind` 字段:

- `directive` — 用户在给 AI 下指令("记住我不喜欢感叹号" / "以后回答不要超过 200 字" / "remember I prefer concise replies") → `source='explicit', confidence=1.0`,直接入主表,绕过 staging
- `statement` — 用户在表态/抱怨/感慨,关键词碰巧命中("我以后都不喝牛奶了" / "从此告别 996" / "I'll never trust them again") → 走正常 staging 流程,不当作显式指令

**为什么 directive 走同步**:用户说"记住 X"期望立即生效,异步会出现"刚说完记住下一条就忘了"的错觉。但 directive vs statement 二级判定避免"以后/再也/never"等模糊关键词把表态送进主表——这是用户最容易感到"它怎么记错了我"的场景。

### 5.2 自动抽取

**触发链:**

```
assistant 回完一轮
   ↓
arq.enqueue('memory_extract', conversation_id, user_msg_id, asst_msg_id)
   ↓
worker 消费(异步,不阻塞主对话)
```

**预过滤**(进 worker 后立即判断,不命中直接 return):

- 用户消息 < 30 字 → skip
- 关键词预过滤:消息须含 `我是 / 我喜欢 / 我不 / 不要 / 从来 / 永远 / 总是 / 在做 / 通常 / 一般` 任一,否则 skip
- conversation 标记 `memory_disabled = true` → skip

**抽取**:

- provider:`pool.select(purpose="chat")` + `model="gpt-5.4-mini"`
- 输出走 OpenAI structured output / JSON schema 强制格式
- 解析失败一律丢弃,不重试(避免 worker 卡死)
- 单次 input ≈ 500 token,output ≈ 200 token,成本可控

**入库决策:**

```
for candidate in extracted:
    if PII 命中:               drop
    if confidence >= 0.85:     去重/冲突判断后入主表(见 §6)
    else:                      去重判断后入 staging
```

### 5.3 抽取 prompt 设计要点

不写完整 prompt 文本,列**必须包含的要素**:

- **目标**:从单轮对话中识别长期适用的 fact / preference / project / avoid,输出严格 JSON 数组
- **type 判定标准**:每类一句话定义 + 1-2 个正例反例
- **必填字段**:`type` / `content`(简短一句,< 200 字) / `confidence`(0..1) / `source_excerpt`(从原文摘 50-120 字)
- **PII 黑名单**:电话 / 地址 / 身份证 / 邀请码 / API key / 密码 / 银行卡 / 邮箱+密码组合 / 验证码 → 一律不抽
- **时效性过滤**:"今天 / 刚才 / 这次 / 上次"开头的事件不要;"我是 / 我在做 / 我不喜欢"才存
- **空数组允许**:大多数对话回合没有记忆点,要敢于输出 `[]`(few-shot 中至少 2 条空数组示例)
- **输出 schema**:`{ "type": "array", "items": {...} }`,通过 OpenAI `response_format` 强制
- **few-shot**:5 条,覆盖正常抽取 / 空数组 / PII 拒绝 / 时效性拒绝 / 多 type 同时抽出

### 5.4 写入反馈(inline,体验关键)

抽取/入库不能"静默发生"。从用户视角,任何记忆变化都必须**在对话气泡里看得见**,否则建立不起信任,会出现"它什么时候偷偷记住我的"惊吓感。这是整个系统从"能用"走到"舒服"的关键差距。

**反馈通道**:每次 assistant 回完一轮(显式同步抽取 / 异步抽取完成回写两条路径都走这里),response envelope 追加 `memory_writes` 数组:

```jsonc
{
  "memory_writes": [
    {
      "id": "uuid",                          // 主表 id 或 staging.id
      "kind": "added"|"updated"|"merged"|"superseded"|"staged"|"rejected_pii",
      "type": "preference",
      "content": "不喜欢感叹号",
      "source_excerpt": "...原文片段...",
      "undo_token": "..."                    // 5 分钟内有效;rejected_pii / staged 时为 null
    }
  ]
}
```

异步抽取在 worker 完成时,如果用户仍在该 conversation 内,通过现有 SSE 通道追推 `memory_writes` 事件,前端追加显示在那一轮 assistant 气泡尾部。如果用户已离开,下次拉 conversation 详情时合并展示。

**前端 UI**:assistant 气泡尾部一行 12px 小字微提示,文案按 kind 区分:

| kind | 文案 | 操作 |
|---|---|---|
| `added` / `updated` | `已记下:不喜欢感叹号 · 撤销 · 管理` | 撤销 / 跳 settings |
| `merged` | `已合并到现有偏好:不喜欢感叹号 · 撤销 · 管理` | 撤销保留独立 |
| `superseded` | `已更新偏好:从"喜欢感叹号"改为"不喜欢感叹号" · 撤销` | 撤销还原老条 |
| `staged` | `想让我记住"不喜欢感叹号"吗? 是 / 否 / 详细` | 单击决策 |
| `rejected_pii` | `检测到敏感信息,未记住` | 无 |

**撤销接口**:`POST /v1/me/memories/undo body: {undo_token}` 5 分钟内有效——已入主表的回到 disabled,已 superseded 的还原老条,staged 的直接 reject。过期 token 返回 410 + 提示用户去 settings 处理。

**为什么 inline 而非通知中心**:用户注意力在对话流里,不在 settings 里。对话里看得到、撤销得了,才有"我控制着系统",而不是"系统在偷偷学我"。这一点决定了首次使用印象。

---

## 6. 冲突 / 去重 / 进化

新条入库前必须经过这一步,否则记忆库 3 个月后就是噪声。

### 6.1 流程

```
new = 抽取出的候选
   ↓
SQL 预筛: 同 user_id ∧ 同 type ∧ alive
   ↓
向量相似度: cosine(new.embedding, existing.embedding) > 0.88 的 existing
   ↓
   命中?
   ├─ 否 → 直接入库
   └─ 是 → GPT-5.4 mini 二次判断:
              - duplicate  → bump existing.updated_at, 丢弃 new
              - conflict   → existing.superseded_by = new.id, new 入库
              - complement → existing.content += " | " + new.content,
                            bump updated_at, 丢弃 new
              - independent → 都保留,new 入库
```

二次判断 prompt 给两条原文,输出 4 选 1 的 enum + 短理由。同样用 GPT-5.4 mini。

### 6.2 superseded_by 链

`superseded_by IS NOT NULL` 的不进入注入候选,但**保留可见**(用户翻 audit 能看到"这条记忆 6 月份被新偏好覆盖了")。永不级联删除,除非用户 forget 整条。

---

## 7. 检索 / 注入

### 7.1 触发点

每次用户发消息时,在 `apps/worker/app/upstream.py` 主对话调用前插入 `assemble_user_memory_prompt(user_id, user_msg, conv_id)`,返回三段文本和命中条 id 列表。

### 7.2 跳过条件

任一命中即跳过(只注入 profile + avoid,或完全跳过):

- 用户消息 < 5 字 → 跳过 query embedding,只注入 profile + avoid
- conversation `memory_disabled = true` → 完全跳过
- account `memory_paused = true` → 完全跳过

### 7.3 流程

```
1. SQL 预筛(单次 query):
   SELECT * FROM user_memories
   WHERE user_id = ?
     AND disabled = false
     AND superseded_by IS NULL
     AND scope_id IN (
       (SELECT id FROM user_memory_scopes WHERE user_id = ? AND is_default = true),
       coalesce(:active_scope_id, default_scope_id)   -- V1: active=null → 退化为 default
     )

2. 切分:
   profile_set     = type='profile'                  全集
   avoid_set       = type='avoid'                    全集
   pinned_set      = pinned = true                   全集
   candidate_set   = type IN ('preference', 'project') ∧ NOT pinned

3. query_vec = embedding(user_msg)   -- 短消息跳过此步

4. ranked_candidates =
     candidate_set
       .map(m => (m, cosine(m.embedding, query_vec) * decay_factor(m)))
       .sort_desc(score)
       .take(8)                       -- top-K

5. 三段构造(去重: pinned 与 ranked 取并集):
   <user_profile>      profile_set ∪ (pinned_set ∩ profile-like)
   <user_constraints>  avoid_set
   <user_context>      ranked_candidates ∪ (pinned_set ∩ context-like)

6. token 预算: 见 §7.5 全局预算表
   - user_profile + user_constraints 软上限 400
   - user_context 软上限 600
   超出按裁剪优先级:
     profile > avoid > pinned > 高相关 preference > project
   按相关性 / 衰减分数从尾部砍;最终全局复核见 §7.5

7. 拼接位置:
   system_prompts → user_profile → user_constraints
                  → conversation.compact (现有) → user_context → recent messages

8. 异步: UPDATE user_memories SET last_used_at = now() WHERE id IN (命中)
   (合并到 batch,每 30 秒 flush 一次,避免每轮 N 次 UPDATE)
```

### 7.4 注入反馈与可观测性(P2 即上,不放 P3)

debug 视图必须跟检索同期上线——用户第一次看到注入错的记忆才会去管理。**它是建立信任的核心入口,排到 P3 太晚**。

**response envelope 追加**:

```jsonc
{
  "used_memory_ids": ["uuid1", "uuid2"],
  "used_memory_summary": [
    { "id": "uuid1", "type": "profile",    "content": "小红书运营" },
    { "id": "uuid2", "type": "preference", "content": "不喜欢感叹号" }
  ]
}
```

**前端 UI**:assistant 气泡角落一枚 chip `🧠 用了 2 条记忆`,点击展开 popover:

```
本回合参考了:
  · 小红书运营 (profile)        — disable
  · 不喜欢感叹号 (preference)   — disable
管理全部记忆 →
```

popover 内 disable 单条立即生效,下一轮不再注入(`negative_signal++`)。注入 0 条时 chip 不显示,避免视觉噪声。

**关键交互**:这个 chip 是用户第一次发现"原来它在用我说过的话"的入口,远比 settings 里的列表更能让用户理解记忆是怎么影响回答的。

### 7.5 全局 prompt token 预算

user_memory 不是孤立的 1000 token——它跟 `system_prompts`、`conversation.compact`、`recent messages` 共用同一个 context window。需要全局预算表,避免"长会话里所有段都被挤压"。

**预算表**(以模型 max_input = 200K 为基准,内层裁剪在主对话调用前完成):

| 段 | 软上限 | 备注 |
|---|---|---|
| `system_prompts` | 2000 | 管理员配置时校验,超出 PUT 拒绝 |
| `user_profile` + `user_constraints` | 400 | profile/avoid 简短,几乎不触上限;触上限时按 confidence + pinned 排序裁尾 |
| `conversation.compact` | 4000 | 沿用现有逻辑 |
| `user_context` | 600 | 原 1000 收紧,因 profile/avoid 已分出去 |
| `recent messages` | 剩余 | 兜底,不限定 |
| **总和软上限** | **15000** | 超出时按下面优先级整体裁剪 |

**裁剪优先级**(从保留到丢弃):

```
system_prompts
  > user_profile
  > user_constraints
  > pinned memories
  > recent N=4 messages
  > conversation.compact
  > user_context (按相关性 / 衰减分数从尾部砍)
  > 更早的 messages
```

profile 在最前是因为"我是谁"丢了对话基本就废了;最近 4 条原文也是高优先,因为它们承载本回合的直接上下文。预算表上线时同步加 metric 监控,持续观察各段实际占用,超出 80% 上限的 conversation 加 trace 标记便于定位。

---

## 8. 用户控制

体验目标:**用户从不主动找设置就能管理记忆**。inline 是默认入口,settings 是兜底。

### 8.1 入口分层

| 入口 | 何时出现 | 谁会用到 |
|---|---|---|
| **assistant 气泡 inline 微提示** | 每次有写入(§5.4) | 几乎所有用户的高频路径 |
| **assistant 气泡角 chip `🧠 用了 N 条`** | 每次有注入(§7.4) | 想理解"它为什么这样回答" |
| **conversation 顶部"记忆"按钮** | 任何时候 | 想看本会话用了什么 / 临时关掉 |
| **settings → 记忆 tab** | 想批量管理 / 看 timeline / 导出 / 清空 | 进阶用户 / 周期性整理 |
| **settings 图标红点 badge** | staging 有候选 | 让用户知道"有东西等你看" |

### 8.2 inline 反馈与撤销(关键体验)

写入和注入的可见性是体验核心。详见 §5.4 / §7.4,这里汇总用户视角:

| 场景 | 用户看到 | 可操作 | 时间窗口 |
|---|---|---|---|
| 自动 / 显式抽取入主表 | `已记下:X · 撤销 · 管理` | 一键撤销 / 跳 settings | 5 分钟 |
| 偏好被覆盖 | `已更新偏好:A → B · 撤销` | 撤销还原老条 | 5 分钟 |
| 合并到已有 | `已合并:X · 撤销` | 撤销保留独立 | 5 分钟 |
| 抽取入 staging | `想让我记住"X"吗? 是 / 否 / 详细` | 单击决策 | 直到 staging 过期(7 天) |
| PII 拒抽 | `检测到敏感信息,未记住` | — | — |
| 注入命中 | 气泡角 chip `🧠 用了 N 条` | 展开看 + disable 单条 | 持续可见 |

撤销统一走 `POST /v1/me/memories/undo body: {undo_token}`,过期返回 410。

### 8.3 单条操作

| 操作 | 行为 | 信号 |
|---|---|---|
| 查看源 | 跳转源 message(`source_message_id` 不空) | — |
| 编辑 content | 直接改,自动重算 embedding | `positive_signal += 2` |
| Pin / Unpin | `pinned` 切换 | pin 时 `positive_signal++` |
| Disable / Enable | `disabled` 切换 | disable 时 `negative_signal++` |
| Forget | 软删,30 天后物理删除 | `negative_signal += 2` |

### 8.4 全局控制

| 开关 | 行为 | 跨设备同步 |
|---|---|---|
| 暂停记忆 | `memory_paused=true`:不写入新,已有继续注入 | SSE 事件 `account_settings_updated` 实时推 |
| 完全关闭 | `memory_disabled=true`:不写入也不注入 | 同上 |
| 一键清空 | 二次确认 + 输入"清空"二字 → 全部软删 | 立即多端生效 |
| 导出 | JSON: type/content/source_excerpt/created_at,不含 embedding | — |
| 本会话不用记忆 | `conversation.memory_disabled=true` | per-conversation,不广播 |

跨设备的 settings 变更通过现有 SSE 通道推 `account_settings_updated`,前端订阅后立即重渲染。最坏情况下离线设备下次拉 conversation 时刷新,不会写入冲突(user_id 唯一)。

### 8.5 staging 候选(inline 优先)

**默认路径**:在 assistant 气泡尾部以 `想让我记住"X"吗? 是 / 否 / 详细` 形式 inline 决策。这是 ChatGPT-style 让用户最少打扰地参与的方式,不该让用户翻列表。

**兜底路径**:settings 图标在 staging 有候选时挂红点 badge `(N)`;memory tab 内"建议加入记忆"列表显示所有 pending 候选,逐条 Accept / Reject / Edit-then-Accept。

候选 7 天后自动 reject(后台 cleanup),期间 inline 提示一直可点。

### 8.6 记忆 timeline(信任入口)

settings memory tab 内独立子页"最近变化",时间倒序展示所有 audit 事件:

```
2026-05-08 14:23  added       不喜欢感叹号                          ← 跳源 message
2026-05-07 09:15  superseded  喜欢感叹号 → 不喜欢感叹号               ← 跳源 + 还原老条
2026-04-30 17:42  merged      偏好简洁文案 + 文案 ≤ 200 字           ← 拆分回独立
2026-04-29 11:02  forget      由用户手动删除(剩余保留 28 天)         ← 永久删除前可恢复
```

每条可点击溯源 / 撤销(在窗口内)。这是用户回头审计"它怎么变成这样"的核心入口,也是出现注入异常时第一时间可以查的地方。ChatGPT 至今还没做好这个视图——做了就是差异化。

### 8.7 首次教育(onboarding)

体验上最容易被忽略但最影响首次印象的部分。三个一次性 tooltip,触发后写 `users.onboarding_seen` bitmap,永不重复:

| 触发 | 内容 |
|---|---|
| 第一次进 memory tab(空) | 卡片:"Lumen 会从对话里学到你的偏好,也可以手动添加。" + 一键暂停 + 1-2 条示例 |
| 第一次自动抽取入主表 / staging | tooltip 指向 inline 提示:"我从这句话学到了 X。5 分钟内可撤销,也可以在记忆 tab 管理。" |
| 第一次注入命中 | tooltip 指向 chip:"我刚才参考了你之前告诉我的 X。" |

不堆 onboarding wizard,不强制走完。任意一处的"知道了"或滑过都视为已读。

### 8.8 主动确认开关(V2,详 §16)

settings 记忆 tab 加 toggle `主动确认偏好`(默认开,V2 起量后视数据决定),关掉后模型永远不主动反问。
被主动确认的 assistant 气泡左上角加微标 ✨,hover 显示「基于你的记忆「X」」;
气泡底部 inline 三按钮 `[ 是 / 不是 / 这次不用 ]` 让用户单击反馈,免去 mini 解析。

### 8.9 作用域管理(V2,详 §17)

| 入口 | 行为 |
|---|---|
| conversation 顶部 scope 选择器 | `[ 默认 ▾ ]` 切换 / 新建 / 管理。切换立即影响下一回合,不影响已生成内容 |
| inline 写入提示扩展(§5.4) | `已记下:X [作用域: 默认 ▾] · 撤销`,默认是 conv 当前 scope,可即时改归类 |
| settings memory tab 左侧 sidebar | scope 列表 + 条数 + emoji,点击切换查看;支持重命名 / 删除(删除时迁回 default) |
| All 视图 | 跨 scope 搜索 + 批量改归类 |

切换 scope 时 inline hint:"已切到「工作」,接下来会用工作 scope 的记忆"。删除 scope 时二次确认 + 数字提示"15 条记忆将迁到 default"。

---

## 9. API 路由

```
# 用户记忆 CRUD
GET    /v1/me/memories                            list (filter by type/pinned/disabled)
POST   /v1/me/memories                            手动新增 (source='manual', confidence=1.0)
PATCH  /v1/me/memories/:id                        edit/pin/disable/enable
DELETE /v1/me/memories/:id                        forget (软删)
POST   /v1/me/memories/undo                       {undo_token}: 5 分钟内撤销最近一次写入(§5.4)
GET    /v1/me/memories/timeline                   audit 事件流(分页,§8.6)

# Staging
GET    /v1/me/memories/staging                    待确认列表
POST   /v1/me/memories/staging/:id/accept         接受 → 复制到主表
POST   /v1/me/memories/staging/:id/reject         拒绝 → 删除 staging
PATCH  /v1/me/memories/staging/:id                edit then accept

# 全局
GET    /v1/me/memories/export                     JSON 导出
DELETE /v1/me/memories                            一键清空(需 confirmation header)
PATCH  /v1/me/memory-settings                     {paused, disabled}
PATCH  /v1/me/onboarding-seen                     {flag}: 一次性 onboarding 位图(§8.7)

# Per-conversation
PATCH  /v1/conversations/:id/memory-disabled      {disabled: bool}
PATCH  /v1/conversations/:id/active-scope         {scope_id}: V2 切换作用域
GET    /v1/conversations/:id/used-memories        debug: 最近一次注入的记忆 id + summary

# Scopes (V2)
GET    /v1/me/memory-scopes                       list scopes + 每个 scope 的条数
POST   /v1/me/memory-scopes                       {name, emoji}: 新建
PATCH  /v1/me/memory-scopes/:id                   重命名 / 改 emoji
DELETE /v1/me/memory-scopes/:id                   删除(records 迁回 default,is_default 不可删)
PATCH  /v1/me/memories/:id/scope                  {scope_id}: 改某条记忆的归属 scope

# Confirmation feedback (V2)
POST   /v1/me/memories/:id/confirm                {decision: 'yes'|'no'|'skip'}: 用户对主动确认的反馈

# Admin (provider purposes 改造)
PATCH  /admin/providers/:name/enabled             {enabled: bool} 单字段切换
# 现有 PUT /admin/providers 保留,接收新增的 purposes 字段
```

---

## 10. 反馈回路

让记忆系统从"能用"走向"惊艳"的关键。

### 10.1 信号收集

| 用户行为 | 信号 |
|---|---|
| Pin | `positive_signal++` |
| Edit | `positive_signal += 2` |
| Disable | `negative_signal++` |
| Forget | `negative_signal += 2`(强负向) |
| 命中注入 | `last_used_at = now()`(不改 score) |

### 10.2 应用到抽取

抽取阶段查询用户 `negative_signal` 高的同 type 主题,作为 prompt 上下文提示模型"这个用户对这类内容容忍度低,提高置信度阈值"。

简化做法:每用户维护一个 `extraction_threshold` 字段,初始 0.85;每条 forget 让它 `+= 0.02`,每条 pin 让它 `-= 0.01`,clamp 到 [0.7, 0.95]。

### 10.3 应用到注入

候选排序的 score 计算:

```
score = cosine_similarity * (1 + 0.1 * positive_signal - 0.15 * negative_signal) * decay
```

正反馈高的同等相关性下排前面;负反馈高的虽未删除但下沉。

---

## 11. 衰减 / 过期

### 11.1 不删除,只降权

衰减只影响**注入排序**,不自动删除任何条目。用户主动 forget 才会软删。

### 11.2 衰减曲线(per type)

```
days = (now - last_used_at).days
decay_factor =
   profile   → 1.0                       (身份长期有效,不衰减)
   avoid     → 1.0                       (禁忌长期有效)
   preference → exp(-days / 90)          (90 天减到 1/e ≈ 0.37)
   project   → exp(-days / 30)           (30 天减到 0.37)
```

未被注入时 `last_used_at = created_at`。pinned 强制 `decay_factor = 1.0`。

### 11.3 staging 自动过期

`expires_at < now()` 的 staging 行,后台 job 标记 `decision='rejected'` + 删除。每天跑一次。

---

## 12. 安全与隐私

- **PII 黑名单**(抽取 prompt 强约束):电话、地址、身份证、邀请码、API key、密码、银行卡、邮箱+密码组合、验证码、信用卡号 — 一律不抽,即使用户明确说"记住我的密码 X"也不抽(返回拒绝消息给用户)
- **DB 列加密**:V1 不做。如果未来有合规要求(GDPR / 个人信息保护法),`content` 列可加 application-level 加密,key 走 KMS
- **Admin 不可见 user 记忆内容**:admin 端只能看条数 / 时间分布 / type 分布,**不能读 `content`**;后端按角色严格限制 `/v1/me/memories` 永不暴露给 admin 视角
- **删账户级联**:`ON DELETE CASCADE` —— 删用户时 `user_memories` + `user_memory_staging` + 相关 audit 全部清理
- **导出**:JSON 包含 type / content / source_excerpt / created_at;**不含 embedding**(向量是内部计算产物,无独立价值)
- **审计**:所有 admin 操作走现有 `audit` 表;user 自己对记忆的操作也写一份 audit(用于"我什么时候删的这条")

---

## 13. 落地阶段

### S0 — Provider purposes 改造(前置,1-2 天)

**后端:**

1. `lumen_core/providers` schema 加 `purposes: list[str]`,默认 `["chat", "image"]`
2. `validate_providers` 校验 purposes 至少 1 项 + 枚举值合法
3. `apps/api/app/routes/providers.py` PUT 接收 purposes;新增 `PATCH /admin/providers/{name}/enabled` 单字段切换
4. `apps/worker/app/provider_pool.py` `ProviderPool.select` 加 `purpose` 参数;旧 `route` 转译为 `purpose`
5. SystemSetting providers 现存数据 migrator(在 bootstrap 启动时检测 + 补默认值)

**前端(`apps/web` admin providers 页):**

6. provider 卡片加 purposes 三选框,本地变更立即 PUT
7. provider 卡片右上角 enabled toggle,点击直接 PATCH,**无二次确认**
8. 卡片 UI 显示当前 purposes 标签和 enabled 状态

**验收:**

- 加一个 `purposes=["embedding"]` 的 provider,主对话调用不会选到它
- 在卡片上点停用,无弹窗,立即生效
- 老 provider 不动配置依然能跑

### P0 — 记忆数据底座 + 手动管理(2-3 天)

**Migration(V1 + V2 预留一次到位,避免 V2 时 ALTER 大表):**

1. `CREATE EXTENSION IF NOT EXISTS vector;`
2. `user_memory_scopes` 表 + 给每个现存 user 创建一条 `name='default', is_default=true` scope(§4.4)
3. `user_memories` 表 + 索引,**包含 V2 预留列** `scope_id`(NOT NULL,默认指向该 user 的 default scope) + `last_confirmed_at`(nullable)
4. `user_memory_staging` 表 + 索引(同样含 `scope_id`)
5. `users` 表加 `memory_paused`、`memory_disabled`、`extraction_threshold`、`onboarding_seen`、`confirmation_enabled`(V2 默认 OFF) 五列
6. `conversations` 表加 `memory_disabled` 列 + `active_scope_id` (V2 预留,V1 永远 null)
7. `memory_audit` 表(§14):记录 added/updated/merged/superseded/forget 事件,timeline 用

**API:**

8. `apps/api/app/routes/memories.py` 完整 CRUD(list/post/patch/delete + staging accept/reject + export + clear)
9. `PATCH /v1/me/memory-settings`、`PATCH /v1/conversations/:id/memory-disabled`

**前端:**

10. `apps/web` settings 页加"记忆"tab:list 表格 / 单条编辑抽屉 / pin / disable / forget / 导出 / 清空(二次确认)
11. conversation 顶部加"记忆"按钮 + 抽屉视图

**验收:**

- 用户能手动添加一条 profile,在新 conversation 里(P2 接入后)被注入
- 用户能 disable / pin / forget,行为正确
- 一键清空有二次确认且生效
- 数据库:每个 user 都有一条 default scope;新建 user 触发器自动建 default
- V2 预留列存在但不暴露 UI,行为与 V1 一致

### P1 — 写入闭环(3-4 天)

1. `apps/worker/app/tasks/memory_extraction.py` arq job
2. extraction prompt 模板 + JSON schema(GPT-5.4 mini,走 `purpose="chat"` + 显式 model 名)
3. 显式 intent 关键词正则 → message handler 同步追加抽取
4. embedding 入库(走 `purpose="embedding"` + `text-embedding-3-large`)
5. 去重 / 冲突 / 进化(GPT-5.4 mini 二次判断)
6. staging 候选 + UI "建议加入记忆"列表
7. accept/reject UI

**验收:**

- 用户说"我是小红书运营"→ 5 秒内 staging 出现该候选 + 气泡尾部 inline `想让我记住吗`(或 confidence 高直接入主表 + `已记下` 提示)
- 用户说"记住:不要用感叹号" → 立即在主表(显式同步,intent_kind=directive) + 气泡尾部 `已记下:不喜欢感叹号 · 撤销 · 管理`
- 用户说"我以后都不喝牛奶了" 不被误当指令(intent_kind=statement,走 staging)
- 5 分钟内点 inline 撤销可一键回滚;过期返回 410
- 同一类偏好说两次,第二次会 merge + 气泡显示 `已合并:X · 撤销`
- 偏好反转 → 老条 superseded + 气泡显示 `已更新偏好:A → B · 撤销`
- PII 内容("我密码是 123")→ 气泡显示 `检测到敏感信息,未记住`,主表无该条

### P2 — 检索注入闭环 + 可观测性(3-4 天)

1. `assemble_user_memory_prompt` 实现(`apps/worker/app/upstream.py` 调用前)
2. 三段式构造 + 全局 token 预算裁剪(§7.5)
3. embedding query + top-K + 衰减分数
4. `last_used_at` 异步批量更新(每 30 秒 flush)
5. 本会话开关 / 全局开关接入,settings 变更走 SSE `account_settings_updated` 跨设备同步
6. response 附带 `used_memory_ids` + `used_memory_summary`
7. **前端 chip + popover(§7.4)**:气泡角落 `🧠 用了 N 条记忆`,展开 disable 单条
8. conversation 顶部"记忆"按钮 + 抽屉(本会话已注入 / 临时关闭)

**验收:**

- 已有"喜欢简洁文案"的用户,新 conversation 第一句话明显风格收敛
- 气泡角 chip 显示 `🧠 用了 X 条记忆`,展开能看到具体内容并 disable
- 注入 0 条时 chip 不出现
- 关闭"本会话不用记忆"开关后,响应风格回到中性,chip 消失
- A 设备改记忆开关,B 设备 1-3 秒内 UI 同步;离线 B 设备下次拉数据时刷新
- token 预算总和超 80% 上限的 conversation 在 metric 里能被定位

### P3 — 质感打磨(3-4 天)

1. 反馈回路接入:positive/negative_signal 影响排序 + extraction_threshold 自适应
2. 衰减排序按 type 应用
3. staging 7 天过期 + 主表 forget 30 天物理删除 cleanup job
4. 删账户级联清理
5. **memory timeline 子页(§8.6)**:audit 事件倒序 + 跳源 + 还原老条
6. **onboarding 三个一次性 tooltip(§8.7)**:空状态卡片 / 第一次抽取 / 第一次注入
7. **inline 反馈节流**:同 conversation 5 分钟内 ≥ 3 条同 type 写入合并显示 `已记下 3 条偏好 · 查看`,避免高密度对话刷屏
8. PII 不可见性单元测试:断言 admin endpoint 永不返回 user_memories.content

**验收:**

- forget 一条 preference 后,同主题再次说出同样的偏好,抽取阈值升高(staging 而非直接入主表)
- 90 天未用的 preference 在排序中下沉
- 删账户后 user_memories + audit + staging 全清
- timeline 能看到所有 added/updated/merged/superseded/forget 事件并跳源
- 新账户首次进 memory tab / 首次抽取 / 首次注入分别看到对应 tooltip,关闭后不再出现
- 短时间内连续 5 条偏好抽取,inline 显示折叠成"已记下 N 条 · 查看",不会刷满气泡尾部

### P4 — 主动确认(V2,2-3 天)

详细设计见 §16。

1. preference / avoid 利用 V1 已预留的 `last_confirmed_at` 列
2. `users.confirmation_enabled` 列(默认 true) + 每周触发次数 metric
3. 注入流程加 §16.1 多重 AND 触发判断,挑 ≤ 1 条 confirmation_candidate;往 system prompt 追加确认元指令
4. 用户下一轮回复用 mini 二分类(yes / no / skip),写回 signal + last_confirmed_at;按钮直点免去 mini
5. UI:主动确认气泡加 ✨ 微标 + `[ 是 / 不是 / 这次不用 ]` 三按钮,hover 显示来源记忆
6. settings 加全局 toggle `主动确认偏好`
7. metric:per-user 每周主动确认次数,超 P95 阈值告警;每用户每周硬上限 5 次

**验收:**

- 用户多次说过"喜欢简洁"(positive_signal ≥ 3),14 天后开放性创作回合中模型偶尔主动确认一次
- 同一会话同日不会被问 ≥ 2 次;同一记忆同会话永不二次确认
- 用户答"不用了"该会话不再注入该条偏好且 negative_signal += 2
- 关闭全局 toggle 后永不触发
- 当前消息含 directive 关键词("记住...")时,跳过主动确认
- 用户每周主动确认次数 ≤ 5

### P5 — 记忆作用域(V2,3-4 天)

详细设计见 §17。

1. UI:conversation 顶部 scope 选择器 + 新建对话框 + 切换 hint
2. UI:inline 写入提示加 scope 选择器(§5.4 扩展);默认 conv 当前 scope,mini 推荐 `recommended_scope` 时高亮
3. UI:settings memory tab 左侧 sidebar scope 列表 + 切换视图 + 重命名 / emoji / 删除
4. UI:All 视图跨 scope 搜索 + 批量改归类
5. 后端 scope CRUD 接口 + 删除时记忆迁回 default(`UPDATE user_memories SET scope_id=default WHERE scope_id=:dead`)
6. 注入 SQL 启用 `scope_id IN (default, active)` 过滤(V1 已预留,V2 启用 active)
7. 抽取 prompt 加 conv 当前 scope hint;候选 metadata 含 `recommended_scope`
8. system prompt 写入弱 hint:"本会话上下文领域: {scope.name}",归到 system_prompts 段不占 user_memory 预算
9. SSE 同步:scope 增删改广播 `account_settings_updated`,active_scope 切换走 conversation 事件

**验收:**

- 创建 "工作" scope,在该 conv 抽取的偏好只在 "工作" scope conv 注入
- "默认" scope 的记忆在所有 scope 都能注入(全局)
- 切 conv 的 scope 立即影响下一回合注入,旧记忆与已生成内容不动
- 删除一个 scope,该 scope 下所有记忆迁到 default,无丢失;is_default scope 不可删
- A 设备改 scope,B 设备 1-3 秒内 SSE 同步
- 跨 scope 搜索能找到任何 scope 的记忆,可批量改归类

---

## 14. 变更影响面

| 模块 | 改动 |
|---|---|
| `apps/api/app/routes/providers.py` | + `PATCH /enabled`;接受 `purposes` |
| `apps/api/app/routes/memories.py`(新) | 完整 CRUD + staging + `POST /undo` + `GET /timeline` + `PATCH /onboarding-seen` |
| `apps/api/app/routes/conversations.py` | + memory_disabled 字段 + 注入命中暴露(used_memory_ids + summary) |
| `apps/api/app/db.py` | + `UserMemory` / `UserMemoryStaging` / `MemoryAudit` model + `users.onboarding_seen` bitmap 列 + `users.memory_paused/disabled/extraction_threshold` 列 |
| `apps/api/app/scripts/bootstrap.py` | 启用 vector 扩展 + 表 + provider purposes 默认值 migrator |
| `apps/api/app/sse.py`(或现有 SSE 通道) | + `account_settings_updated` 事件 + `memory_writes` 事件 |
| `apps/worker/app/provider_pool.py` | `select` 加 `purpose`;`route` 转译 |
| `apps/worker/app/tasks/memory_extraction.py`(新) | 抽取 + 去重 worker;完成后通过 SSE 推 `memory_writes` 给在线用户 |
| `apps/worker/app/upstream.py` | 接入 `assemble_user_memory_prompt` + 全局 token 预算裁剪 |
| `apps/web/src/...admin/providers/...` | + 三选框 + 启停 toggle |
| `apps/web/src/...settings/memory/...`(新) | 记忆管理 tab + timeline 子页(§8.6) + onboarding 卡片(§8.7) |
| `apps/web/src/...conversation/...` | + 气泡 inline 写入提示(§5.4) + 注入 chip + popover(§7.4) + 记忆抽屉 + 本会话开关 |
| `apps/web/src/...common/...` | + onboarding tooltip 复用组件 + undo toast |
| `apps/api/app/routes/scopes.py`(V2 新) | scope CRUD + per-conversation active_scope |
| `apps/api/app/db.py`(V2 增量) | + `MemoryScope` model + `users.confirmation_enabled` 列 + `conversations.active_scope_id` 列 + `user_memories.scope_id / last_confirmed_at` 列(V1 即建,V2 启用) |
| `apps/worker/app/upstream.py`(V2 增量) | + 主动确认元指令注入 + scope 过滤 + `pending_confirmation` 状态机 |
| `apps/worker/app/tasks/memory_extraction.py`(V2 增量) | + scope 归类 hint + recommended_scope metadata + 用户回复 yes/no/skip 二分类 |
| `apps/web/src/...conversation/...`(V2 增量) | + scope 选择器 + 主动确认 ✨ 微标 + 三按钮 |
| `apps/web/src/...settings/memory/...`(V2 增量) | + scope sidebar + 主动确认 toggle + 跨 scope 搜索 + 批量改归类 |
| `lumen_core/providers` | schema 加 purposes |

---

## 15. 待定问题与风险

1. **embedding-3-large 成本**:管理员手上的中转报价 ¥0.005/次偏贵(官方价 ~¥0.00005/次)。内测阶段(< 100 对话/天)月开销 < ¥20 可接受;起量后再切 small 或自部署 BGE-M3 / Qwen3-Embedding。`purposes=["embedding"]` 字段提前留好,切换时只改 provider 配置,代码不动
2. **GPT-5.4 mini 的 JSON schema 稳定性**:需要在抽取上线前用 100+ 条真实对话做评测;如有解析失败率 > 2%,加 `json_object` 兜底或换 Sonnet 4.6 + 结构化引导
3. **多语言**:V1 关键词正则中英双语,抽取 prompt 以中文 system 主导但 few-shot 加 1 条英文样例;非中英语种(日 / 西 / 法等)V1 不保证质量,起量后单独评测
4. **生产数据回填**:已有 conversation 历史是否回填抽取?V1 **不回填**(成本 + 隐私两难);用户从启用之日起开始积累
5. **删除合规**:用户要求"忘记我"时,30 天物理删除窗口可能不符合 GDPR 的"立即删除"要求;若上线区域含欧盟需收紧到 7 天或立即
6. **tgbot 复用**:数据层共享,但 tgbot 当前只生图,等加文本对话再上 UI;不阻塞本设计
7. **冲突判断成本**:每条新候选都跑一次 GPT-5.4 mini 二次判断会增加抽取成本约 50%;若 staging 队列长,可批量(一次 prompt 处理 5 条)摊销
8. **记忆库膨胀**:单用户记忆数 > 1000 条时,top-K 检索质量下降。届时加 type-quota(profile ≤ 30 / preference ≤ 200 / avoid ≤ 50 / project ≤ 50),超出时按衰减分数物理删除最低分
9. **跨设备同步语义**:settings 改动通过 SSE 广播 `account_settings_updated`,所有在线设备订阅。离线设备下次拉数据时刷新。极端场景:A 设备暂停写入与 B 设备 inline 接受 staging 几乎同时发生,以服务端时间戳为准 last-write-wins,不会写入冲突(user_id 唯一);用户视角最坏只是"我刚点的接受没生效",P3 metric 监控该窗口
10. **inline 反馈刷屏**:连续多轮高密度对话每条都有"已记下"会噪。P3 节流:同 conversation 5 分钟内 ≥ 3 条同 type 写入合并成 `已记下 3 条偏好 · 查看` 折叠态;点击展开见全部
11. **撤销 token 丢失**:用户跨设备 A 看到 inline,B 看不到 token——undo 仅在 A 设备 5 分钟内有效。这是可接受的简化(token 本就 5 分钟过期,不持久化到 DB);用户跨设备需求时走 timeline 还原
12. **首次抽取的"惊吓感"**:即便有 inline 提示,首次发现"AI 在记我说的话"仍可能有隐私顾虑。§8.7 onboarding tooltip 第一次抽取必触发,且 settings 默认提供"暂停记忆"一键开关。文档/官网 onboarding 页同步说明数据不出账号边界
13. **主动确认打扰风险(V2)**:触发条件激进会让对话变啰嗦。§16.1 多重 AND + 每周硬上限 5 次是控频底线;生产 metric 监控 per-user 每周主动确认次数,超 P95 调阈值。首期可考虑 toggle 默认 OFF,跑两周数据后再决定默认值
14. **scope 过度细分(V2)**:用户可能创建过多 scope("学习""工作""家庭""购物"...),反而让管理变累。UI 软性引导:scope > 5 时显示 hint "更多 scope 不一定让记忆更准";不强制限制
15. **scope 跨界泄露(V2)**:有些通用偏好(如"不喜欢感叹号")用户希望全局生效。规则:V2 默认抽取归当前 scope,但 inline 写入提示让用户即改"作用域: 默认"升到全局。文档强调这是组织维度,非加密分区
16. **scope 切换的歧义(V2)**:对话进行中切 scope,前面已经基于 A scope 记忆生成的内容怎么办?规则:切换只影响下一回合注入,已生成内容不变;UI 切换时 inline hint 让用户清楚边界。极端场景:用户切完 scope 后说"按上面的修改" — 模型上下文里仍有前文,行为正确
17. **scope 与 conversation.compact 的协同(V2)**:同一 conversation 跨多个 scope 切换后,compact 会包含跨 scope 信息。这是预期行为(compact 是会话内压缩,不分 scope);若用户希望"严格隔离",指引使用新建 conversation 而非切 scope

---

## 16. V2:主动确认

让记忆从"默默生效"升级为"被觉察"。模型在适当场景主动反问一次"按你之前说的 X 来吗?",让用户感到"它真的记得",同时不打扰。

### 16.1 触发条件(AND 关系,全部满足才触发)

- conversation 内本日内未曾主动确认过(per-day, per-conversation 节流)
- 命中的某条 memory 满足:
  - `type IN ('preference', 'avoid')`
  - `positive_signal >= 3`(用户已正向 reinforce 过)
  - `last_confirmed_at` 为空 或 距今 ≥ 14 天
  - 本次相关性 cosine ≥ 0.92(高度相关,不是擦边)
- 当前 query 形态是"开放性创作"(assistant 即将生成内容,而非闭合问答 / 工具调用)
- 当前消息**不含** §5.1 的 directive 关键词(用户已给出明确指令时不应被反问)
- 用户全局 toggle `confirmation_enabled = true`
- 用户本周主动确认次数 < 5(硬上限)

任一条件不满足 → 不主动确认,默默应用,跟 V1 行为一致。

### 16.2 实现

在 §7.3 流程的步骤 7 之前插入:

```
6.5 confirmation_candidates =
       used_memories
         .filter(满足 §16.1 全部条件)
         .sort_desc(positive_signal)
         .take(1)                       -- 一次最多一条;条件相同选 positive_signal 最高

     if confirmation_candidates 非空:
       system_prompt 追加一段元指令:
         "如果用户问题与用户偏好「{content}」相关,请在回答开头用一句话简短确认:
          『按你之前提到的 {content} 来吗?』再继续回答。
          不要重复说明该偏好,不要解释为什么记得。"
       记录 pending_confirmation = {memory_id, conversation_id, ts}
```

### 16.3 用户反馈解析

下一轮用户回复时,如果存在 pending_confirmation:

**优先**:用户点 inline 三按钮(§16.5)直接 POST 到 `/v1/me/memories/:id/confirm`,跳过 mini

**兜底**:用户用自然语言回复,worker 异步调 mini 二分类,产出三选 enum:

- `yes`(对 / 嗯 / 继续 / yes / 沉默继续创作) → `positive_signal++; last_confirmed_at = now()`
- `no`(不要 / 换 / 不用了 / no) → `negative_signal += 2`;当前会话临时 disable 该条 memory
- `skip`(用户答非所问转聊别的) → 不计入信号,但 `last_confirmed_at = now()` 防止下一轮再问

### 16.4 全局开关 + 节流

- settings 记忆 tab toggle `主动确认偏好`(首期建议默认 OFF,跑两周数据再决定开)
- 每用户每周主动确认 ≤ 5 次硬上限,后端 metric;超出本周不再触发
- 同一条 memory 同一 conversation 永不二次主动确认(`last_confirmed_at` per memory + per conv 控制)

### 16.5 UI

主动确认句出现时,assistant 气泡左上角加微标 ✨,hover 显示「基于你的记忆「{content}」」;气泡底部 inline 三按钮:

| 按钮 | 行为 |
|---|---|
| 是 | `POST /confirm {decision:'yes'}` → `positive_signal++` |
| 不是 | `decision:'no'` → 本会话临时 disable;inline 折叠为「已记录,本会话不再使用」 |
| 这次不用 | `decision:'skip'` → 只影响本回合,不改信号 |

按钮点击后 inline 折叠为微提示 `已确认 / 已忽略`。

### 16.6 边界与风险

- **打扰感**:§16.1 多重 AND + 每周 5 次硬上限 + 默认 OFF 是三重控频
- **多记忆争抢**:一回合可能多条命中,`take(1)` + `positive_signal` 排序裁掉余下
- **冷启动**:V2 上线时无人 `positive_signal >= 3`,首批用户体验跟 V1 一致;随 reinforce 累积逐步触发,符合"逐步学懂你"的体感
- **绕过 directive**:§16.1 已保障当前消息含 directive 关键词时不触发

---

## 17. V2:记忆作用域(collections / scopes)

让用户在不同领域的记忆相互隔离。工作时要简洁专业,个人写作时要有温度——全局记忆库会让两者污染,scope 把它们分开。

### 17.1 概念

每条记忆挂一个作用域标签 `scope_id`。conversation 选一个 active scope,注入时只取 (`scope=default` OR `scope=active_scope`) 的记忆。

- `default` scope 是"全局" — 任何 conversation 都注入,适合放跨领域偏好(如"不喜欢感叹号")
- 其它命名 scope 是"领域" — 只在 active 时注入(如"工作" / "小红书运营")

### 17.2 数据模型

V1 P0 migration 已建表(§4.4),V2 仅启用 UI + 注入过滤 + 抽取归类。

- `user_memory_scopes`(详见 §4.4)
- `user_memories.scope_id`(V1 全部指向 default;V2 起可改归类)
- `conversations.active_scope_id`(V1 永远 null;V2 起可设)

### 17.3 注入时过滤

§7.3 流程 SQL 预筛已预留(详见 §7.3 步骤 1):

```sql
WHERE scope_id IN (
  default_scope_id,
  coalesce(:active_scope_id, default_scope_id)
)
```

V1 active=null 时退化为只取 default,行为不变。V2 启用 active 后自动按 scope 过滤。

### 17.4 抽取时归类

抽取出的候选默认归到 `conv.active_scope_id`(若 null 则 default)。mini 抽取 prompt 加 conv 当前 scope 作为 hint,候选 metadata 含 `recommended_scope`(模型根据上下文推荐,如检测到"小红书"上下文 → 推荐 scope='小红书运营");inline staging 候选展示时让用户 accept + 决定是否改归类。

### 17.5 UI

**conversation 顶部 scope 选择器**:

```
[ 默认 ▾ ]   ← 下拉切换 / 新建 / 管理
```

切换立即影响下一回合注入和抽取归类;**已生成的内容不变**(避免历史回溯歧义)。切换时 inline hint:"已切到「工作」,接下来会用工作 scope 的记忆"。

**inline 写入提示扩展(§5.4)**:

```
已记下:不喜欢感叹号  [作用域: 默认 ▾]  · 撤销 · 管理
```

默认是 conv 当前 scope,可即时改归类。mini 推荐的 `recommended_scope` 高亮为推荐项。

**settings memory tab 加 scope 视图**:

- 左侧 sidebar:`[ All / Default / 工作 / 小红书运营 / + 新建 ]`,每个 scope 显示条数 + emoji
- 点击切换:右侧只显示该 scope 的记忆
- 操作:重命名 / 改 emoji / 删除(删除时该 scope 下记忆迁回 default,is_default 不可删)
- All 视图:跨 scope 搜索 + 批量改归类

### 17.6 与 system prompt 协同

active_scope.name 作为弱 hint 写入 system prompt:

```
本会话上下文领域: 小红书运营
```

让模型对该领域的回应风格、术语更聚焦。这一行归到 `system_prompts` 段(详见 §7.5 预算表),不计入 `user_memory` 的 600 token 上限。

### 17.7 边界与风险

详见 §15 的风险 14-17。核心点:

- **过度细分**:UI 软性引导 scope > 5 时 hint;不强制
- **跨界泄露**:inline 写入提示支持即改"作用域: 默认"升到全局
- **切换歧义**:只影响下一回合;UI 切换时 inline hint 让用户清楚
- **scope 与 compact**:跨 scope 切换后 compact 不分 scope(预期行为);严格隔离用新 conversation
- **删除 scope 数据迁移**:必须迁回 default,UI 给二次确认 + 数字提示"15 条记忆将迁到 default"

---

## 18. 与现有 docs 的关系

- 不修改 `docs/DESIGN.md`(主架构)
- 不影响 `docs/poster-design-workflow.md`(海报工作流仍为图像偏好沉淀)
- 引用现有的 `system_prompts` / `conversations.compact` 机制,不重复设计

**Release 计划:**

- S0 (provider purposes 改造) + P0 (记忆数据底座) → 合并成一个 release 推上,顺手把 V2 预留列(scope_id / last_confirmed_at / active_scope_id / confirmation_enabled)和 user_memory_scopes 表 + 每用户的 default scope 一次性建好
- P1 / P2 / P3 (V1 闭环) → 各自独立 release,互不阻塞
- P4 (主动确认) / P5 (作用域 UI) → V2 阶段,V1 跑稳后 1-2 周观察数据再启动;P4 / P5 可并行(影响面不重叠)
