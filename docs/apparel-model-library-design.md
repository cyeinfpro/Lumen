# 服饰模特展示图：模特库设计方案

> 状态：设计稿
> 适用范围：`apparel_model_showcase` 工作流
> 目标：在「选择模特」前后沉淀可复用模特图。GitHub 预设是全站通用；用户收藏和用户上传只属于当前用户，并在个人模特库里与全站预设一起展示。

## 1. 背景与目标

当前服饰模特展示图工作流是线性的：

1. 上传商品图。
2. 确认商品约束。
3. 填写模特设定。
4. 生成 3 套模特候选。
5. 确认模特，再生成配饰四宫格和最终展示图。

这个流程适合第一次探索，但有两个明显缺口：

- 项目作者已经有一些稳定、好看的预设模特参考图，却无法统一分发给用户复用。
- 用户在项目里生成或上传过满意模特后，不能沉淀成自己的模特资产，下次还要重新生成。

模特库要解决的是「模特参考图复用」问题，而不是替代候选生成能力。用户可以从库里直接选择一个模特，也可以继续生成新候选；生成出的满意候选也可以收藏回库里。

## 2. 设计原则

### 2.1 不覆盖，只追加

GitHub 预设库同步到本地时，只能新增或更新同一预设条目的元信息，不能删除用户本地已有图片，也不能覆盖用户收藏图。

推荐规则：

- 预设条目用稳定 `preset_id` 去重。
- 同一 `preset_id` 再次同步时，如果远端 `version` 或文件 hash 变化，可以新增一个版本或更新预设元信息。
- 用户自定义条目永远不被同步任务删除。
- 用户可以隐藏不想看的预设，但隐藏状态只存在本地。

### 2.2 全站预设和个人资产分层

模特库来源分三类：

| 来源 | 归属范围 | 典型入口 | 是否可删除 | 是否其他用户可见 |
|---|---|---|---|---|
| GitHub 预设 | 全站通用 | GitHub 同步 | 不能删除，只能按用户隐藏 | 是 |
| 用户收藏 | 当前用户私有 | 从候选模特收藏 | 可删除/取消收藏 | 否 |
| 用户上传 | 当前用户私有 | 模特库上传入口 | 可删除 | 否 |

用户看到的「模特库」不是一个完全全站共享库，而是一个个人视图：

```text
当前用户的模特库 = GitHub 全站预设 + 当前用户收藏 + 当前用户上传
```

只有 GitHub 预设进入全站通用范围。用户上传和用户收藏永远不进入全站库，不会被其他用户看到，也不会被 GitHub 同步任务新增、更新或删除。

UI 上可以把三类条目混合展示，但数据层必须保留来源和归属范围。这样后续才能做同步、去重、隐藏、筛选和权限控制。

### 2.3 选择后进入现有 `image_id` 体系

现有工作流的生成任务、图片访问和权限校验都围绕 `images.id`。因此模特库条目被选中后，应该转换为当前用户可引用的普通图片引用。

推荐做法：

- 用户上传和用户收藏本来就是用户自己的 `Image`。
- GitHub 预设在全站缓存中保存原始文件和索引；用户选用某个预设时，后端为当前用户创建或复用一条私有 `Image` 记录，指向同一份本地缓存文件或复制一份文件到用户目录。
- 后续生成候选、配饰四宫格、展示图都只使用普通 `image_id`，不让 worker 感知「预设库」这个概念。

这能最大限度复用现有权限模型，避免把公共图片权限穿透到所有图片 API。

### 2.4 参考图驱动生成

模特库独立生成入口支持两种模式：

- `text`：原有文生模特模式，用户手动选择年龄段、性别、外貌方向、气质标签和其他要求。
- `reference_image`：用户上传一张人像参考图，后端同步用 vision 模型抽取年龄段、性别、外貌方向和气质标签，再把参考图作为图生图附件传给生成 provider，生成同一个人的 2×2 contact sheet。

参考图模式不新增存储体系。上传仍走 `/images/upload`，生成任务仍复用 `apparel_model_library_generate` workflow、`model_library_generate` step、任务中心轮询、自动打标签和入库流程。差异只体现在 prompt 来源和 generation intent：参考图模式使用 `Intent.IMAGE_TO_IMAGE` 并附带 `reference_image_id`，同时在 `WorkflowRun.metadata_jsonb` 与 `WorkflowStep.input_json` 中保存 `mode`、`reference_image_id` 和 `extracted_profile`，方便任务中心展示来源和识别结果。

