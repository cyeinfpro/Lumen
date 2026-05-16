# 服饰模特展示图自然场景生成设计文档

> 目标：让服饰模特展示图从“设计感摆拍”升级为“真实拍摄出来的一组自然穿搭图”。既保证衣服还原准确，又让场景、机位、动作、道具和氛围按衣服动态适配，避免重复、僵硬和模板感。

## 1. 背景与样张观察

本次分析的参考目录：`/Users/liangchanghua/Downloads/别人示例/`，共 9 张图。

样张内容可以分成两类：

1. 商品原图：蓝色格纹宽松长袖衬衫，白底，重点细节包括蓝色细格纹、宽松版型、胸袋、胸前小标识、纽扣、领口刺绣/装饰、胸前圆章。
2. 成品图：同一位金发卷发年轻女性模特穿同一件蓝色格纹衬衫，搭配蓝色棒球帽、深色宽松长裤、黑鞋，部分图里有法斗和牵引绳，场景从停车场低角度、城市街头、斑马线俯拍、墙边站姿、车站站台、咖啡店街道到手机/饮料/托特包等日常动作。

这些图“真实、自然、好看”的关键不是单纯换背景，而是：

- 同一套穿搭形成连续拍摄感：人物、衣服、帽子、裤子、鞋和狗这个辅助角色有稳定性。
- 每张图都有具体事件：遛狗、等车、过马路、靠墙休息、拿咖啡、看手机、回头看镜头。
- 机位差异大：低角度近距离、俯拍、半身正面、背面回头、远一点的街拍全身。
- 光线和环境有真实缺陷：街道阴影、逆光、斑马线纹理、车辆虚化、站牌/招牌/路灯、背景行人。
- 商品仍然是主角：衬衫面积大，纹理、领口、胸袋、纽扣和胸前装饰大多清楚。

当前成品图“死板”的根因通常是：

- prompt 只描述“自然街拍/生活感”，没有生成一张图背后的具体事件。
- 场景、动作、机位是分散短句，缺少统一的场景计划。
- 多张图之间只随机 shot type，没有防止同质化的 scene fingerprint。
- 为了商品还原，prompt 过度保守，导致动作幅度、环境细节和生活道具被压平。
- 为了自然丰富，又容易让模型改衣服、加图案、遮挡商品。

## 2. 当前系统现状

后端入口：`apps/api/app/routes/workflows.py::create_showcase_images`

当前链路：

1. `product_analysis` 从商品图抽取 `category / must_preserve / styling_recommendations / background_recommendation / risks`。
2. 用户确认模特候选。
3. `create_showcase_images` 根据模板、张数、年龄段选择 shot variants。
4. `_showcase_prompt()` 拼出最终 image-to-image prompt。
5. 每张图单独创建一个 image generation task，引用商品图和模特图/配饰图。

已有优点：

- 商品 1:1 还原约束已经放在最高优先级。
- 已有 `white_ecommerce / premium_studio / urban_commute / lifestyle / daily_snapshot / natural_phone_snapshot / social_seed` 多模板。
- 已有 `SHOT_POOL`，能按张数分配正面、自然动作、半身细节、侧背面。
- 已有室内/室外开关，生活化模板会切换户外场景。
- 已经在 prompt 中强调真实皮肤、自然光、不要 AI 美颜脸。

主要短板：

- `SHOT_POOL` 是“动作短标签池”，不是“场景叙事计划”。它只能说“街边站定/咖啡店门口”，不能组合出“在东京街角遛狗、狗也戴同色帽、模特俯身伸手遮镜头”这种完整画面。
- `scene_environment` 只有 `indoor/outdoor`，粒度太粗。
- 同一批图没有全局世界观，没有稳定的辅助元素，也没有跨图去重。
- prompt 里有固定“50mm 标准焦段、平视或胸口高度机位”，会抑制样张里很有效的低角度、俯拍、手机近距离和动感镜头。
- “单人照”会阻止宠物、朋友视角、路人背景等自然生活元素。应该改成“主角只有一个模特；可有宠物/远处路人，但不得抢商品”。

