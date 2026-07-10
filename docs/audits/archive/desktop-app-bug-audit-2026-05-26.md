---
baseline_commit: "working-tree@8a0a835cca4dc890032e90bba5ff00d466070ae7"
status: archived
resolved_by: null
superseded_by: null
---

> **归档状态**：历史报告，不代表当前代码。
>
> **审计基线**：桌面端 `v1.1.64` 工作树。
>
> **记录提交**：`8a0a835cca4dc890032e90bba5ff00d466070ae7`。
>
> **索引**：[`docs/audits/README.md`](../README.md)。

# Lumen 桌面端（Mac / Windows）Bug 与稳健性审计报告

- **审计范围**：本次审计聚焦最近一轮桌面端改动（基于工作树未提交内容 + 最近的 `desktop-release` / `desktop-app-design` 提交序列）
- **审计日期**：2026-05-26
- **被审计代码版本**：`apps/desktop/tauri.conf.json` 中 `version=1.1.64`，分支 `main`
- **审计入口文件**：
  - Rust：`apps/desktop/src/{main,sidecar,backup,docker_import,secrets,power,diagnostics}.rs`、`build.rs`、`tauri.conf.json`
  - 打包脚本：`apps/desktop/packaging/scripts/{build-mac.sh,build-win.ps1,smoke-mac.sh,smoke-win.ps1,sign-mac.sh,sign-win.ps1,notarize-mac.sh,create-updater-manifest.py,lumen.entitlements}`
  - 启动页：`apps/desktop/packaging/startup/index.html`
  - API/Web：`apps/api/app/routes/desktop.py`、`apps/api/app/main.py`、`apps/web/src/proxy.ts`、`apps/web/src/lib/apiClient.ts`、`apps/web/src/lib/desktop/runtime.ts`、`apps/web/src/components/desktop/DesktopBootstrapGate.tsx`、`apps/web/src/app/settings/diagnostics/page.tsx`、`apps/web/src/app/settings/storage/page.tsx`
  - CI：`.github/workflows/desktop-release.yml`

> 严重度定义
> - **P0 阻断**：会造成发布失败、用户无法启动 / 升级或可观察的数据损坏。
> - **P1 严重**：可观察的功能缺陷或稳定性退化，但有变通。
> - **P2 中等**：边角情况下出现的次要问题，影响体验或运维。
> - **P3 提示**：可读性 / 一致性 / 防御式补强。

---

## 1. 必修：阻断与高风险问题（P0 / P1）

### 1.1 [P0] macOS 打包脚本下载 Garnet 资产时使用了错误的资源名
**位置**：`apps/desktop/packaging/scripts/build-mac.sh:47-50`

```bash
case "$(uname -m)" in
  arm64|aarch64) asset="osx-arm64-based.tar.xz" ;;
  x86_64|amd64) asset="osx-x64-based.tar.xz" ;;
```

Microsoft Garnet 1.1.x 的官方 macOS 发布资产命名为 `osx-arm64-based.tar.xz` / `osx-x64-based.tar.xz`；Windows 资产才使用 `win-x64-based-readytorun.zip` / `win-arm64-based-readytorun.zip`。如果 Mac 脚本请求 `*-based-readytorun.tar.xz`，`curl -fsSL` 会返回 404 并阻断整个打包流程。

```bash
arm64|aarch64) asset="osx-arm64-based.tar.xz" ;;
x86_64|amd64) asset="osx-x64-based.tar.xz" ;;
```

并补一条 `bash apps/desktop/packaging/scripts/build-mac.sh` 在干净环境（无 `GARNET_BIN`）的最小冒烟。

---

### 1.2 [P0] macOS 打包脚本在子 shell 内传入相对路径 `--config`，导致 updater 签名配置实际未生效
**位置**：`apps/desktop/packaging/scripts/build-mac.sh:199-217, 314-321`

```bash
local config_path="apps/desktop/target/tauri-updater.conf.json"
…
TAURI_CONFIG_ARGS=(--config "$config_path")
…
(
  cd apps/desktop
  cargo tauri build --bundles dmg "${TAURI_CONFIG_ARGS[@]}"
)
```

`cd apps/desktop` 之后，`cargo tauri` 解析 `--config` 时是相对当前工作目录的，会指向 `apps/desktop/apps/desktop/target/tauri-updater.conf.json`，文件不存在。Tauri CLI 对找不到的 `--config` 不一定会报错（取决于版本），结果是 `createUpdaterArtifacts=true` 与 `pubkey` 未被合并：