参考图可以是任意朝向、任意表情或任意构图。生成 prompt 固定要求输出四视图：正面全身、左 90° 侧面全身、背面全身、正面大头照，并要求中性表情。未出现在参考图里的侧面、背面和全身比例由上游模型推断，但身份锚定始终以附件参考图为准。

## 3. 用户体验设计

### 3.1 模特设定阶段的入口（统一入口）

在「模特设定」阶段只放一个清晰入口：

```text
模特设定
  风格方向输入
  禁用项
  配饰四宫格方向

  [打开模特库]
```

「模特库」是模特相关动作的唯一入口。模特库弹窗内同时承载：

- 浏览全站预设、当前用户收藏、当前用户上传。
- 上传新的私有模特图。
- 触发候选生成（按钮 `生成模特候选`）。

不采用早期方案「[从模特库选择] [生成模特候选]」双按钮的原因：

- 双按钮把「我已经有想要的」和「我想新做一个」暴露给用户，但用户的真实心智只是「我要给这个项目挑/做一个模特」。
- 单入口让认知模型更连贯：预设、收藏、新生成在一个面板里贯穿，模特库就是模特工作台。

推荐交互：

- 用户在弹窗内选中库内模特后，弹窗自动收起，页面展示「已选库内模特」摘要卡。
- 选中即创建状态为 `ready` 的 `ModelCandidate`（详见 §6.4），用户仍可进入候选确认阶段查看和确认。
- 后续配饰四宫格、最终展示图都沿用现有流程。

「生成模特候选」走现有候选阶段，不在弹窗内承载：

- 点击 `生成模特候选` → 关闭弹窗 → 进入现有候选确认阶段，沿用当前 3 套候选生成流程。
- 不在弹窗内做实时生成 + 多候选预览 + 批量收藏：生成是 30s+ 长任务，弹窗承载长任务会变成第二个工作台（异步状态、关闭后任务保留、移动端体验都要重做），工程复杂度高一档。
- 候选确认阶段每张候选卡片提供 `[设为当前模特]` + `[收藏到库]` 双操作（详见 §3.5），就是「一次性使用 vs 沉淀到库」的天然分流口。

「基于库内某张预设生成相似候选」放到 Phase 3 再做（§10），不作为第一版主路径，避免弹窗内再承载有状态的生成流程。

### 3.2 模特库展示形态

桌面端推荐使用弹窗，移动端使用底部抽屉。原因：

- 模特库是阶段内的辅助选择，不应该跳离当前项目。
- 用户选完要回到当前工作流继续配置配饰和生成图。
- 弹窗能保持上下文，同时给足网格展示空间。

桌面布局：

```text
┌──────────────────────────────────────────────────────────────┐
│ 模特库                             [同步预设] [上传模特图] [X] │
├───────────────┬──────────────────────────────────────────────┤
│ 来源          │  年龄段 tabs / 筛选 / 搜索                    │
│ ○ 全部        │  ┌────┐ ┌────┐ ┌────┐ ┌────┐                 │
│ ○ 全站预设    │  │图  │ │图  │ │图  │ │图  │                 │
│ ○ 我的收藏    │  └────┘ └────┘ └────┘ └────┘                 │
│ ○ 我的上传    │                                              │
│               │  选中后底部固定操作：                         │
│ 性别/外貌方向 │  [查看大图] [设为当前模特]                     │
└───────────────┴──────────────────────────────────────────────┘
```

移动端：

- 顶部只放标题、关闭、上传。
- 年龄段用横向 tabs。
- 来源筛选放在 segmented control 或筛选按钮里。
- 底部固定「设为当前模特」按钮。

### 3.3 年龄段组织

模特库分类包含一个用户收藏分类和真实年龄段：

- 用户收藏
- 幼儿
- 儿童
- 青少年
- 青年
- 成年
- 中老年
- 老年

模特库应该沿用同一套枚举，避免用户在创建项目和选模特时看到两套语言。

推荐展示规则：

- 年龄段作为主筛选 tabs，不作为目录树。
- 默认选中当前项目创建时的年龄段。
- 「全部」保留，用于跨年龄段浏览。
- 如果某个年龄段没有条目，显示空状态和上传入口，不隐藏 tab。
- 上传/收藏时分类必选；不提供「不指定/不识别」选项。
- 「用户收藏」是独立分类，不是年龄段，目录里同样分 `female/` 和 `male/`。

