# 项目功能界面层级调整实施报告

## 1. 背景

当前项目入口 `/projects` 直接承载了「服饰模特展示图」的业务页面：上方是模特库入口，下方是服饰项目列表，并且新建项目按钮也在这一层展示。

后续需要继续新增「海报制作」「分镜制作」等项目功能。如果继续把所有功能放在 `/projects` 同一层，入口会变得混杂，也会让「项目」这个一级导航失去清晰的信息架构。

本次调整目标是增加一个项目功能层级：

- 进入「项目」后，先看到项目功能中心。
- 点击「服饰模特图」后，才进入现有的服饰模特图工作区。
- 模特库、已有项目列表、新建项目按钮都收进「服饰模特图」内部。
- 「海报制作」「分镜制作」先做占位，后续再接入真实功能。

## 2. 当前现状

### 2.1 现有路由

```text
/projects
  当前直接展示服饰模特展示图项目列表

/projects/new
  当前是项目模板选择页

/projects/apparel-model-showcase/new
  当前是新建服饰模特展示图页面

/projects/[projectId]
  当前是服饰模特展示图项目详情页

/library
  当前是独立模特库页面
```

### 2.2 关键文件

```text
apps/web/src/app/projects/page.tsx
apps/web/src/app/projects/new/page.tsx
apps/web/src/app/projects/apparel-model-showcase/new/page.tsx
apps/web/src/app/projects/[projectId]/page.tsx
apps/web/src/components/ui/projects/ProjectsIndex.tsx
apps/web/src/components/ui/projects/ApparelWorkflowNewPage.tsx
apps/web/src/components/ui/projects/ApparelWorkflowDetail.tsx
apps/web/src/components/ui/projects/components/ProjectTopBar.tsx
apps/web/src/components/ui/projects/index.ts
```

### 2.3 当前问题

1. `/projects` 同时承担了「项目功能入口」和「服饰模特图项目列表」两个职责。
2. 模特库入口直接暴露在项目一级页面，后续多功能并列后会造成信息层级混乱。
3. 新建项目按钮出现在项目一级入口，但新建动作实际属于具体项目功能。
4. `/projects/new` 已有模板选择页雏形，但它和未来的 `/projects` 功能中心语义重复。
5. 详情页和新建页返回路径都偏向 `/projects` 或 `/projects/new`，调整层级后需要统一回到「服饰模特图」工作区。

## 3. 目标信息架构

调整后的目标结构如下：

```text
/projects
  项目功能中心
  - 服饰模特图
  - 海报制作（后续）
  - 分镜制作（后续）
  - 其他项目功能（后续）

/projects/apparel-model-showcase
  服饰模特图工作区
  - 上方：模特库入口
  - 下方：已有服饰模特图项目列表
  - 新建服饰模特图项目按钮

/projects/apparel-model-showcase/new
  新建服饰模特图项目

/projects/[projectId]
  现有项目详情页，短期保持兼容

/library
  现有完整模特库页面，短期保持不变
```

核心原则：

- `/projects` 只做功能选择。
- 具体项目列表和新建动作下沉到对应功能内部。
- 后续新增功能时，只需要在 `/projects` 增加功能卡片，并为该功能新增独立子路由。

## 4. 页面改造方案

### 4.1 新增项目功能中心

新增组件：

```text
apps/web/src/components/ui/projects/ProjectFunctionHub.tsx
```

职责：

- 作为 `/projects` 的主页面。
- 展示项目功能卡片。
- 当前仅「服饰模特图」可点击。
- 「海报制作」「分镜制作」显示占位状态。

建议功能卡片：

| 功能 | 状态 | 路径 |
| --- | --- | --- |
| 服饰模特图 | 可用 | `/projects/apparel-model-showcase` |
| 海报制作 | 后续 | 暂无 |
| 分镜制作 | 后续 | 暂无 |

文案建议：

```text
标题：项目
说明：选择要创建和管理的项目类型。

服饰模特图：
上传商品图，管理模特库、候选模特、展示图生成和交付流程。

海报制作：
为商品、活动和品牌场景生成海报版式。（后续）

分镜制作：
将商品卖点拆成镜头脚本和画面分镜。（后续）
```

### 4.2 修改 `/projects/page.tsx`

当前：

```tsx
import { ProjectsIndex } from "@/components/ui/projects";

export default function ProjectsPage() {
  return <ProjectsIndex />;
}
```

目标：

```tsx
import { ProjectFunctionHub } from "@/components/ui/projects";

export default function ProjectsPage() {
  return <ProjectFunctionHub />;
}
```

### 4.3 新增服饰模特图工作区路由

新增文件：

```text
apps/web/src/app/projects/apparel-model-showcase/page.tsx
```

内容：