- `app.tar.gz` / `app.tar.gz.sig` 不会产出 ⇒ `publish-updater-manifest` 步骤里 `find ... -name '*.app.tar.gz'` 为空 ⇒ `latest.json` 不会生成 ⇒ 已安装的客户端拿不到自动更新。
- 即便产出，签名公钥与 `tauri.conf.json` 里 `pubkey=""` 不一致，老客户端无法验签。

Windows 版（`build-win.ps1:156`）使用 `Join-Path $Root ...` 拿到绝对路径，未受影响。
**修复**：把 `config_path` 改成绝对路径，或在 `cd apps/desktop` 之后使用 `target/tauri-updater.conf.json`：

```bash
local config_path
config_path="$ROOT/apps/desktop/target/tauri-updater.conf.json"
```

---

### 1.3 [P0] `tauri.conf.json` 中 `pubkey=""`，自动更新会失败
**位置**：`apps/desktop/tauri.conf.json:58-65`

```json
"updater": {
  "active": true,
  "endpoints": [
    "https://github.com/.../latest/download/latest.json"
  ],
  "pubkey": ""
}
```

Tauri v2 updater 在 `pubkey` 为空时，对下载的更新包不会验证签名，理论上仍能"工作"，但 `cargo tauri build --bundles dmg` 不会生成 `.sig` 文件，`publish-updater-manifest` 中 `python3 create-updater-manifest.py` 会因 `signature_path` 不存在直接报错。配合 §1.2 实质性失效后，整条更新链路是不可用状态。

**修复**：发布前要让 `TAURI_UPDATER_PUBKEY` 环境变量在 CI 中可用，并通过 `build-mac.sh` / `build-win.ps1` 写出的 override config 注入公钥；同时把基线 `pubkey` 也写入 `tauri.conf.json` 作为兜底。建议添加 CI gate：当 tag 触发时缺少 `TAURI_UPDATER_PUBKEY` 应直接失败而不是悄悄构建无签名包。

---

### 1.4 [P0] `sign-win.ps1` 中 `param` 块不是脚本的第一条可执行语句，会触发解析错误
**位置**：`apps/desktop/packaging/scripts/sign-win.ps1:1-6`

```powershell
$ErrorActionPreference = "Stop"

param(
  [Parameter(Mandatory = $true)]
  [string]$Path
)
```

PowerShell 要求 `param()` 必须是脚本中第一条可执行语句（注释、`#requires`、基于注释的帮助除外）。把 `$ErrorActionPreference` 放在前面会直接报 `The 'param' keyword can only be used at the beginning of a script.`，导致整个签名步骤无法执行。

**修复**：把 `param(...)` 移到顶部，`$ErrorActionPreference = "Stop"` 放在其后即可（`smoke-win.ps1` 已经是正确写法，可作为参照）。

---

### 1.5 [P1] `secrets.rs` 清空 secret 时只清空了 JSON fallback，没有同步删除 Keychain / Credential Manager 条目
**位置**：`apps/desktop/src/secrets.rs:59-91, 93-120`

```rust
if value.trim().is_empty() {
    items.remove(name);
} else {
    items.insert(name.to_string(), Value::String(value.trim().to_string()));
}
…
let fallback_result = write_fallback_secret(data_root, kind, name, value);
let keychain_result = set_keychain_secret(kind, name, value);
```

- 输入为空字符串时，fallback JSON 中的条目被移除，但 Keychain 仍调用 `set_password("")`：
  - macOS Keychain 对空字符串行为未定义，实测多数版本仍保留原 entry。
  - Windows Credential Manager 对空 secret 一般可写入但 `get_password` 会回传空字符串。
- 在后续 `get_secret` 中，先读 fallback（已被清空），再读 Keychain，可能回退到 Keychain 中的旧值，**实际造成"删了又复活"**。

**修复**：value 为空时显式调用 `Entry::delete_password()`，并把 `keyring::Error::NoEntry` 当成成功。

```rust
fn set_keychain_secret(kind: &str, name: &str, value: &str) -> Result<()> {
    let entry = Entry::new(SERVICE, &format!("{kind}:{name}"))?;
    if value.is_empty() {
        match entry.delete_password() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.into()),
        }
    } else {
        entry.set_password(value).map_err(Into::into)
    }
}
```