年龄段字段命名建议：

```text
user_favorites 用户收藏
toddler      幼儿
child        儿童
teen         青少年
young_adult  青年
adult        成年
middle_aged  中老年
senior       老年
```

前端展示继续使用中文标签，后端和索引用英文枚举，便于 GitHub 预设文件夹稳定维护。

### 3.4 卡片信息

模特库卡片需要展示足够少但有用的信息：

- 图片缩略图。
- 年龄段标签。
- 来源标签：全站预设 / 我的收藏 / 我的上传。
- 可选标签：性别、外貌方向、风格气质。
- 收藏状态。

卡片操作：

- 点击图片：预览大图。
- 点击卡片底部按钮：设为当前模特。
- `...` 菜单：收藏全站预设、重命名个人条目、编辑个人标签、隐藏全站预设、删除用户上传。

不建议在卡片上堆很多文字。模特图是视觉资产，网格浏览优先。

### 3.5 候选阶段的「使用 / 收藏」双操作

候选阶段是 §3.1 单入口模型下「一次性使用 vs 沉淀到库」的天然分流口。

每个候选卡片提供两个并列操作：

```text
[设为当前模特]   [收藏到库]
```

- `设为当前模特`：选定此候选作为本项目的模特，进入配饰四宫格阶段；不写入模特库。
- `收藏到库`：把候选登记为当前用户的私有库条目，下次新建项目时可以直接从模特库选用。

两个操作互不冲突，用户可以「先收藏，再选定」或「先选定，再补一个收藏」。

收藏行为：

- 收藏的是候选的主参考图，优先 `contact_sheet_image_id`；取不到时回退到 `model_brief_json.candidate_image_ids[0]`；再取不到 API 返回 422，不静默失败。
- 如果候选有多张拆分视图，后续可以支持「收藏整组」；MVP 先收藏主图。
- 收藏时弹出轻量表单：名称、年龄段、性别、外貌方向、是否填写标签。年龄段选择后应显示目标文件夹，例如 `05_adult`。
- 默认年龄段优先读取 `workflow_runs.metadata_jsonb.model_profile.age_segment`（详见 §7.2），取不到再回退到从 `user_prompt` 推断；不要把自然语言解析作为主路径。

### 3.6 用户上传模特图

上传入口放在模特库弹窗顶部。

上传后要求用户补充：

- 名称。
- 年龄段。
- 目标文件夹，随年龄段自动显示，例如 `05_adult`；后续整理到 GitHub 预设目录时按该字段归档。
- 性别，可选。
- 外貌方向，可选。
- 风格标签，可选，默认不填写，用户开启后再输入。

上传图建议限制：

- 支持 `png`、`jpg/jpeg`、`webp` 三种格式。
- 复用现有图片上传大小限制。
- 上传后走已有 `images/upload`，再通过模特库 API 登记为 `user_upload` 条目。

## 4. GitHub 预设库设计

### 4.1 仓库内目录

预设模特图放在项目 GitHub 仓库中，建议目录：

```text
assets/apparel-model-presets/
  00_user_favorites/
    female/
    male/
  01_toddler/
    female/
    male/
  02_child/
    female/
    male/
  03_teen/
    female/
    male/
  04_young_adult/
    female/
    male/
  05_adult/
    female/
    male/
  06_middle_aged/
    female/
    male/
  07_senior/
    female/
    male/
```

每个图片文件可以是：

```text
05_adult/female/adult-minimal-studio-001.png
05_adult/female/adult-minimal-studio-001.thumb.png
```

图片可以是 `png`、`jpg/jpeg`、`webp`。`thumb` 可选；没有时由同步任务本地生成缩略图或直接使用主图变体。

### 4.2 文件夹即预设源

第一版不要求维护 `manifest.json`。同步任务默认读取 GitHub Contents API：

```text
https://api.github.com/repos/cyeinfpro/Lumen/contents/assets/apparel-model-presets?ref=main
```

同步规则：