## 3. 设计目标

### 3.1 效果目标

- 每批 4/8/16 张像同一次真实拍摄里的精选图，而不是 4 张模板图。
- 场景根据衣服自动适配：衬衫、卫衣、外套、裙装、童装、运动服、睡衣、礼服要进入不同的生活场景。
- 单张图有自然事件：走路、等车、拿咖啡、遛狗、开门、坐在窗边、整理袖口、看手机、回头。
- 多张图不重复：地点、机位、动作、道具、远近景至少有 2-3 个维度不同。
- 商品准确：颜色、廓形、领口、袖型、衣长、纹理、图案/logo、纽扣、口袋、缝线不被改。

### 3.2 工程目标

- 在成本不是主要约束的前提下，所有展示图生成都默认经过 GPT-5.5 导演层。
- GPT-5.5 不做无约束自由发挥，而是按 schema 输出：整批拍摄概念、每张 SceneCard、每张最终 prompt 和生成前风险检查。
- 最终发给图片模型的 prompt 由 GPT-5.5 编排、后端强制注入 Garment Lock 和禁令，保证商品还原、模特一致、质量约束稳定。
- 场景计划、逐张 prompt 和风险审稿要可存储、可复现、可回放。
- 规则池只作为 GPT-5.5 的参考素材库和故障兜底，不作为默认主路径。

## 4. 核心方案：Scene Planner + Garment Lock

把生成拆成两层：

1. Garment Lock：锁定商品不可变约束。
2. Scene Planner：为一批图生成不重复的自然场景卡。

最终 prompt 不再只传 `shot_variant.label`，而是传一张结构化 `SceneCard`。

### 4.1 Garment Lock

从商品图和商品分析中生成稳定的服装锁定对象：

```json
{
  "category": "宽松长袖格纹衬衫",
  "core_identity": "蓝色细格纹宽松长袖衬衫",
  "must_preserve": [
    "蓝色细格纹",
    "宽松落肩版型",
    "衬衫领",
    "长袖袖口",
    "胸前贴袋",
    "胸前文字/小标识",
    "前襟纽扣",
    "胸前圆章装饰"
  ],
  "visibility_priority": ["正面胸口", "领口", "胸袋", "袖口", "整体廓形"],
  "occlusion_policy": "手、头发、包带、宠物、饮料杯不得遮挡胸前主体超过 15%",
  "mutation_bans": ["改颜色", "改格纹密度", "新增图案", "新增口袋", "改成外套", "改短袖"]
}
```

Garment Lock 的用途：

- 每张图 prompt 都必须注入。
- 生成前风险审稿逐项检查。
- 场景规划时根据 `visibility_priority` 限制动作和道具。

### 4.2 SceneCard Schema

建议新增结构：

```json
{
  "id": "street-dog-low-angle-01",
  "scene_family": "urban_street",
  "location": "城市街角斑马线旁",
  "micro_event": "模特蹲低和戴同色帽的狗靠近镜头，像朋友随手抓拍",
  "camera": {
    "distance": "near",
    "angle": "low_angle",
    "lens_feel": "wide_phone",
    "orientation": "vertical"
  },
  "pose": "单膝微蹲，一手伸向镜头但不挡住衣服主体",
  "motion": "狗靠近镜头，背景轻微动态",
  "props": ["蓝色棒球帽", "牵引绳", "狗"],
  "lighting": "晴天户外侧逆光，天空和云清晰",
  "composition": "模特偏右上，狗在前景，衣服胸前区域清楚",
  "product_visibility": "upper_body_priority",
  "negative": ["不要遮挡胸前口袋和纽扣", "不要改衬衫格纹", "不要让狗变成主体"]
}
```

SceneCard 不是自由文案，而是受控字段。这样可以同时做到自然和可控。

## 5. GPT-5.5 生成前必经策略

如果不在乎成本，最优策略是：所有展示图在进入图片模型前都让 GPT-5.5 过一次，而且不止一次。

这里的“过一次”不是让 GPT-5.5 自由写一大段 prompt，而是把它放在三个生成前固定位置：