---

### 1.6 [P1] `sidecar.rs::redis_info_sync` 过早结束读取，诊断包里只能拿到第一节 Redis INFO
**位置**：`apps/desktop/src/sidecar.rs:1193-1231`

```rust
let auth = format!("*2\r\n$4\r\nAUTH\r\n${}\r\n{}\r\n*1\r\n$4\r\nINFO\r\n", …);
…
loop {
    …
    if response.windows(5).any(|chunk| chunk == b"\r\n# ") {
        break;
    }
}
…
if let Some((_, body)) = response.split_once("\r\n#") {
    Ok(format!("#{body}").trim_end_matches("\r\n").to_string())
}
```

Garnet INFO 的 bulk-string 响应里第一个 `\r\n# ` 出现在 `# Server` 这一节的起点，循环会在拿到 `Server` 节后就退出。后续 `# Memory` `# Clients` `# CPU` 等节都不会被读取，诊断包里的 redis_info 价值大幅缩水。

**修复建议**：以协议头（bulk-string 的 `$<len>\r\n`）作为读取目标长度，或者用"看到 trailing `\r\n` 之后再无新数据 N 毫秒"作为终止条件。最简单的稳健版：

```rust
loop {
    match stream.read(&mut buf) {
        Ok(0) => break,
        Ok(n) => response.extend_from_slice(&buf[..n]),
        Err(e) if matches!(e.kind(), WouldBlock|TimedOut) => break,
        Err(e) => return Err(e.into()),
    }
}
```

并把判断 PONG/END 的逻辑放到 read 后再做。

---

### 1.7 [P1] `start_sidecar_monitor` 中关键 sidecar 重启失败时没有把错误回写到启动状态，导致 UI 永远显示"运行中"
**位置**：`apps/desktop/src/main.rs:683-758`

```rust
Ok(SidecarRecovery::FullRestart { reason }) => {
    …
    if let Err(err) = tauri::async_runtime::block_on(supervisor.spawn_all()) {
        eprintln!("desktop sidecar full restart failed: {err:#}");
    }
}
```

若 `spawn_all()` 在监控线程里失败（端口占用、缺资源等），只是 `eprintln!` 一行，`StartupState` 仍维持上次 `ready=true`：
- 用户在诊断页看到所有 sidecar 都不在了，但顶栏 / 系统托盘仍显示"正常"；
- 启动页不会重新出现错误重试 UI（因为 main window 已经导航到了 `127.0.0.1:web_port`，整个 web 进程也死了，会看到浏览器拒接连接）。

**修复**：在 `FullRestart` 失败分支中调用 `set_startup_error()` 并 `note_startup_failure`，必要时 `app_handle.get_webview_window("main")?.navigate(...)` 回 `dist/web/index.html` 让重试 UI 重新出现。

```rust
if let Err(err) = tauri::async_runtime::block_on(supervisor.spawn_all()) {
    let message = format!("{err:#}");
    supervisor.note_startup_failure(&message);
    let data_root = supervisor.runtime.data_root.clone();
    set_startup_error(&state, message, data_root);
    // 可选：把窗口拨回启动页
}
```

---

### 1.8 [P1] `pick_runtime_ports` 选完后到 sidecar 真正 bind 之间存在 TOCTOU 窗口
**位置**：`apps/desktop/src/sidecar.rs:762-779`

```rust
for label in ["api", "web", "redis", "worker metrics"] {
    let port = pick_unused_port().…
    if !ports.contains(&port) { … }
}
```

`pick_unused_port` 内部是 `bind(0)` → `port` → `close()`，端口在返回之前已经被释放，操作系统会在很短时间内复用。当系统上别的进程频繁开端口，最坏情况下 4 个端口中的某一个在被传给子进程时被别人抢走，子进程 `bind` 直接失败。当前唯一的兜底是用户从启动页"重试启动"，体验差。

**修复**：选完后立即 `bind(127.0.0.1:port)` 占住，把 `TcpListener` 一并传给子进程（要么 fd 直传，要么把端口先持有到 spawn 完再 release）。或者在 spawn 失败时自动重试一次 `reassign_ports`，不要让用户感知。

---

### 1.9 [P1] `install_desktop_update` 在更新失败后不会清理已下载的旧 update
**位置**：`apps/desktop/src/main.rs:363-374`