```tsx
"use client";

import { ProjectsIndex } from "@/components/ui/projects";

export default function ApparelModelShowcaseProjectsPage() {
  return <ProjectsIndex />;
}
```

该页面承接现有 `/projects` 的服饰项目列表能力。

### 4.4 调整 `ProjectsIndex`

文件：

```text
apps/web/src/components/ui/projects/ProjectsIndex.tsx
```

调整点：

1. 页面语义从「项目」改为「服饰模特图」。
2. 保留顶部模特库入口。
3. 保留项目列表、筛选、搜索、重命名、删除。
4. 新建按钮统一指向 `/projects/apparel-model-showcase/new`。
5. 增加返回项目功能中心的面包屑或顶部返回语义。

需要替换的主要路径：

```text
/projects/new
  -> /projects/apparel-model-showcase/new
```

建议文案：

```text
移动端标题：服饰模特图
Hero 标题：服饰模特图
说明：管理模特库、商品图分析、模特候选、展示图生成和交付流程。
```

### 4.5 处理 `/projects/new`

`/projects/new` 当前是项目模板选择页。调整后它与 `/projects` 功能中心重复。

建议短期改成重定向：

```tsx
import { redirect } from "next/navigation";

export default function NewProjectPage() {
  redirect("/projects");
}
```

这样可以兼容旧链接，同时避免出现两个功能选择入口。

### 4.6 调整新建页返回路径

文件：

```text
apps/web/src/components/ui/projects/ApparelWorkflowNewPage.tsx
```

当前移动端返回：

```tsx
backHref="/projects/new"
backLabel="返回项目模板"
```

目标：

```tsx
backHref="/projects/apparel-model-showcase"
backLabel="返回服饰模特图"
```

桌面面包屑建议从：

```text
项目 / 新建
```

改为：

```text
项目 / 服饰模特图 / 新建
```

其中：

- `项目` 链接到 `/projects`
- `服饰模特图` 链接到 `/projects/apparel-model-showcase`
- `新建` 为当前页文本

### 4.7 调整详情页返回路径

文件：

```text
apps/web/src/components/ui/projects/ApparelWorkflowDetail.tsx
```

当前移动端返回：

```tsx
backHref="/projects"
```

目标：

```tsx
backHref="/projects/apparel-model-showcase"
```

桌面面包屑建议从：

```text
项目 / 当前项目名
```

改为：

```text
项目 / 服饰模特图 / 当前项目名
```

删除项目成功后的跳转也应从：

```tsx
router.push("/projects");
```

改为：

```tsx
router.push("/projects/apparel-model-showcase");
```

## 5. 兼容策略

### 5.1 项目详情 URL 短期不改

当前详情页为：

```text
/projects/[projectId]
```

短期建议保持不变。

原因：

1. 当前后端 `WorkflowType` 只有 `apparel_model_showcase`。
2. 保持详情 URL 不变可以减少后端、前端查询、历史链接和分享链接的影响。
3. 本次主要目标是界面层级收纳，不需要同时做详情路由重构。

后续新增多个真实项目类型后，可以再考虑升级为：

```text
/projects/apparel-model-showcase/[projectId]
/projects/poster/[projectId]
/projects/storyboard/[projectId]
```

### 5.2 `/projects/new` 兼容旧入口

把 `/projects/new` 重定向到 `/projects`，避免旧入口 404。

### 5.3 `/projects/library` 保持现有重定向

当前 `/projects/library` 已重定向到 `/library`。本次可保持不变。

## 6. 入口同步清单

需要全局检查并统一的路径：

```text
/projects/new
/projects/apparel-model-showcase/new
backHref="/projects"
router.push("/projects")
href="/projects"
```

已发现需要重点检查的文件：

```text
apps/web/src/components/Onboarding.tsx
apps/web/src/components/ui/chat/mobile/MobileEmptyStudio.tsx
apps/web/src/components/ui/projects/ProjectsIndex.tsx
apps/web/src/components/ui/projects/ApparelWorkflowNewPage.tsx
apps/web/src/components/ui/projects/ApparelWorkflowDetail.tsx
apps/web/src/app/projects/new/page.tsx
apps/web/src/app/projects/page.tsx
```

调整原则：

- 顶级项目入口仍然使用 `/projects`。
- 创建服饰模特图项目统一使用 `/projects/apparel-model-showcase/new`。
- 从服饰模特图新建页、详情页返回时，回到 `/projects/apparel-model-showcase`。
- 全局导航里的「项目」Tab 仍然指向 `/projects`。

## 7. 组件导出调整

文件：

```text
apps/web/src/components/ui/projects/index.ts
```

新增导出：

```ts
export { ProjectFunctionHub } from "./ProjectFunctionHub";
```

保留现有导出：