- 递归枚举 `assets/apparel-model-presets/` 下所有 `png`、`jpg/jpeg`、`webp`。
- 分类由一级子目录名推断，支持序号前缀，例如 `05_adult/female/xxx.png` → `adult`。
- 性别由二级子目录名推断，只支持 `female` 和 `male`。
- `preset_id` 由目录名 + 文件名生成；如果文件名已经带分类前缀则不重复添加，例如 `05_adult/female/adult-minimal-studio-001.png` → `adult-minimal-studio-001`。
- `*.thumb.webp`、`*.thumb.jpg`、`*.thumb.png` 不进入主列表，只作为同名主图缩略图。
- 标题、性别、外貌方向、风格标签从文件名尽力推断；后续如需更精确元信息，再追加可选 metadata 文件或 manifest。

### 4.3 同步按钮放在哪里

用户初步想法里提到「项目作者的场面」需要同步按钮。推荐分两层：

1. 管理员/项目作者入口：系统设置或项目模板管理里有「同步预设模特库」。
2. 普通用户入口：模特库弹窗里展示「预设库更新时间」，不默认暴露强同步按钮。

原因：

- GitHub 同步是全站缓存操作，应该由作者/管理员控制，避免多个普通用户频繁拉取。
- 普通用户最关心可用模特，不关心远端文件枚举。
- 如果服务是自用，也可以在模特库弹窗顶部保留一个小的「同步预设」按钮，但需要加冷却和权限控制。

### 4.4 何时拉取

推荐策略：

- 手动同步：作者点击按钮时立即拉取。
- 懒同步：打开模特库时，如果距离上次成功同步超过 24 小时，后台触发一次非阻塞同步。
- 启动同步：API 启动后可选择检查一次，但不阻塞服务启动。

MVP 建议只做手动同步 + 24 小时懒同步提示，不做定时任务。

拉取频率建议：

```text
manual sync: 允许立即触发，但 5 分钟内重复点击返回上次结果
lazy sync: last_success_at 超过 24 小时才触发
failure backoff: 失败后 30 分钟内不自动重试
```

同步按钮状态：

- `同步预设`
- `同步中`
- `已是最新`
- `同步失败，稍后重试`
- `上次同步：2026-05-04 15:20`

## 5. 本地存储设计

### 5.1 lumendata 目录

模特库的本地数据放在 `lumendata`，推荐目录：

```text
/opt/lumendata/storage/apparel-model-library/
  index.json
  sync-state.json
  presets/
    adult-female-minimal-studio-001/
      v1.png
      thumb.png
      meta.json
  users/
    {user_id}/
      index.json
```

如果开发环境使用本地 `storage_root`，则相对路径相同：

```text
{settings.storage_root}/apparel-model-library/...
```

### 5.2 索引文件

全局索引 `index.json` 存预设和轻量聚合信息：

```json
{
  "schema_version": 1,
  "updated_at": "2026-05-04T07:20:00Z",
  "preset_items": [
    {
      "id": "preset:adult-female-minimal-studio-001:v1",
      "source": "preset",
      "preset_id": "adult-female-minimal-studio-001",
      "version": 1,
      "title": "成年女性｜高级简洁棚拍",
      "age_segment": "adult",
      "gender": "female",
      "image_storage_key": "apparel-model-library/presets/adult-female-minimal-studio-001/v1.png",
      "thumb_storage_key": "apparel-model-library/presets/adult-female-minimal-studio-001/thumb.png",
      "sha256": "..."
    }
  ]
}
```

用户索引 `users/{user_id}/index.json` 只存当前用户私有条目和该用户自己的预设隐藏状态：

```json
{
  "schema_version": 1,
  "updated_at": "2026-05-04T07:30:00Z",
  "hidden_preset_ids": ["preset:adult-female-minimal-studio-001:v1"],
  "items": [
    {
      "id": "user:01HV...",
      "source": "favorite",
      "title": "我的通勤女模特",
      "age_segment": "adult",
      "gender": "female",
      "image_id": "01HV...",
      "owner_user_id": "current-user-id",
      "tags": ["通勤", "冷淡"]
    }
  ]
}
```

### 5.3 文件索引 vs 数据库表

第一版推荐文件索引，不新增数据库表。

原因：

- 需求主要是资产索引和本地缓存，不涉及复杂查询、协作权限、审计。
- 能避免数据库 migration，改动面小。
- 预设同步天然是文件级别操作。

未来如果模特库需要更复杂的个人库搜索、排序、批量管理、预设管理或使用统计，再迁移到数据库表：

```text
apparel_model_library_items
apparel_model_library_preset_syncs
```

文件索引必须注意：