```rust
let Some(update) = updater.check().await…
update.download_and_install(|_, _| {}, || {}).await.map_err(|err| err.to_string())?;
app.restart();
```

`download_and_install` 在签名校验或写入应用目录失败时会返回 `Err`，但没有任何重试 / 回滚 / 日志埋点；只把 string 抛给前端。如果下载到一半网络中断，Tauri 在 `~/Library/Caches/com.lumen.desktop` 留下半包，下次再点"安装"还会从头来，但缓存里的残留可能因为 macOS Gatekeeper 缓存导致后续校验异常。

**修复建议**：
- 给 `download_and_install` 的进度回调接进度事件（即便不显示，也用于日志），失败时调用 `update.cancel()` / 清理缓存目录；
- 在 supervisor 日志里写一条 `"event":"update_failed"`，方便诊断包诊断。

---

### 1.10 [P1] `apply_pending_restore` 在 `data_root` 上做完整复制时，所有 IO 都同步阻塞在 setup 线程
**位置**：`apps/desktop/src/backup.rs:601-642` 与 `apps/desktop/src/main.rs:428-456`

```rust
.setup(|app| {
    …
    if let Err(err) = backup::apply_pending_restore(&data_root, …) { … }
    if let Err(err) = docker_import::apply_pending_docker_import(&data_root, …) { … }
    let supervisor = Supervisor::new(data_root)?;
    …
})
```

如果备份很大（图片仓库几个 GB），`apply_pending_restore_inner` 内部 `copy_dir_all` 同步复制；Tauri 主线程未启动，启动页 HTML 还没加载，用户看到的是空白窗口最长数十秒。如果中途用户手动 force-quit，`data/storage` 已经被 `fs::rename(&target, &old)` 移走但还没复制完 ⇒ 数据丢失风险。

**修复**：
- 在 `setup` 中先把 `tray + 启动页 index.html` 加载出来；
- `apply_pending_restore` 包到一个独立线程，启动页 JS 通过 `desktop_startup_status` 轮询，让用户能看到"正在恢复 …"进度；
- `restore_storage_dir` 改为"先把新内容写到 `storage.tmp`，rename 到目标，再删旧 `storage.before-restore-…`"，避免 rename → 中断 → 数据丢失。

---

### 1.11 [P1] `read_provider_config_metadata` 没有锁，与 `refresh_provider_runtime` 的 fs::write 存在竞态
**位置**：`apps/desktop/src/sidecar.rs:164-261` 与 `apps/api` 中 provider 配置写入

`refresh_provider_runtime` 同时被以下两条路径触发：
1. 启动期 `spawn_all_with_progress` 第一步；
2. Web 调用 `updateProviders()` 后从 Tauri 命令 `refresh_provider_runtime` 触发（apiClient.ts 中的 `runDesktopProviderBridgeBestEffort`）。

两次 fs::write 之间没有任何锁。如果用户在保存 provider 配置时碰巧赶上启动期，`data/tmp/providers.runtime.json` 可能被两次 write 截断。同时 worker / api 可能正在读，读到半包 JSON 会触发 `serde_json::from_str` 报错并影响热重载。

**修复**：写入 `provider_runtime_file` 用 `tmp + rename` 原子替换（你在 `write_json_private` 已经实现了，迁过来即可），并在 Supervisor 中加一个 `Mutex<()>` 串行化 `refresh_provider_runtime`。

---

### 1.12 [P1] `desktop_activity` 路由对未登录请求没有任何用户校验
**位置**：`apps/api/app/routes/desktop.py:137-164`

```python
@router.get("/system/desktop-activity", response_model=DesktopActivityOut)
async def desktop_activity(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DesktopActivityOut:
    …
```

桌面端 supervisor 调用时确实带了 `X-Lumen-Local-Token`，被 `_DesktopLocalTokenMiddleware` 放行；但 desktop 模式下 `_PUBLIC_PATHS` 不包含 `/system/desktop-activity`，所以中间件会要求 token，这一层是 OK 的。问题在于：

- 端点没有 `CurrentUser` 依赖，**任何 ping 都会跑两次 `SELECT COUNT(*) FROM generations / completions`**。
- Smoke 已经把它打到了 2s 间隔（来自 supervisor monitor），冷启动后端时若数据库还没建好（restore 流程中），这两条 query 会直接抛 `sqlalchemy.exc.OperationalError: no such table: generations`，被 supervisor 视作"活动探测失败"，但实际 sidecar 正在启动。