1. Batch Director：一次性规划整批图的拍摄概念、连续元素、场景分布、镜头分布和去重策略。
2. Per-image Prompt Composer：每张图单独生成结构化最终 prompt，结合 SceneCard、Garment Lock、模特一致性、遮挡风险和画质指令。
3. Risk Review：派发图片任务前做风险审稿，不通过就重写 SceneCard 或 prompt。

### 5.1 默认主路径：GPT-5.5 Batch Director

每次点击“生成展示图”，都先调用 GPT-5.5：

- 输入：商品分析、商品图关键视觉点、用户方向、模特摘要、配饰方案、输出张数、比例、质量、年龄段、安全约束。
- 输出：`series_concept`、`continuity_anchors`、`scene_cards[]`、`scene_fingerprints[]`、`risk_notes[]`。
- 后端校验：数量、去重、年龄适配、危险动作、遮挡风险、商品变更词。

这样做的价值：

- 同一批图会有统一“拍摄策划”，不再像随机抽模板。
- GPT-5.5 能根据衣服动态决定场景，不局限于固定池子。
- 多张图可以形成真实系列感，同时避免重复。

### 5.2 每张图再过 GPT-5.5 Prompt Composer

每个 SceneCard 进入图片模型前，再调用一次 GPT-5.5 生成单张最终 prompt：

- 将 `Garment Lock` 放在最高优先级。
- 把 SceneCard 翻译成自然摄影指令。
- 明确这张图的商品可见区域和遮挡禁令。
- 根据机位调整镜头语言，例如低角度、俯拍、近景、背面回头、手机随拍。
- 输出固定 JSON：`final_prompt`、`negative_prompt_notes`、`product_visibility_checklist`、`regenerate_if`。

这一步的作用是把“导演想法”变成“图片模型能执行的单张任务”，并且让每张图都更细腻。

### 5.3 生成前 GPT-5.5 Risk Review

在派发图片任务前，对每张最终 prompt 做一次审稿：

- 是否可能改衣服。
- 是否可能遮挡胸前、领口、袖口、图案/logo。
- 是否可能让宠物、包、饮料杯、手机抢主体。
- 是否和本批其他图太像。
- 是否动作太复杂导致模型不稳定。

如果风险高，不直接生成，而是让 GPT-5.5 改 SceneCard 或 prompt。

### 5.4 生成后处理：本期不接 Vision QA

首版生成后不再自动调用 GPT-5.5 Vision QA。原因：

- Vision QA 会显著增加等待时间，且用户本来会看图挑选。
- 当前更关键的是生成前把 prompt 写准，减少明显跑偏。
- 返修仍走现有文字返修链路，由用户对不满意图片发起。

结论：当前版本 GPT-5.5 应该是“导演 + 分镜师 + 生成前审稿”的控制层；规则池只是参考和兜底。Vision QA 作为后续可选 Phase，不进入首版默认链路。

## 6. 场景动态适配规则

Scene Planner 需要根据衣服推导场景，而不是只问用户 indoor/outdoor。

### 6.1 商品到场景的映射维度

从 `product_analysis` 扩展或派生：

- `formality`：休闲 / 通勤 / 正装 / 派对 / 运动 / 居家。
- `seasonality`：春夏 / 秋冬 / 四季。
- `energy`：安静 / 日常 / 运动 / 户外 / 夜间。
- `material_behavior`：轻薄飘动 / 厚重挺括 / 柔软垂坠 / 牛仔硬挺。
- `visibility_risk`：容易被遮挡的重点区域。
- `styling_anchor`：适合搭配的低存在感道具，如帽子、包、咖啡、狗绳、手机。

### 6.2 示例映射

蓝色宽松格纹衬衫：

- 场景：城市街角、咖啡店外、车站、斑马线、便利店门口、公园边、电话亭/公交站。
- 动作：遛狗、拿咖啡、看手机、靠墙、过马路、等车、回头。
- 道具：蓝色棒球帽、黑色托特包、饮料杯、牵引绳、狗。
- 不适合：高奢酒店大堂、晚宴、强棚拍、夸张跑跳、湿身/风暴场景。

