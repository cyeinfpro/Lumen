# Lumen 生图请求协议修复（v1.2.180）

日期：2026-11-01

## 根因

用户收到的错误为 `Unknown parameter: 'response_format'.`，HTTP 状态 400。
Worker 的直连流式生成分支在开启 `stream` 时额外注入
`response_format: b64_json`。非流式分支没有该字段，因此同一模型切换到
流式生成后才触发严格上游的参数拒绝。旧回归测试还错误地要求该字段必须存在。

OpenAI 当前 Images API 将 `response_format` 标记为旧模型参数：
GPT Image 模型不支持它，图片本来就以 base64 返回。合法的
`stream: true` 和 `partial_images: 0` 不需要删除，也不需要换端点。

参考：[OpenAI Create image API reference](https://developers.openai.com/api/reference/resources/images/methods/generate)。

## 本次修复

### 1. 直连生成、编辑与流式请求

- 删除流式生成请求中的旧 `response_format` 字段。
- 为 GPT Image 2、2.5 Flare、2.5 Sunburst 增加严格上游模拟：
  遇到旧字段直接返回同样的 400，验证普通 JSON 和流式请求都能交付最终图片。
- 保留模型、质量、透明 WebP、压缩设置、幂等键和流式事件行为。
- 修正旧测试中的错误协议预期。

涉及 `apps/worker/app/upstream_parts/direct_requests.py`。

### 2. HTTP 200 内嵌错误和流式错误

部分兼容网关会用 HTTP 200 携带错误 JSON，或者在 SSE 中返回没有
JSON `type` 的错误信封。原直连提取器会把它们误判为
`no_image_returned` 或 `direct_image_result_unknown`。

- 在生成和编辑共享的图片结果提取器中识别嵌套与扁平错误信封。
- 保留真实 HTTP 状态、上游错误码、消息和 `param`。
- 识别无 `type` 的 SSE 错误信封。
- 从 `response.failed` / `response.incomplete` 的嵌套
  `response.error` 提取真实错误；保留 `incomplete_details.reason`。
- 明确错误沿现有错误策略处理；成功响应无图片、坏 base64 和断流继续保留
  原有错误分类，不把它们当成确定的参数拒绝。

涉及 `direct_images.py` 与 `direct_generation_responses.py`。

### 3. 异步 image-job 参数兼容

- 对已知 GPT Image 模型移除调用方遗留的 `response_format`。
- Responses 仅清理 `image_generation` 工具的该字段；不影响
  `text.format` 或函数工具的参数定义。
- 对未知供应商模型保留其原有 `response_format` 协议。
- 规范化在深拷贝上完成，不修改调用方原始请求。

涉及 `image-job/image_job/payloads.py`。

### 4. 透明输出及异常参数类型

- 透明背景保留合法 PNG / WebP；不支持透明的 JPEG 转为 PNG。
- PNG 不携带 `output_compression`；WebP 保留指定压缩值。
- 删除透明分支的提前返回，继续完成 moderation 规范化。
- 输出格式、背景、moderation 收到数组或对象时按已有默认规则规范化，
  避免集合成员判断触发 `TypeError`，把参数问题升级成 500。

### 5. 异步错误分类与重试

- HTTP 200 JSON、Images SSE、Responses SSE 都识别嵌套、扁平和无类型错误信封。
- `unknown_parameter` / `unsupported_parameter` 分类为参数拒绝，
  保留真实原因，不再误报为缺图、网络中断或上游 5xx。
- 在上游幂等保证开启和关闭两种配置下，都验证参数拒绝只发送一次。
- 真正的中断、未知结果和交付失败仍遵循既有不确定性处理。
- 本次不修改历史任务、账本或生产数据。

涉及 `image-job/image_job/candidates.py`。

## 验证

先用回归测试复现原始失败，再修复请求构造和响应解析。

| 检查 | 结果 |
| --- | --- |
| Worker 生图请求、重试、编辑和协议回归 | 160 passed |
| image-job 全套测试 | 379 passed |
| Core 图片模型与上游计费决策测试 | 71 passed |
| Python Ruff | 通过 |
| 仓库完整性、复杂度、架构检查 | 通过 |
| Facade、死代码、运行时状态、基线单调性检查 | 通过 |
| `git diff --check` | 通过 |
| 版本同步与 `uv.lock` workspace 版本检查 | 通过，1.2.180 |

本地共 610 项相关回归通过。测试使用严格模拟上游，不代表额外发起了真实
付费生成。生产可用性另由正式镜像发布后的版本及服务健康检查确认。

新增测试：

- `apps/worker/tests/test_image_protocol_regressions.py`
- `image-job/tests/test_payload_image_protocol.py`
- `image-job/tests/test_image_error_protocol.py`

## 发布范围

按仓库既有流程发布 `v1.2.180`：同步版本及 lockfile，推送 main 和对应 tag，
等待 tag 触发的 Docker Release 成功，再通过稳定通道更新已关联的生产部署。

异步 image-job 的源代码修复包含在本版本中。生产预检确认当前关联部署没有
独立的 `image-job.service` 或同名容器，本次不额外安装或启用该服务。