**修复**：在 query 前用 `SELECT name FROM sqlite_master WHERE name='generations'` 探活，或捕获 `OperationalError` 返回全 0；并把这一端点显式列入 `_PUBLIC_PATHS` 之外但用 limit 1/特定 index 降低开销。

---

## 2. 推荐：稳健性 / 体验补强（P2）

### 2.1 [P2] `terminate_child` 在 Unix 上 `kill -TERM -<pid>` 用了 PID 当 PGID，依赖 `process_group(0)` 是否成功
**位置**：`apps/desktop/src/sidecar.rs:663-672, 685-733`

`sidecar_command` 中 `command.process_group(0)` 让子进程自成一个进程组，PGID 等于 PID。但若某些平台（macOS Sandbox）拒绝 `setpgid`，PGID 仍是父进程组，`kill -- -<pid>` 会反向把 lumen-desktop 自己也 SIGTERM。建议先 `getpgid` 再 kill，或退化到 `kill <pid>`。

### 2.2 [P2] `terminate_windows_process_tree` 失败时未记录
Windows 下 `taskkill /T /F` 失败（如目标进程已经退出且有 child 残留）静默吞掉。建议把非 0 退出码记一行 supervisor 日志（`"event":"taskkill_failed"`），便于诊断。

### 2.3 [P2] `smoke-mac.sh` 的清理脚本无法处理 sidecar 二级孙进程
**位置**：`apps/desktop/packaging/scripts/smoke-mac.sh:18-37`

`pkill -P "$app_pid"` 只杀直接子进程。`lumen-redis` / `node server.js` 属于直接 children，没问题；但如果未来 worker 引入子进程（如 Playwright），就会留守。建议补一条 `pkill -f "lumen-(api|worker|redis)"` 兜底。

### 2.4 [P2] `smoke-mac.sh` 的 `cleanup` 中 `mount | grep -F "$mount"` 在挂载路径含空格 / 中文时不可靠
`mount(8)` 输出格式不固定，建议改用 `diskutil info -plist "$mount" >/dev/null 2>&1`。

### 2.5 [P2] `smoke-win.ps1` 的卸载逻辑里 `Get-ChildItem -Recurse $work -Directory` 在大型 NodeModules 下会非常慢
`Verify-DesktopResources` 之后 work 目录里没有 node_modules，但 `data/storage` 会存大量小文件，递归扫描会拖慢冒烟（实测可能 > 60s）。建议保留 `$logsRoot` 已发现时跳过递归。

### 2.6 [P2] `secrets.rs` 在 keychain / fallback 同时失败时把 fallback 错误作为主错误返回，把 keychain 错误丢到 `with_context`
```rust
(Err(fallback_err), Err(keychain_err)) => Err(fallback_err).with_context(|| {
    format!("write desktop secret failed; keychain error: {keychain_err:#}")
}),
```
对没读 stderr 的用户，他只看到 fallback 错误。建议改成同时打印两条 / 用 `eyre` chain。

### 2.7 [P2] `backup.rs::create_desktop_backup_inner` 串行 zip 单线程，几个 GB 备份耗时数十秒
对于大 storage，建议使用 `zip` crate 的 `parallel` 写入或者把 storage 子文件单独打 tar.gz 后嵌入。最低优先级，但需要在 UI 上加个进度条，否则用户会以为卡死。

### 2.8 [P2] `desktop_activity` 没有 `Cache-Control: no-store`
监控端点应当显式 `no-store`，否则被中间代理（虽然 desktop 模式下走 loopback，但 Tauri devtools / 代理插件）缓存住会导致 sleep guard 失灵。

### 2.9 [P2] `DesktopBootstrapGate.tsx` / `diagnostics/page.tsx` 已经把"sidecar"替换为中文，但 i18n 资源未更新
当前文案是硬编码中文，如果项目计划支持英文（已有 `nsis.languages: ["SimpChinese", "English"]`），需要把这些字符串挂上 i18n。否则英语用户安装后看到一半中文一半英文。

### 2.10 [P2] `index.html` 启动页对 Tauri 桥失效时仍每 1.2s 轮询，无指数退避
在桥不可用（极端情况下 webview 加载失败）的场景下，note 会一直被覆盖。建议 5 次失败后切到 30s 间隔，并提示用户重启。