运动服：

- 场景：晨跑路边、网球场外、健身房入口、公园步道。
- 动作：系鞋带、拉伸、拿水瓶、走出场馆。
- 禁忌：过度时装化、过多首饰。

裙装/礼服：

- 场景：画廊、街边餐厅、酒店门口、夜晚橱窗、室内窗光。
- 动作：轻扶裙摆、走下台阶、回头、坐在椅边。
- 禁忌：大风遮挡裙型、坐姿压坏版型。

童装：

- 场景：公园、学校门口、家庭客厅、亲子空间。
- 动作：走路、拿玩具、背小包。
- 禁忌：成人化姿势、成熟妆容、危险街道动作。

## 7. 去重机制

每张 SceneCard 生成 `scene_fingerprint`：

```text
scene_family + location_type + camera_angle + distance + micro_event + primary_prop + lighting
```

同一批约束：

- `scene_family` 不得连续重复超过 2 次。
- `camera.angle` 至少覆盖 2 种，8 张以上至少覆盖 4 种。
- `distance` 至少覆盖 full body / half body / near detail。
- `micro_event` 必须全批唯一。
- `primary_prop` 不能每张都相同；可以有“连续拍摄锚点”，但不能让道具成为重复模板。
- 4 张图至少 1 张全身、1 张半身细节、1 张动作/事件、1 张侧背或环境氛围。

允许“系列感”，禁止“复制感”：

- 可以同一个模特、同一只狗、同一顶帽子。
- 不可以每张都是同一街角、同一站姿、同一距离、同一表情。

## 8. Prompt 组装策略

最终图片 prompt 建议由四段组成：

1. Invariants：商品锁定、模特一致、禁止改衣服。
2. SceneCard：具体场景、事件、机位、道具、光线。
3. Product Visibility：本张图衣服重点展示区域。
4. Realism + Negatives：真实摄影质感、皮肤、光影、禁令。

示例最终片段：

```text
请根据白底产品图和已确认模特参考图，生成真实自然的真人服饰穿搭照片。

【最高优先级：商品还原】
衣服以白底产品图为唯一来源。必须保留：蓝色细格纹、宽松落肩版型、衬衫领、长袖袖口、胸袋、胸前文字/小标识、前襟纽扣、圆章装饰。不得改款、改色、改格纹密度、改口袋位置、改纽扣、添加新 logo。

【场景】
城市街角斑马线旁，晴天自然侧逆光。模特穿这件衬衫和深色宽松长裤，牵着一只戴蓝色帽子的狗过马路。朋友从略高处随手拍，模特小步向前，狗在右下前景，背景有真实街道、路牌和车辆轻微虚化。

【构图和动作】
竖图，模特完整入镜但不僵硬，身体重心可信。衣服胸前、领口、纽扣和袖口清楚可见。牵引绳和手不要遮挡胸前主体。

【真实感】
真实街拍照片，自然皮肤毛孔、碎发、衣服褶皱、真实阴影和路面纹理。不要棚拍感、不要 AI 美颜脸、不要塑料皮肤、不要时装秀夸张 pose。
```

注意：不把所有场景都写成“50mm 平视”。样张里低角度、俯拍、近距离手机视角非常有效。应改为按 SceneCard 控制镜头，并用安全规则限制广角畸变。

## 9. 质量控制

### 9.1 生成前 Prompt Lint

对 SceneCard 做静态检查：

- 是否有遮挡商品高风险动作。
- 是否出现与年龄不匹配的场景。
- 是否出现会改衣服的道具或造型。
- 是否和本批已有 SceneCard 重复。
- 是否过度复杂：场景、道具、动作太多会压低商品还原。

### 9.2 生成后处理：本期不做自动 Vision QA

首版不自动调用 GPT-5.5 Vision QA。生成后先把图交给用户挑选，用户不满意时走现有文字返修或重新生成。

未来如果要接自动 Vision QA，可以输出结构化报告：