```ts
export { ProjectsIndex } from "./ProjectsIndex";
export { ApparelWorkflowNewPage } from "./ApparelWorkflowNewPage";
export { ApparelWorkflowDetail } from "./ApparelWorkflowDetail";
```

## 8. UI 设计要求

项目功能中心应保持当前产品的工具型界面风格：

1. 信息密度适中，不做营销式 landing page。
2. 卡片用于功能入口，不嵌套卡片。
3. 可用功能和后续占位功能要有清晰状态区分。
4. 移动端首屏应能看出「项目功能中心」的含义。
5. 「服饰模特图」应是第一主入口，视觉权重最高。
6. 占位功能不能让用户误以为已可用。
7. 保持现有暗色主题、边框、hover、focus-visible 规范。

建议卡片状态：

```text
可用：
  边框使用 accent 或 amber tone
  右侧使用 ChevronRight
  整卡可点击

后续：
  普通 border
  opacity 降低
  显示「后续」Badge
  不绑定 href
```

## 9. 实施步骤

1. 新增 `ProjectFunctionHub.tsx`。
2. 在 `index.ts` 导出 `ProjectFunctionHub`。
3. 修改 `/projects/page.tsx`，改为渲染项目功能中心。
4. 新增 `/projects/apparel-model-showcase/page.tsx`，渲染现有 `ProjectsIndex`。
5. 修改 `ProjectsIndex`：
   - 标题改为「服饰模特图」
   - 新建路径改为 `/projects/apparel-model-showcase/new`
   - 保留模特库入口和现有项目列表
6. 修改 `/projects/new/page.tsx` 为重定向到 `/projects`。
7. 修改 `ApparelWorkflowNewPage`：
   - 返回路径改为 `/projects/apparel-model-showcase`
   - 面包屑增加「服饰模特图」
8. 修改 `ApparelWorkflowDetail`：
   - 返回路径改为 `/projects/apparel-model-showcase`
   - 删除成功跳转改为 `/projects/apparel-model-showcase`
   - 面包屑增加「服饰模特图」
9. 全局搜索旧路径，统一入口。
10. 运行前端 lint/build，并做桌面和移动端手动验收。

## 10. 验收标准

### 10.1 路由验收

- 打开 `/projects`，展示项目功能中心。
- 点击「服饰模特图」，进入 `/projects/apparel-model-showcase`。
- 打开 `/projects/apparel-model-showcase`，上方展示模特库，下方展示已有项目列表。
- 点击新建按钮，进入 `/projects/apparel-model-showcase/new`。
- 打开 `/projects/new`，自动回到 `/projects`。
- 打开现有详情页 `/projects/[projectId]`，仍能正常加载。

### 10.2 交互验收

- 「海报制作」「分镜制作」只显示占位，不可点击进入空页面。
- 服饰模特图列表的搜索、筛选、重命名、删除不受影响。
- 新建页上传、参数填写、创建流程不受影响。
- 详情页阶段流转不受影响。
- 从新建页返回时回到 `/projects/apparel-model-showcase`。
- 从详情页返回时回到 `/projects/apparel-model-showcase`。
- 删除项目后回到 `/projects/apparel-model-showcase`。

### 10.3 导航验收

- 顶部和底部导航的「项目」Tab 仍指向 `/projects`。
- 停留在 `/projects/apparel-model-showcase` 时，「项目」Tab 仍高亮。
- 停留在 `/library` 时，现有「项目」Tab 高亮逻辑不变。

### 10.4 响应式验收

至少检查：

```text
375px 移动端
768px 平板
1024px 桌面
1440px 宽屏
```

重点确认：

- 标题不溢出。
- 功能卡片不挤压变形。
- 新建按钮位置符合层级预期。
- 底部 Tab 不遮挡主要内容。
- 面包屑在桌面可读，移动端返回按钮语义正确。

## 11. 风险与注意事项

1. 不要把 `/projects` 继续作为服饰模特图列表入口，否则层级调整不完整。
2. 不要把新建按钮放在项目功能中心，否则会重新混淆「项目」和「具体功能」。
3. 不要提前改详情 URL，避免扩大影响范围。
4. 修改返回路径时，要同时处理移动端 TopBar、桌面面包屑、删除成功跳转。
5. 占位功能必须明确是后续，避免用户点击无反馈。
6. 当前工作区已有多处未提交改动，实施时应只修改项目层级相关文件，避免混入无关变更。

## 12. 后续扩展建议

后续接入「海报制作」「分镜制作」时，可以按同一模式扩展：

```text
/projects/poster
/projects/poster/new

/projects/storyboard
/projects/storyboard/new
```

并在后端扩展 `WorkflowType`：

```text
apparel_model_showcase
poster_creation
storyboard_creation
```

前端列表页可继续按 `useWorkflowsQuery({ type })` 查询不同项目类型，避免各功能互相污染。