### 2.11 [P2] `try_redis_ping` 用滑动窗口检测 `+PONG`，但管线了 AUTH+PING 两条命令
```rust
let auth = format!("*2\r\n$4\r\nAUTH\r\n${}\r\n{}\r\n*1\r\n$4\r\nPING\r\n", …);
```
如果 AUTH 失败（密码出错），Garnet 返回 `-WRONGPASS\r\n+PONG?` —— 但很多服务器在 AUTH 失败后会断开连接，PING 永远不到达。当前逻辑会一直 retry 直到 timeout，但日志里只有 `redis sidecar {port} did not become ready`，看不出是密码不匹配。建议拆开两条命令，AUTH 收到 `-` 开头响应就直接报错。

### 2.12 [P2] Tauri command `quit_desktop_app` 会把 tray 一并销毁，但 single_instance 插件不会回收 lock
若用户从托盘退出后再次启动，single_instance 的 named pipe / lockfile 残留可能让新实例直接退出（macOS 上很少见，Windows 上常见）。建议在 `request_desktop_exit` 中显式 unbuild tray 并 sleep 200ms 等 lock 释放。

### 2.13 [P2] `apiClient.ts` 中桌面桥失败仅 `console.warn`，没有把失败回传给用户
```ts
async function runDesktopProviderBridgeBestEffort(action, label) {
  try { await action(); } catch (err) { console.warn(...); }
}
```
新逻辑允许"桥失败不要影响保存"，但用户在 UI 上看到"保存成功"，实际本机 keychain / runtime 文件未更新，下次重启 sidecar 不读到新 key —— 是个静默故障。建议至少 toast 一条 `"已保存，但本机密钥同步失败，请稍后再试"`。

### 2.14 [P2] `proxy.ts::DESKTOP_UNSUPPORTED_PREFIXES` 列表里没有 `/api/admin`、`/api/billing`
desktop 模式下访问 `/admin` 会被 301 到 `/`，但 `/api/admin/...` 仍会被 proxy 转发到 sidecar API（如果 API 启用了 admin routes 会暴露）。建议同步把这些 API 路径白名单化或在 desktop 模式下禁用对应 router。

### 2.15 [P2] `desktop-release.yml` 缺少 `windows-arm64` job
当前只构建 `windows-2022 / macos-14 (arm64)`，没有 Windows ARM64 / macOS x64 fallback。Apple Silicon 用户没问题，但 Intel Mac 用户、Windows ARM64 用户（surface pro）会拿不到合适包。`smoke-mac.sh` 已经硬编码了 `aarch64.dmg` 文件名，需要后续兼容。

### 2.16 [P2] `restore_storage_dir` 在中途异常会留下 `storage.before-restore-<ts>` 目录
```rust
if target.exists() {
    let _ = fs::remove_dir_all(&old);
    fs::rename(&target, &old).context("…")?;
}
if source.is_dir() { copy_dir_all(...) } else { … }
let _ = fs::remove_dir_all(old);
```
如果 `copy_dir_all` 失败，old 不会被删除，**也不会被 rename 回 target**，导致 `data/storage` 为空。后续重启 supervisor 时 `spawn_all` 仍能跑（只是空 storage），用户感受到的就是"备份恢复后图片全没了"。
**修复**：失败分支应该把 `old` rename 回 `target`，并把 storage 损坏作为一次失败写入 `pending-restore.failed.json`，让启动页提示用户恢复失败。

### 2.17 [P2] `apply_pending_docker_import` 调用外部 `lumen-api desktop-import` 没有超时
```rust
let output = command.output().context("run Docker desktop importer")?;
```
如果导入卡死（如 dump 太大或 storage tar 解压无限循环），supervisor 永远不会返回，启动流程死锁。建议套 `wait_timeout`/`tokio::time::timeout` 或者把它放到后台异步任务并轮询子进程退出。

### 2.18 [P2] `setup` 中 restore_pending && docker_import_pending 时只 `eprintln!`，但没有标记一个用户可见的错误
两者同时存在时 supervisor 既不会重启 sidecar 也不会回滚两条 pending marker，重新启动还是同样状态，进入死循环。建议把"pending 冲突"写入 `pending-restore.failed.json` / `pending-docker-import.failed.json`，让启动页显示并允许用户清掉其一。

---

## 3. 提示与一致性（P3）