```json
{
  "garment_accuracy": 0.0,
  "model_consistency": 0.0,
  "scene_naturalness": 0.0,
  "product_visibility": 0.0,
  "duplicate_risk": 0.0,
  "issues": ["胸前标识缺失", "手遮挡纽扣", "场景和上一张过于相似"],
  "action": "accept | regenerate | revise"
}
```

未来 QA 通过门槛建议：

- `garment_accuracy >= 0.82`
- `product_visibility >= 0.75`
- `scene_naturalness >= 0.70`
- `duplicate_risk <= 0.35`

未来失败处理：

- 商品错误：用同一 SceneCard 重新生成，增强 Garment Lock。
- 场景死板：保留 Garment Lock，替换 SceneCard。
- 遮挡严重：保留场景，改动作和道具位置。
- 重复：替换 location/camera/micro_event 中至少两个字段。

## 10. 数据结构建议

### 10.1 API 入参扩展

在 `ShowcaseImagesCreateIn` 增加：

```python
scene_strategy: Literal["balanced", "natural_series", "editorial_campaign"] = "natural_series"
scene_variety: Literal["safe", "rich", "wild"] = "rich"
scene_planner: Literal["gpt55_preflight", "gpt55_batch_only", "rules_fallback"] = "gpt55_preflight"
continuity_anchor: Literal["none", "accessory", "pet", "location_series"] = "accessory"
allow_pet: bool = False
allow_background_people: bool = True
```

解释：

- `balanced`：当前体验增强版，商品展示优先。
- `natural_series`：像样张一样，有日常事件和连续拍摄感。
- `editorial_campaign`：更像品牌 campaign，机位和光线更大胆。
- `gpt55_preflight`：默认主路径。GPT-5.5 做批量导演、逐张 prompt 编排和生成前审稿；生成后不自动 Vision QA。
- `gpt55_batch_only`：只让 GPT-5.5 生成整批 SceneCards，逐张 prompt 由后端模板编排。可作为降级模式。
- `rules_fallback`：仅当 GPT-5.5 不可用或超时时使用本地规则池兜底，不作为常规入口。

### 10.2 WorkflowStep 存储

`showcase_generation.input_json` 增加：

```json
{
  "scene_strategy": "natural_series",
  "scene_planner": "gpt55_preflight",
  "scene_cards": [],
  "per_image_prompts": [],
  "prompt_reviews": [],
  "scene_fingerprints": [],
  "garment_lock": {},
  "planner_version": "apparel-gpt55-preflight-v1"
}
```

每个 generation 的 `workflow_meta` 增加：

```json
{
  "workflow_scene_card_id": "street-dog-low-angle-01",
  "workflow_scene_family": "urban_street",
  "workflow_camera_angle": "low_angle",
  "workflow_micro_event": "dog_selfie",
  "workflow_scene_fingerprint": "..."
}
```

## 11. 模块拆分

建议新增：

- `apps/api/app/routes/_apparel_scene_planner.py`
  - `build_garment_lock(product_analysis)`
  - `plan_scene_cards_with_gpt55(...)`
  - `fallback_scene_cards_from_pool(...)`
  - `scene_fingerprint(card)`

- `apps/api/app/routes/_apparel_scene_pool.py`
  - 本地规则池：给 GPT-5.5 提供参考素材，也作为服务不可用时的兜底。

- `apps/api/app/routes/_apparel_scene_prompt.py`
  - `compose_image_prompt_with_gpt55(card, garment_lock, ...)`
  - `review_prompt_risk_with_gpt55(prompt, garment_lock, batch_context)`
  - `render_fallback_scene_prompt(card, garment_lock, ...)`

- 后续可选：`apps/api/app/routes/_apparel_scene_qa.py`
  - `review_generated_image_with_gpt55_vision(...)`
  - `recommend_revision_or_regeneration(...)`
  - 本期不接入默认生成链路。

- `apps/worker` 或 API 现有 completion 能力
  - GPT-5.5 director / composer / risk review 调用，严格 JSON schema。

## 12. GPT-5.5 生成前合约

### 12.1 Batch Director 输入

