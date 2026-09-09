# GPT Image 模型选择

核对日期：2026-09-09。

## 官方接口约定

依据 [OpenAI Image generation](https://developers.openai.com/api/docs/guides/image-generation#customize-image-output)：

| 模型 | 可选渲染质量 |
| --- | --- |
| `gpt-image-2`（兼容默认值） | Low、Medium、High |
| `gpt-image-2.5-flare` | Low、Medium、High、Xhigh、Max |
| `gpt-image-2.5-sunburst` | Low、Medium、High、Xhigh、Max |

API 兼容 `auto`；Lumen 沿用现有处理，将 `auto` 解析为 Medium。分辨率档位
1K / 2K / 4K 与渲染质量独立。三个模型沿用相同的尺寸边界：两边为 16 的倍数，
最长边不超过 3840，宽高比不超过 3:1，总像素为 655,360 至 8,294,400。
透明背景使用 PNG 或 WebP；JPEG 会自动调整为 PNG。

Image API 通过请求的 `model` 选择图片模型；Responses API 的顶层 `model`
仍为独立的推理模型，图片模型写入 `tools[].model`，两者不能混用。

## Lumen 行为

- 桌面生图快捷栏、执行设置及手机执行设置提供三个模型的菜单。画布生图/编辑节点
  使用同一模型清单。切回 GPT Image 2 时，Xhigh / Max 自动回到 High。
- API 的 `image_params.model` 保存所选模型。参数校验拒绝未知模型，以及
  GPT Image 2 + Xhigh / Max 的组合。
- 文生图 JSON、图生图/遮罩编辑 multipart、image-job 异步任务和显式启用的
  Responses 图片工具均透传模型与质量。批任务、重新生成及重抽保留原选择。
- `Generation.model`、请求/生效参数和管理后台记录保留实际图片模型。
- 计费继续使用现有分辨率价格规则；未引入模型差价。官方两个 2.5 模型的 token
  单价与 GPT Image 2 相同，但每张图片的 token 数量可能不同。

## `/responses` 默认关闭

默认 `IMAGE_ENGINE=image2`，后台默认值与 Worker/API 一致。直连失败只执行
Provider 重试；image-job 在此模式锁定 `generations` 端点，不会自动尝试
`responses`。遮罩编辑继续走图片编辑端点。

后台仍可显式选择 `responses` 或 `dual_race`。已有数据库或环境变量中的显式
引擎配置及旧配置迁移保持兼容；若原部署已显式配置 Responses，需要在后台
将“生图引擎”切换为直连。默认值调整不会覆盖管理员保存的选择。

## 验证范围

回归测试覆盖三个模型各个质量的实际 JSON/multipart 请求体、异步请求体、
Responses 顶层模型与图片工具模型分离、默认禁用及无隐式回退、重新生成参数
保留、前端参数恢复与切换，以及桌面/手机浏览器菜单和提交 payload。
这些测试使用模拟上游，不代表生产 Provider 已开放两个新模型的账号权限。