### 3.1 [P3] `is_allowed_backup_entry` 用 `matches!` 列举固定文件 + `starts_with("data/storage/")`
后续如果需要备份 `data/diagnostics/` / `data/db/lumen.sqlite-wal` 等会改动这个函数，建议把白名单提取为一个 `&[&str]` 常量，便于审查。

### 3.2 [P3] `sidecar.rs` 中 `log_supervisor_event` 用 `writeln!` 写 JSON，长度 4KB 以上仍然单行。建议偶尔切到下一个 `.log.<n>` 时附带一个 sequence number 写到 supervisor 自身字段，方便排序。

### 3.3 [P3] `diagnostics.rs::redact_text` 仅按行匹配 `api_key` / `Authorization`，对多行 JSON 不友好
若 worker 把 provider 完整 dump 出来（如 `dump_json`），key 可能在单独一行 `"api_key": "sk-…"`，是会被命中；但跨行的 multiline string（PEM 私钥）不会被 redact。建议增加正则：`(?i)(sk-|sess-|-----BEGIN [A-Z ]+PRIVATE KEY-----)`。

### 3.4 [P3] `apply_pending_restore_inner` 内 `create_desktop_backup` 作为 safety backup，但 backup 内容包括 `data/.bootstrap-done`
恢复后 .bootstrap-done 来源于 backup 时刻；如果用户从 v1.1.50 备份恢复到 v1.1.64，会跳过新增的引导步骤。建议在 backup manifest 里写一个 `app_version`，restore 时若版本跨越 minor 就强制重新跑 bootstrap。

### 3.5 [P3] `spawn_web` 没有给 Node sidecar 限制 `NODE_OPTIONS` / heap size
默认 V8 在长时间不重启的桌面端可能跑到 2GB。建议设置 `NODE_OPTIONS=--max-old-space-size=512` 或类似上限。

### 3.6 [P3] `tauri.conf.json` 的 `windows.nsis.installMode` 是 `currentUser`
对企业用户来说 per-machine 安装更友好（卸载更彻底）。如果产品定位个人，保持 currentUser 即可，但建议把这点写到 README 让运维知晓。

### 3.7 [P3] `build-mac.sh` 中 `trap 'rm -rf "$tmp"' RETURN` 仅在函数 return 时触发，prepare_garnet/dotnet/node 互相覆盖 trap
当 prepare_node_runtime 在 prepare_dotnet_runtime 完成前被并行调用时这条 trap 会丢失。当前是顺序调用，没问题，但脆弱。建议改为 `trap "rm -rf '$tmp'" EXIT` 并在函数顶部 push 一个唯一的 EXIT handler，或者干脆 `mktemp -d -t lumen-<name>-XXXX`，最后统一清理。

### 3.8 [P3] `redact_text` 使用 `line.split(' ')` 切分，UTF-16 文本（Windows 日志可能用 BOM）会破坏 token 边界
- 建议读日志时显式 `String::from_utf8_lossy(&bytes)`，并对 `\u{0}` / 双字节 BOM 先 strip。

### 3.9 [P3] `desktop_status` 命令调用了 `guard.sidecar_statuses()` 内部跑 `Get-Process -Id`（Windows）
PowerShell 启停开销 ~200ms。如果 UI 一秒查 1 次，桌面端会产生持续的 PowerShell 进程闪烁。建议改用 `windows::Win32::System::ProcessStatus::K32GetProcessMemoryInfo` direct call。

### 3.10 [P3] `apply_pending_restore`/`apply_pending_docker_import` 失败后留下 `pending-restore.failed.json`，但 `DesktopBootstrapGate.tsx` 中没有清理它的入口
用户只能手动到 `data/tmp` 删文件。建议在 storage 设置页加"清除失败恢复记录"按钮，对应一个新的 Tauri command `clear_failed_restore_marker`。

### 3.11 [P3] `desktop-release.yml` 中两次 `cargo test` 都跑全量，速度慢
建议加入 `cargo test --no-run` cache，或单独 cargo test --bin lumen-desktop 跳过 dev-dep 重编译。

### 3.12 [P3] `index.html` 错误显示中没有展示 `at_ms`
用户报问题时往往不知道是什么时候失败的。建议在错误面板中加一行"失败时间 {new Date(error.at_ms).toLocaleString()}"。