```json
{
  "product": {
    "category": "蓝色格纹宽松衬衫",
    "must_preserve": ["蓝色细格纹", "胸袋", "纽扣", "圆章"],
    "risks": ["胸前细节容易被包带/手遮挡"]
  },
  "model": {
    "age_segment": "young_adult",
    "style": "自然街拍，金发卷发，松弛城市感"
  },
  "request": {
    "count": 8,
    "strategy": "natural_series",
    "variety": "rich",
    "aspect_ratio": "4:5",
    "allow_pet": true
  }
}
```

### 12.2 Batch Director 输出

输出必须是 JSON：

```json
{
  "series_concept": "城市遛狗日常街拍",
  "continuity_anchors": ["蓝色棒球帽", "深色宽松裤", "同一只法斗"],
  "scene_cards": [
    {
      "id": "crosswalk-high-angle-01",
      "scene_family": "urban_street",
      "location": "斑马线",
      "micro_event": "牵狗过马路时抬头看镜头",
      "camera": {"distance": "full_body", "angle": "high_angle", "lens_feel": "phone"},
      "pose": "小步向前，牵引绳在身体侧边",
      "props": ["狗", "牵引绳", "蓝色帽子"],
      "lighting": "晴天下午侧光",
      "product_visibility": "front_full_body",
      "negative": ["不要让牵引绳遮挡胸前", "不要改变衬衫格纹"]
    }
  ]
}
```

后端必须校验：

- `scene_cards.length == output_count`
- 每个 id 唯一
- micro_event 唯一
- 不出现危险/违规/年龄不匹配场景
- 不出现商品变更语言
- 不出现真实品牌、名人、可识别私人地点要求

### 12.3 Per-image Prompt Composer 输出

每张 SceneCard 再调用 GPT-5.5，输出固定 JSON：

```json
{
  "scene_card_id": "crosswalk-high-angle-01",
  "final_prompt": "请根据白底产品图和已确认模特参考图生成真实街拍照片...",
  "product_visibility_checklist": [
    "胸前格纹清晰",
    "胸袋和纽扣清晰",
    "牵引绳不遮挡衣服主体"
  ],
  "negative_prompt_notes": [
    "不要改变衬衫颜色和格纹",
    "不要新增 logo 或图案",
    "不要让狗成为画面主体"
  ],
  "regenerate_if": [
    "胸前细节被手、包带或宠物遮挡",
    "衬衫变成纯色或格纹明显变化",
    "人物身份和参考模特不一致"
  ]
}
```

后端仍需把 Garment Lock 注入到 `final_prompt` 的最高优先级段落，防止 GPT-5.5 漏写或弱化商品约束。

### 12.4 Risk Review 输出

派发图片任务前，对每张 prompt 审稿：

```json
{
  "scene_card_id": "crosswalk-high-angle-01",
  "risk_level": "low",
  "risks": [],
  "must_rewrite": false,
  "rewrite_instruction": ""
}
```

如果 `risk_level=high` 或 `must_rewrite=true`，必须回到 Prompt Composer 重写，不进入图片生成。

### 12.5 后续可选：Vision QA 输出

本期不自动调用 Vision QA。未来如果要做自动质检，可以在生成后对每张图输出：

```json
{
  "scene_card_id": "crosswalk-high-angle-01",
  "garment_accuracy": 0.88,
  "model_consistency": 0.84,
  "scene_naturalness": 0.91,
  "product_visibility": 0.82,
  "duplicate_risk": 0.18,
  "issues": [],
  "action": "accept",
  "revision_instruction": ""
}
```

如果未来接入且 `action=revise`，按 `revision_instruction` 走现有返修链路；如果 `action=regenerate`，保留 SceneCard 但重写 prompt 或替换高风险动作。

## 13. UI 设计建议

当前 UI 只有模板、比例、质量、张数、室内/室外。建议改为：

- 场景风格：商品清晰 / 自然街拍 / 手机随拍 / 品牌 campaign
- 丰富度：稳妥 / 丰富 / 大胆
- 连续元素：无 / 保留配饰 / 加宠物 / 同一地点系列
- 场景范围：自动 / 室内 / 室外 / 城市 / 居家 / 通勤 / 度假

默认值：

