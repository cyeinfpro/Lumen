# Image 一键更新监听器修复 · 2026-09-13

## 生产只读检查

新加坡 ARM 服务器的 Image/Lumen 当前为 1.2.165；API /healthz 返回正常，核心容器健康。
当前 release：`/opt/lumen/releases/releases-20260910-164904`。
上一 release：`/opt/lumen/releases/releases-20260909-061552`。

`lumen-update.path` 已进入 `failed / unit-start-limit-hit`；实际执行单元是
`lumen-update-runner.service`，最后一次退出码为 2，并且此前每隔 5 秒重复重启。
数据挂载与监听路径均指向 `/opt/lumendata/backup`，不是单纯的数据目录配置不一致。
最后一份宿主机 journal 为 `complete`；检查时没有遗留 request、trigger、running 或 resume 标记。

代码复现了一个足以导致此故障的路径：CLI 更新也写 `.update-resume`，
`PathExists` 立即唤醒只接受已验证 API 请求的 runner；CLI 没有这份请求，runner 拒绝，
反复重试耗尽 systemd 启动额度，连带让 watcher 失效。生产的逐条拒绝日志未进一步读取。

## 本次代码修复

1. 只有带 API operation ID 与合法 request SHA-256 的更新才发布自动恢复标记；
   CLI journal 仍持久保存，并通过 `LUMEN_UPDATE_RESUME=1` 显式恢复。
   标记改为在匹配的 journal 持久化成功之后发布，初始化失败不会覆盖旧恢复状态。
2. 安装和更新刷新入口时，显式重置更新 watcher/runner 的失败额度，并重启 watcher
   以加载最新监听路径；不重启正在执行更新的 runner。
3. API 触发失败写入明确的失败步骤。刷新页面后，失败不再只依赖临时红色提示条。
4. CLI runner 在最终 readiness 和 journal 提交成功之后输出 `complete` 记录。
   前端只有明确成功记录才显示完成和自动刷新；预拉取、仅版本检查、清理步骤完成、
   没有运行标记或连接中断均不等于部署成功。

保留现有请求校验、所有权交接、恢复证据、数据库备份和版本签名验证；
没有放宽 runner 对缺失请求的拒绝策略。

## 回归覆盖

新增测试覆盖 CLI/自刷新后的 CLI 不误唤醒、API journal 先持久化后唤醒、
journal 初始化失败不发布新标记、失败 watcher 重置顺序、重置失败传播、
不重启当前 runner、最终成功证明顺序、API 超时持久失败、日志写失败不吞原错误，
以及前端空闲/中断/失败/预拉取/真实完成/延迟成功刷新判定。

## 生产恢复步骤（本次尚未执行）

后续 SSH 调用被会话安全检查拦截，因此服务器尚未修改，没有新建生产备份，
也没有完成部署或端到端验收。以下仅是恢复步骤，不能视作执行记录。

先确认没有活动更新或遗留恢复标记；若存在，不要删除，应先恢复该任务。
在已确认 journal 完成且不存在待处理请求的情况下，重新启动监听器：

```bash
set -e
for marker in /opt/lumen/shared/.update-resume \
  /opt/lumendata/backup/.update.request.json \
  /opt/lumendata/backup/.update.running \
  /opt/lumendata/backup/.update.trigger; do
  test ! -e "$marker" || { echo "存在待处理更新状态，请先检查：$marker"; exit 1; }
done
systemctl reset-failed lumen-update.path lumen-update-runner.service
systemctl enable --now lumen-update.path
systemctl is-active lumen-update.path
```

待 v1.2.168 的 Docker Release 成功并可供 stable 渠道使用后，通过管理面板再次触发更新。
更新完成需要同时核验 API 版本、/readyz、Worker readiness、Web、实际镜像版本、
宿主机 journal complete、请求清理及 watcher 的 active/waiting 状态。

保持上一 release 可回滚，不对同机其他项目或数据执行清理操作。

## 本地验收记录

版本同步检查、相关 Shell 语法检查、更新/安装/权限专项测试、API 更新测试、前端生产构建均通过。
前端新增测试、TypeScript 类型检查、UI/架构/复杂度检查与 ESLint 的结果由独立质量操作记录。