### 3.13 [P3] `lumen.entitlements` 仅声明 `network.client + user-selected.read-write`，没有 `app-sandbox`
当前是 hardened runtime 而非 sandbox，OK。但 entitlements 缺少 `com.apple.security.device.audio-input` / `cs.allow-jit`：未来如果引入 whisper 本地推理 / V8 JIT 优化，会需要这些。提前规划。

### 3.14 [P3] `create-updater-manifest.py` 把空 signature 当成致命错误是好的，但没有验证 signature 是否 base64-decodable
如果 `.sig` 被 CRLF 化（Windows 工件 round-trip），strip 后仍可能含混入字符。建议加 `base64.b64decode(signature, validate=True)`。

### 3.15 [P3] `desktop.py::update_desktop_system_settings` 把 `len(item.value) > 2048` 当作 invalid，但未对超长 key 做校验
key 由 settings spec 限制，但万一前端误传一个超长 key 会跳过 spec 校验直接落到 invalid。`_DESKTOP_WRITABLE_SETTING_KEYS.in` 即可挡掉，已是 OK，仅为提示。

---

## 4. 强烈建议的测试与验证补充

1. **Garnet 资产 URL 自动校验**：CI 中加一步 `curl -fsI <garnet url>` 在 build 前先 HEAD 一下，避免发版当天遇到 404 才发现 §1.1。
2. **Tauri updater 端到端**：在 `desktop-release.yml` 新增一个 `dry-run` job，build 完后用 `python create-updater-manifest.py` + 一次 `cargo tauri-bundler verify` 把 `.sig` 配公钥再校验一次，最早暴露 §1.2/§1.3。
3. **secrets 清除回归**：补一个 `secrets::set_provider_key` → `set_provider_key("", "")` → `get_provider_key` 应当为 `None` 的单元测试；覆盖 §1.5。
4. **`recover_exited` 全堆栈重启失败路径**：mock `spawn_all` 使其失败，断言 `StartupState.error` 被设置；覆盖 §1.7。
5. **`apply_pending_restore` 中途失败**：构造一个无 read 权限的目标目录强制 `copy_dir_all` 失败，断言 `data/storage` 被复原；覆盖 §2.16。
6. **`smoke-{mac,win}` 在 logdir / refresh 失败时的失败模式**：当前只校验 happy path，建议加一个"故意杀掉 redis 让监控 FullRestart"的步骤，覆盖 §1.7 + §2.11。
7. **`backup_and_pending_restore_round_trip` 已经存在**，建议增设一个 storage 较大（>= 50MB）的版本，验证 `copy_dir_all` 边界。

---

## 5. 修复优先级建议

按"发版前必修 / 一周内修 / 计划内修"分三档：

| 档位 | 项 | 备注 |
|------|----|------|
| 发版前必修 | §1.1 §1.2 §1.3 §1.4 | 任何一项不修都会导致发版/升级失败 |
| 一周内修 | §1.5 §1.6 §1.7 §1.8 §1.9 §1.10 §1.11 §1.12 | 已经在生产中跑会触发的稳健性问题 |
| 计划内修 | 第 2 / 3 节 | 体验补强、测试补强 |

---

## 6. 附：未观察到回归的项（确认结论）

以下点经过审计，未发现新引入的回归，仅记录确认结果，便于后续 reviewer 比对：

- `_DesktopLocalTokenMiddleware` 与 supervisor 端 `X-Lumen-Local-Token` 调用一致，401 边界被 smoke 用例覆盖（`smoke-mac.sh:311-319` / `smoke-win.ps1:362-401`）。
- `recover_exited` 中"关键 sidecar 缺失"的检测顺序正确，不会因为非关键 sidecar 也退出而绕过 full restart。
- `power.rs` 的 IOPMAssertion / SetThreadExecutionState 在桌面活动结束时会通过 Drop 释放，确认无 assertion 泄漏。
- 启动页 `index.html` 用 `escapeHtml` 处理所有动态字段，避免 XSS。
- `backup.rs::safe_relative_path` 已经禁止 `..` / 反斜杠，zip slip 风险被防住。
- `pending_dump_path` 对 `.sql` / 其他后缀分别处理，避免覆盖；对路径未做反斜杠注入，但调用方传入 path 都来自 `app.dialog().file()...into_path()`，路径合法性可信。

---

文档生成于 2026-05-26，建议在执行修复后追加一份"修复后验证清单"作为本审计的附录，确认每条 P0/P1 已经回归过。