- 新手默认 `自然街拍 + 丰富 + 自动 + 不加宠物`
- 如果商品是童装，关闭成人化场景和宠物高风险动作。
- 如果商品细节很小，自动降低丰富度，优先半身和正面。

## 14. 分阶段落地计划

### Phase 1：GPT-5.5 Batch Director 必接入

范围：

- 新增 SceneCard schema 和本地 scene pool。
- 接入 GPT-5.5 Batch Director，所有展示图生成都先产出整批 SceneCards。
- 改 `_showcase_prompt()` 支持 `scene_card`，并保存 `scene_cards / scene_fingerprints / garment_lock`。
- 扩展 `scene_environment` 到更细的 scene family，但 UI 可先不完全暴露。
- 去掉生活化模板里固定 50mm/平视限制，改为 SceneCard 控制镜头。
- 增加 scene fingerprint 去重。

收益：

- 每批图立刻有整体拍摄策划。
- 场景和动作开始真正按商品适配。
- 规则池转为 GPT-5.5 参考素材和故障兜底。

### Phase 2：GPT-5.5 Per-image Prompt Composer

范围：

- `scene_planner=gpt55_preflight` 默认开启。
- 每张 SceneCard 单独调用 GPT-5.5 生成最终 prompt JSON。
- 加入生成前 Risk Review，风险高的 prompt 自动重写。
- 后端强制把 Garment Lock 注入最终 prompt 最高优先级段落。

收益：

- 每张图不只是“套模板”，而是有独立分镜和执行细节。
- 大幅减少遮挡商品、场景重复、动作僵硬。

### Phase 3：可选 GPT-5.5 Vision QA + 自动返修

范围：

- 本期不做；后续如果需要，再用 GPT-5.5 Vision 检查衣服准确、遮挡、重复、自然度。
- 自动决定 accept/regenerate/revise。
- 对失败图生成针对性修复 prompt。

收益：

- 商品准确性从 prompt 约束升级为闭环控制。
- 对复杂自然场景更安全。

## 15. 对样张风格的复现策略

如果要复现参考图这种“城市街拍 + 狗 + 蓝色帽子 + 同款衬衫”的效果，应该使用：

```json
{
  "scene_strategy": "natural_series",
  "scene_variety": "rich",
  "scene_planner": "gpt55_preflight",
  "continuity_anchor": "pet",
  "allow_pet": true,
  "scene_family": ["urban_street", "commute", "cafe_exterior", "station"],
  "camera_mix": ["low_angle", "high_angle", "eye_level", "back_view", "half_body"],
  "micro_events": ["遛狗", "过马路", "等车", "靠墙", "拿咖啡", "看手机", "回头"]
}
```

但需要明确商品约束：

- 狗和帽子是辅助叙事元素，不能抢主体。
- 包带、牵引绳、饮料杯不能遮挡衬衫胸前细节。
- 衬衫的格纹、胸袋、纽扣、文字/标识、圆章必须逐项保留。
- 可允许非商品配饰文案轻微变化，但商品本身的标识不能变化。

## 16. 决策结论

在当前阶段，最优实现应当是“每批、每张、生成前都经过 GPT-5.5”，生成后先不接自动 Vision QA。每一步都必须结构化、可校验、可回放：

1. 商品准确先靠 Garment Lock、逐张 prompt 编排和生成前 Risk Review。
2. 自然丰富靠 GPT-5.5 Batch Director 生成整批 SceneCards。
3. 单张质感靠 GPT-5.5 Per-image Prompt Composer，把每张图写成具体可执行的拍摄任务。
4. 去重靠 scene fingerprint、批量规划和生成前 Risk Review。
5. 生成后由用户挑图和发起文字返修；自动 Vision QA 以后再加。
6. 规则池不再是默认主路径，只作为参考素材和故障 fallback。

这样可以兼顾三个目标：

- 衣服准：商品锁定、逐张 prompt 和生成前审稿。
- 场景丰富：GPT-5.5 按衣服动态生成自然生活事件。
- 不重复：整批计划、逐张 prompt、审稿和指纹去重，而不是每张图随机碰运气。