- 写入用临时文件 + 原子 rename。
- API 进程内可以短缓存，但每次写入后要失效。
- 多进程部署时最好加文件锁；MVP 可以用简单锁文件或 Redis lock。

## 6. API 设计

### 6.1 列表

```http
GET /workflows/apparel-model-library?age_segment=adult&source=all&q=
```

这里的 `source=all` 表示「当前用户可见的全部条目」，不是数据库意义上的全站全部条目。它包含全站 GitHub 预设，以及当前用户自己的收藏和上传。

返回：

```json
{
  "items": [
    {
      "id": "preset:adult-female-minimal-studio-001:v1",
      "source": "preset",
      "visibility_scope": "global_preset",
      "title": "成年女性｜高级简洁棚拍",
      "age_segment": "adult",
      "gender": "female",
      "appearance_direction": "asian",
      "style_tags": ["高级简洁", "棚拍"],
      "image_url": "/api/workflows/apparel-model-library/items/.../binary",
      "thumb_url": "/api/workflows/apparel-model-library/items/.../thumb",
      "image_id": null,
      "created_at": "2026-05-04T07:20:00Z"
    }
  ],
  "sync": {
    "last_success_at": "2026-05-04T07:20:00Z",
    "last_error": null,
    "can_sync": true
  }
}
```

### 6.2 同步预设

```http
POST /workflows/apparel-model-library/sync-presets
```

行为：

- 通过 GitHub Contents API 递归枚举预设文件夹。
- 下载新增或变化的图片。
- 按文件内容 hash 判断是否变化。
- 写入本地 `apparel-model-library/index.json`。
- 返回新增数量、更新数量、跳过数量。

返回：

```json
{
  "status": "ok",
  "added": 8,
  "updated": 2,
  "skipped": 20,
  "last_success_at": "2026-05-04T07:20:00Z"
}
```

### 6.3 添加用户条目

上传图先走现有：

```http
POST /images/upload
```

然后登记到模特库：

```http
POST /workflows/apparel-model-library/items
```

Body：

```json
{
  "source": "user_upload",
  "visibility_scope": "user_private",
  "image_id": "01HV...",
  "title": "我的成年女性模特",
  "age_segment": "adult",
  "gender": "female",
  "appearance_direction": "asian",
  "style_tags": ["高级简洁", "独立站"]
}
```

从候选收藏时同样调用该接口，只是 `source` 为 `favorite`。

后端必须把条目写入当前用户的个人索引；不能写入全站预设索引，也不能让其他用户列表接口返回该条目。

### 6.4 选用模特库条目

```http
POST /workflows/{workflow_run_id}/model-library/select
```

Body：

```json
{
  "library_item_id": "preset:adult-female-minimal-studio-001:v1",
  "mode": "use_directly"
}
```

后端行为：

1. 校验 workflow 属于当前用户。
2. 解析模特库条目。
3. 如果是预设条目，为当前用户创建或复用可引用 `Image`。
4. 创建一个 `ModelCandidate`：
   - `status = "ready"` 或直接 `selected`，取决于产品路径。
   - `contact_sheet_image_id = image_id`。
   - `model_brief_json.library_item_id = ...`。
5. 更新 `model_settings/model_candidates/model_approval` 步骤状态。
6. 返回完整 `WorkflowRunOut`。

MVP 建议创建为 `ready`，让用户仍在「模特候选」阶段点一次确认。这能保持现有流程一致，也给用户反悔空间。

### 6.5 收藏候选

```http
POST /workflows/{workflow_run_id}/model-candidates/{candidate_id}/save-to-library
```

Body：

```json
{
  "title": "冷淡通勤女模特",
  "age_segment": "adult",
  "library_folder": "05_adult",
  "gender": "female",
  "appearance_direction": "asian",
  "style_tags": ["通勤", "冷淡", "高级简洁"]
}
```

后端从 candidate 里取 `contact_sheet_image_id` 或 `model_brief_json.candidate_image_ids[0]`，登记为 `favorite`。

## 7. 工作流状态接入

### 7.1 新增输入字段

`model_settings.output_json` 可以记录：

```json
{
  "style_prompt": "...",
  "avoid": [],
  "candidate_count": 3,
  "accessory_plan": {},
  "selected_library_item_id": "preset:...",
  "selected_library_image_id": "01HV..."
}
```

`ModelCandidate.model_brief_json` 可以记录：

```json
{
  "summary": "成年女性｜高级简洁棚拍",
  "source": "model_library",
  "library_item_id": "preset:adult-female-minimal-studio-001:v1",
  "age_segment": "adult",
  "gender": "female",
  "prompt_hint": "高级简洁，成年女性，干净电商棚拍气质",
  "note": "来自模特库，未试穿商品"
}
```

### 7.2 和年龄段的关系

当前项目创建页把年龄段拼进 `user_prompt`。模特库第一版可以从 `user_prompt` 推断默认 tab，但更推荐在下一次迭代把结构化创建参数写入 `workflow_runs.metadata_jsonb`：

```json
{
  "model_profile": {
    "age_segment": "adult",
    "gender": "female",
    "appearance_direction": "asian",
    "style_direction": "高级简洁"
  }
}
```

这样模特库默认筛选、候选生成 prompt、配饰年龄适配都能使用同一个事实源，而不是解析自然语言。

## 8. 权限与安全

### 8.1 预设图访问

预设图不应直接暴露为匿名公开路径。推荐使用登录态保护的 API：

```text
/api/workflows/apparel-model-library/items/{item_id}/binary
/api/workflows/apparel-model-library/items/{item_id}/thumb
```

普通用户只能读取预设图，不能修改全局预设索引。

### 8.2 用户图访问

用户收藏和上传条目引用现有 `Image`，继续使用当前图片 API 权限：

```text
/api/images/{image_id}/binary
```

### 8.3 同步权限

同步预设建议限制为管理员或项目作者。

如果当前产品没有明确管理员界面，可以先通过环境变量控制：

```text
APPAREL_MODEL_LIBRARY_SYNC_MODE=admin_only | any_authenticated | disabled
```

默认建议 `admin_only`。

## 9. 同步失败与边界情况

| 场景 | 处理 |
|---|---|
| GitHub 无法访问 | 保留本地旧库，显示上次同步时间和错误 |
| GitHub Contents API 返回异常 | 拒绝本次同步，不写入新索引 |
| 单张图片下载失败 | 跳过该条目，记录错误，其他条目继续 |
| hash 不匹配 | 跳过该条目，标记校验失败 |
| 远端删除某个 preset | 本地保留，标记 `remote_missing=true`，不从用户界面强制消失 |
| 用户隐藏 preset 后远端更新 | 保持隐藏状态 |
| 用户上传重复图 | 可按 `sha256` 提示已存在，但不强制阻止 |

## 10. 分阶段落地

### Phase 1：个人模特库

目标：先让用户能沉淀自己的私有模特资产。

- 新增模特库列表弹窗。
- 用户上传图片并登记到库。
- 候选模特收藏到库。
- 按年龄段/来源筛选。
- 从个人库选择一个用户私有条目并创建 `ready` candidate。

不做 GitHub 同步。

### Phase 2：GitHub 预设同步

目标：项目作者可以维护和分发预设模特。

- 增加 GitHub 预设文件夹同步。
- 增加同步 API。
- 本地缓存到 `lumendata/storage/apparel-model-library/presets`。
- 当前用户的模特库视图混合展示全站预设和该用户自己的条目。
- 选用 preset 时转换为当前用户可引用 `image_id`。

### Phase 3：体验增强

目标：让库更像资产管理工具。

- 编辑标签、重命名、隐藏预设。
- 根据项目年龄段自动推荐。
- 最近使用、收藏优先排序。
- 整组多视图收藏。
- 基于库内模特生成相似候选。

## 11. 推荐的 MVP 决策

为了尽快落地且不破坏现有工作流，推荐第一版按以下方式实现：

1. 模特库入口放在「模特设定」阶段，打开弹窗。
2. 年龄段使用现有创建页枚举，并作为主 tabs。
3. 数据先用文件索引放在 `lumendata/storage/apparel-model-library`。
4. 用户上传和收藏都引用现有 `images.id`。
5. GitHub 预设同步作为第二阶段，预设文件夹结构先定好。
6. 选中库内模特后创建一个 `ready` 的 `ModelCandidate`，仍让用户在候选阶段确认一次。
7. 同步预设默认管理员/作者可见，普通用户只看「上次同步时间」。

这个方案的核心好处是：UI 上用户获得了「全站预设可选、个人模特可收藏/上传」的模特库；工程上后续生成仍然只认 `image_id` 和 `ModelCandidate`，不会把新概念扩散到 worker 和 showcase 生成链路。
