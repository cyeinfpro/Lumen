# Lumen 桌面端（macOS / Windows）深度 Bug 审计

- 审计日期：2026-05-29
- 审计范围：`apps/desktop`（Tauri v2 桌面外壳 + Rust supervisor）、桌面打包/签名/公证脚本、CI 发布流水线（`.github/workflows/desktop-release.yml`）、`apps/web` 中的桌面集成层。
- 审计方式：只读源码审计 + Tauri 2.11.2 源码级追踪 + 近期 commit 回归核对。**本文档只记录问题，未改动任何代码。**
- 代码版本：`v1.1.71`（`tauri.conf.json` / `Cargo.toml`），分支 `main`，HEAD = `b6e4004`。
- 说明：仓库根目录已有的 `DESKTOP_BUG_AUDIT.md` 描述的是**修复前**的旧代码，多项已失效，本文件为反映**当前代码状态**的全新审计。

## 严重程度图例

| 等级 | 含义 |
|------|------|
| **P0** | 致命：直接导致"完全不能正常工作"（下载后无法启动 / 原生功能全线失效） |
| **P1** | 高危：数据安全、数据丢失或核心流程在常见场景下失败 |
| **P2** | 中危：特定场景失败、误判、或掩盖了 P0 的测试盲区 |
| **P3** | 低危：健壮性 / 体验 / 残留线索 |

---

## P0-1　Web UI 运行于"远程源"，空 capabilities 使全部原生 IPC 命令被 ACL 拒绝

**这是"桌面端完全不能正常工作"的首要根因。**

### 现象 / 影响
应用能加载出界面（本地启动页 + 之后的 Web UI 都能显示，HTTP 接口也通），但**所有桌面原生功能全部失效**：保存 API Key、导出/恢复备份、Docker 导入、导出诊断包、重启应用、检查/安装更新、打开数据目录、查看 sidecar 状态——`runtime.ts` 暴露的 17 个命令全部不可用（报错或按钮无响应）。对用户而言就是"装了等于没装核心功能"。

### 根因（已源码级验证）
1. 启动完成后，`main.rs:1005-1009` 把主窗口导航到 `http://127.0.0.1:{web_port}`（Next.js standalone）：
   ```rust
   if let Some(window) = app_handle.get_webview_window("main") {
       if let Ok(url) = format!("http://127.0.0.1:{web_port}").parse::<tauri::Url>() {
           let _ = window.navigate(url);
       }
   }
   ```
2. Tauri 2.11.2 把 `http://127.0.0.1:PORT` 判定为**远程源**（非 `tauri://` 资源、非 `frontendDist` 本地资源）。见 `webview/mod.rs` 的 `is_local_url`（行 1698-1739），三项本地判定均不通过。
3. IPC 入口 `on_message`（`webview/mod.rs:1742`）的 ACL 闸门（行 1819-1852）：
   ```rust
   if (plugin_command.is_some() || has_app_acl_manifest || !is_local)
       && request.cmd != FETCH_CHANNEL_DATA_COMMAND
       && invoke.acl.is_none()
   { /* reject: "Command {} not allowed by ACL" */ }
   ```
   自定义 app 命令从远程源进入时 `!is_local == true`，触发闸门；而本应用**没有任何 capability** 来为远程源放行，于是 `invoke.acl.is_none()` 成立 → **一律拒绝**。
4. 应用确实没有 capabilities：
   - `apps/desktop/capabilities/` 目录**不存在**；
   - `gen/schemas/capabilities.json == {}`（空）；
   - `gen/schemas/acl-manifests.json` 的 key 里**没有** `__app-acl__`，也没有任何自定义 app 命令（即 `has_app_manifest() == false`）；
   - `tauri.conf.json` 里**没有** `app.security.capabilities` 字段，更没有 `remote.urls` 允许列表。

> 对比：导航前的本地启动页（`navigate_startup_page`，`main.rs:1185`，走 `dist/web/index.html` 的 `file://`/资源源）属于本地源，因此 ACL 闸门不触发——这正是"界面能出来、但原生功能全废"的原因。

### 受影响的命令（`apps/web/src/lib/desktop/runtime.ts` → `window.__TAURI__.core.invoke`）
`open_data_dir`、`desktop_status`、`set_provider_key`、`set_proxy_secret`、`refresh_provider_runtime`、`export_diagnostics_bundle`、`export_desktop_backup`、`desktop_restore_status`、`clear_failed_restore_marker`、`clear_pending_restore`、`select_desktop_restore_backup`、`desktop_docker_import_status`、`clear_failed_docker_import_marker`、`select_docker_import_backup`、`restart_desktop_app`、`check_desktop_update`、`install_desktop_update`。

### 修复方向（仅供参考，未实施）
新增 `apps/desktop/capabilities/*.json`，为窗口 `main` 声明对上述自定义命令的权限，并在 `remote` 中显式允许 `http://127.0.0.1:*`（Tauri v2 远程能力需要 `remote.urls`）；同时在 `tauri.conf.json` 引用该 capability。或者改为不导航到远程 HTTP，而是用本地资源源加载前端并以 `connect-src` 访问本地 API（改动更大）。

### 证据索引
- `apps/desktop/src/main.rs:1005-1009`、`:1185-1198`
- `apps/desktop/tauri.conf.json`（无 capabilities 字段；`withGlobalTauri:true`）
- `apps/desktop/gen/schemas/capabilities.json`（`{}`）、`acl-manifests.json`（无 `__app-acl__`）
- `apps/web/src/lib/desktop/runtime.ts:130-259`
- Tauri 源：`webview/mod.rs:1698-1739, 1742, 1819-1852`；`ipc/authority.rs:132`；`tauri-utils/src/acl/mod.rs:50, 348-350`

---

## P0-2　macOS 产物未公证（notarization），下载后被 Gatekeeper 拦截

### 现象 / 影响
用户从 GitHub Release 下载 `.dmg` 安装后，首次打开报"**已损坏，应移到废纸篓**"或"**无法验证开发者**"，应用根本无法启动。Apple Silicon 上对带 quarantine 属性的未公证 app 拦截尤其严格。这是 mac 端"完全不能用"的第二大根因。

### 根因（已验证）
- CI 的 mac job 只执行 `build-mac.sh`，仅设置了 `APPLE_SIGNING_IDENTITY`（`desktop-release.yml:123`）。**全流水线中没有任何 `APPLE_ID` / `APPLE_TEAM_ID` / `APPLE_APP_PASSWORD` / `notarytool` / `stapler` 引用**（grep 结果为空）。
- `packaging/scripts/notarize-mac.sh` 写得正确（`notarytool submit --wait` + `stapler staple/validate`），但**从未被 `build-mac.sh` 或 CI 调用**，是孤儿脚本。
- 同理 `packaging/scripts/sign-mac.sh`（会对所有嵌套 Mach-O 用 `--options runtime --timestamp` 逐一签名）也**从未被调用**。
- `build-mac.sh` 仅依赖 `cargo tauri build` + `APPLE_SIGNING_IDENTITY` 给**外层 app 包**签名；`resources/runtime/**` 下打包的 Python（`.so`/`.dylib`）、Node、.NET、Garnet 等嵌套二进制**不会被 Tauri 自动深度签名**，因而即使将来开启公证也会失败（公证要求所有可执行 Mach-O 都带 hardened runtime 签名）。
- 若 `APPLE_SIGNING_IDENTITY` secret 未配置，`build-mac.sh` 会回退为 ad-hoc 签名 `-`，比未公证更易被拦截。

### 修复方向
在 CI mac job 中配置 `APPLE_ID`/`APPLE_TEAM_ID`/`APPLE_APP_PASSWORD`，构建后依次调用 `sign-mac.sh`（深度签名所有嵌套 Mach-O）→ `notarize-mac.sh`（公证 + staple）。

### 证据索引
- `.github/workflows/desktop-release.yml:121-129`（仅 `APPLE_SIGNING_IDENTITY`）
- `apps/desktop/packaging/scripts/notarize-mac.sh`、`sign-mac.sh`（均未被引用）
- `apps/desktop/packaging/scripts/lumen.entitlements`（hardened runtime，`disable-library-validation` 等）

---

## P0-3　Windows 产物无 Authenticode 签名，SmartScreen 拦截

### 现象 / 影响
Windows 用户下载 NSIS 安装包后，SmartScreen 弹"**Windows 已保护你的电脑 / 未知发布者**"，多数用户会被吓退；企业策略下可能直接禁止运行。

### 根因（已验证）
- `build-win.ps1:318` 的签名分支条件是 `if ($env:WINDOWS_SIGNING_THUMBPRINT -or $env:WINDOWS_SIGNING_CERT_PATH)`，只有设置了这些环境变量才会调用 `sign-win.ps1`。
- **CI 全流水线中没有任何 `WINDOWS_SIGNING_*` / `signtool` / `sign-win` / `WINDOWS_TIMESTAMP` 引用**（grep 结果为空）→ 该分支永不触发 → 产物从不签名。
- `sign-win.ps1` 本身写得正确（`signtool sign /tr /td sha256 /fd sha256` + `verify /pa`，且拒绝自动选证），但形同孤儿。
- 注意：CI 里的 `TAURI_SIGNING_PRIVATE_KEY` 是 **updater 工件签名密钥**，与 Authenticode 代码签名完全不是一回事，不能互相替代。

### 修复方向
在 CI win job 配置 `WINDOWS_SIGNING_THUMBPRINT` 或 `WINDOWS_SIGNING_CERT_PATH`(+`_PASSWORD`)，使 `build-win.ps1` 末尾自动签名 NSIS 产物；理想情况使用 EV 证书以尽快积累 SmartScreen 信誉。

### 证据索引
- `apps/desktop/packaging/scripts/build-win.ps1:318-337`
- `apps/desktop/packaging/scripts/sign-win.ps1`
- `.github/workflows/desktop-release.yml`（无 Windows 代码签名相关 env）

---

## P1-1　NSIS 预卸载钩子用 MessageBox 但无 `/SD`，自动更新时可能弹窗阻塞并**删除全部本地数据**

### 现象 / 影响
`windows/hooks.nsh` 在卸载前弹出"是否同时删除 Lumen 本地数据？"的 `MB_YESNO` 对话框：
```nsis
!macro NSIS_HOOK_PREUNINSTALL
  MessageBox MB_YESNO|MB_ICONQUESTION "是否同时删除 Lumen 本地数据？..." IDNO done
  RMDir /r "$APPDATA\com.lumen.desktop"
  RMDir /r "$LOCALAPPDATA\com.lumen.desktop"
done:
!macroend
```
- Tauri/NSIS 在**更新升级**时会以静默模式 `/S` 运行旧版卸载器。NSIS 的 `MessageBox` **未加 `/SD` 默认值**：在静默模式下该弹窗仍会显示（这是 NSIS 经典坑），从而**阻塞本应无人值守的自动更新**；
- 即便用户在场，若误点"是"，则在一次"升级"中把 `$APPDATA\com.lumen.desktop` 与 `$LOCALAPPDATA\com.lumen.desktop`（含数据库、备份、密钥回退文件）**全部删除**——升级变成清库。

### 修复方向
为该 `MessageBox` 增加 `/SD IDNO`（静默时默认不删数据）；并考虑仅在"用户主动卸载"而非"更新驱动的卸载"时才提示删除数据。

### 证据索引
- `apps/desktop/windows/hooks.nsh:1-6`

---

## P1-2　.NET 运行时下载源 `dotnetcli.azureedge.net` 已被微软停用，全新构建会 404

### 现象 / 影响
mac 与 win 的构建脚本都从 `dotnetcli.azureedge.net` 拉取 .NET 运行时（Garnet/`lumen-redis` 依赖它运行）。微软的 `*.azureedge.net`（Edgio）CDN 已于 2025 年初停用。**在未提供 `DOTNET_RUNTIME_DIR` 的全新构建环境中，该下载会 404**，导致打包失败或缺失 .NET 运行时 → Garnet 无法启动 → Redis 不可用 → 整个 sidecar 链起不来。

### 根因（已验证）
- `build-mac.sh:108`：`https://dotnetcli.azureedge.net/dotnet/Runtime/${DOTNET_RUNTIME_VERSION}/...tar.gz`
- `build-win.ps1:92`：`https://dotnetcli.azureedge.net/dotnet/Runtime/$DotnetRuntimeVersion/...zip`
- 近期 commit `db875d3 "fix: run bundled garnet with packaged dotnet"` 正是依赖这套打包的 .NET runtime，使该下载源成为关键路径。

### 修复方向
改用现行有效源 `https://builds.dotnet.microsoft.com/...`（或官方 `dotnet-install` 脚本/`packages.microsoft.com`），或在 CI 固定提供 `DOTNET_RUNTIME_DIR`。

### 证据索引
- `apps/desktop/packaging/scripts/build-mac.sh:108`
- `apps/desktop/packaging/scripts/build-win.ps1:92`

---

## P1-3　API 密钥始终以明文落盘，keychain 形同虚设；Windows 上明文文件无额外权限保护

### 现象 / 影响
用户保存的 Provider/代理密钥被**明文**写入 `${data_root}/data/tmp/secrets.local.json`（`secrets.rs:9` `FALLBACK_RELATIVE_PATH`）。即使系统 keychain 写入成功，明文副本依然存在，且读取时优先取明文，使 keychain 失去保护意义。

### 根因（已验证）
- `set_secret`（`secrets.rs:133-135`）**同时**写"明文 fallback"和"keychain"，二者任一成功即算成功 → 正常情况下明文始终被写。
- `get_secret`（`secrets.rs:207-217`）**先读明文 fallback**（`:212`），keychain 仅作兜底 → 明文文件是事实上的权威来源。
- `write_fallback_secret` 仅在 `#[cfg(unix)]` 下设置 `0o600`（`secrets.rs:116-119`）。**Windows 上没有等价的权限收紧**，明文密钥文件按默认 ACL 落盘。

### 修复方向
keychain 写入成功时不再保留明文（或对明文做加密/DPAPI 保护）；读取优先 keychain；Windows 下对回退文件应用 ACL 限制（仅当前用户）。

### 证据索引
- `apps/desktop/src/secrets.rs:8-9, 87-122, 133-155, 207-217`

---

## P2-1　Redis AOF "恢复失败"检测靠子串匹配 Garnet 内部类型名，可能每次重启都误删缓存

### 现象 / 影响
`49f3cb3` 新增的恢复逻辑：启动 redis 后，读取本次启动新产生的日志，若包含 `"AofProcessor.RecoverReplay"` 或 `"TsavoriteLogRecoveryInfo"` 即判定"AOF 恢复失败"，于是**隔离整个 AOF 目录并重建缓存**。

风险：这两个字符串是 Garnet/Tsavorite 的内部类型/类名。**如果它们也出现在"正常 AOF 重放"的日志里**（而不仅是失败堆栈），那么每次带 AOF 的重启都会被误判为失败 → 每次重启都隔离 AOF + 重建缓存（丢弃缓存状态，并增加"重建本地缓存"耗时）。

> 检测本身实现得不错（基于 offset 只读"本次启动后"的新日志，避免读到历史失败记录；并有单元测试）。**唯一未验证的是这两个匹配串在 Garnet"成功恢复"日志中是否也会出现**——需对照真实 Garnet 成功/失败日志确认；若会出现，应改为匹配明确的异常/失败标记。

### 证据索引
- `apps/desktop/src/sidecar.rs`：`redis_aof_recovery_failed_since`、`quarantine_redis_aof`、`restart_redis_after_aof_recovery_failure`（`49f3cb3` 引入）

---

## P2-2　macOS 仅构建 aarch64，Intel Mac 无可用产物

- CI 只产出 `Lumen_<version>_aarch64.dmg`（`desktop-release.yml:129` smoke 仅引用 aarch64 dmg），更新清单也只有 `darwin-aarch64`（`:272`）。Intel Mac 用户**没有可安装/可更新的产物**。
- 若产品定位需覆盖 Intel Mac，应增加 `x86_64` 或 universal 构建。

---

## P2-3　CI smoke 在"干净、无 quarantine、未签名直跑可执行文件"环境下运行，掩盖了 P0-2 / P0-3

### 现象 / 影响
`smoke-mac.sh` / `smoke-win.ps1` 直接启动 `target/.../lumen-desktop`(.exe) 这个**裸可执行文件**（headless 模式），在干净的 GitHub runner 上运行、**不带 quarantine 属性、不经过已签名/已公证的安装包**。它们对运行时/接口覆盖很全面（会话、提示词、Provider、记忆、图片、分享、sidecar 重启恢复等），但**结构上无法复现真实下载用户遇到的 Gatekeeper / SmartScreen 拦截**。

后果：P0-2、P0-3 这种"下载后根本打不开"的致命问题在 CI 里永远是绿的，给出**虚假的"通过"信心**。

### 修复方向
增加一条针对"已签名/已公证安装包 + 设置 quarantine 属性"的冒烟（mac 上 `xattr -w com.apple.quarantine` 后 `spctl --assess`；win 上校验 Authenticode 与 SmartScreen 信誉）。

### 证据索引
- `apps/desktop/packaging/scripts/smoke-mac.sh`、`smoke-win.ps1`（直接 `Process.Start` 裸 exe）

---

## P2-4　Node 运行时以符号链接形式打包，Tauri 资源打包/签名可能不保留

- `build-mac.sh:125,151` 与 `build-win.ps1:110`（Windows 走 `New-NodeRuntimeLinkOrCopy`，失败回退 copy）把 `resources/runtime/node/node` 建为指向 `bin/node` 的**符号链接**。
- macOS `.app` 在被打包/`codesign` 时，bundle 内符号链接可能不被保留或导致签名校验异常；Tauri 资源复制对符号链接的处理也不保证。一旦符号链接在最终产物里失效，`node` 启动失败 → Web sidecar 起不来。
- 缓解：mac smoke 会在挂载的 dmg 上执行 `node --version`，对**已构建产物**有一定兜底；但与 P2-3 同源，仍不覆盖签名/公证后的真实安装包。

### 证据索引
- `apps/desktop/packaging/scripts/build-mac.sh:116-152, 216, 225`
- `apps/desktop/packaging/scripts/build-win.ps1:28-41, 100-129`

---

## P3 低危 / 健壮性

- **P3-1　PyInstaller `hookspath` 指向不存在的目录**：`lumen-api.spec`/`lumen-worker.spec` 都设 `hookspath=[.../packaging/pyinstaller/hooks]`，但该目录**不存在**。PyInstaller 容忍（仅不加载自定义 hook），但若原本指望它打包 tiktoken 词表等数据，则未生效。离线时 tiktoken 会退化为近似 token 计数（有 fallback，非致命；smoke 会检测 `tiktoken_unavailable`）。
  - 证据：`apps/desktop/packaging/pyinstaller/lumen-api.spec:54`、`lumen-worker.spec:38`
- **P3-2　Windows keep-awake 线程亲和**：`power.rs` 在 Windows 用 `SetThreadExecutionState`（线程级），若从瞬态/线程池化的 Tauri 命令线程切换 keep-awake，状态可能随线程结束而丢失。
  - 证据：`apps/desktop/src/power.rs:165-180`
- **P3-3　诊断脱敏折叠行内空白**：`diagnostics.rs` 的 `redact_jwt_tokens` 按空白切分后用单空格 rejoin，会压缩行内空白（仅影响诊断可读性）。
  - 证据：`apps/desktop/src/diagnostics.rs`
- **P3-4　Docker 导入明文密钥中转残留**：provider key 经 `data/tmp/docker-import-provider-keys-*.json` 明文中转，成功后删除、**失败时残留**在磁盘。
  - 证据：`apps/desktop/src/docker_import.rs`

---

## 已排查并排除的"疑似回归"（验证后判定为非问题）

- **`f373d86 "initialize tokio runtime for desktop smoke"` —— 不是回归。**
  该 commit 把 `spawn_all_with_timeout`（`main.rs:703`）从 `tauri::async_runtime::block_on` 改为**自建一个多线程 tokio runtime 并 `block_on`**，且该函数被 4 处调用（含正常启动路径 `main.rs:1165` 与全量重启 `:669`，并非仅 smoke）。担心点有二，均已排除：
  1. **会不会"在 runtime 内再 block_on"导致 panic？** 不会——这 4 处调用全部位于 `std::thread::spawn` 的普通 OS 线程或同步主线程，非异步上下文。
  2. **runtime 在函数返回时被 drop，会不会杀掉后台任务？** 不会——`spawn_all`（`sidecar.rs:277`）内部**完全没有** `tokio::spawn`/`async_runtime::spawn`/`thread::spawn` 等分离任务，只是顺序 `.await` 各就绪探测；子进程是 `std::process::Child`（OS 级进程，独立于任何 tokio runtime）。私有 runtime 顺序驱动完 spawn 流程后被 drop，没有遗留任务可被中止。
  （唯一可忽略的副作用：每次全量重启会创建并销毁一个多线程 runtime，开销极小。）

---

## 修复优先级建议

1. **P0-1**：补 `capabilities/`（含 `remote.urls` 允许 `http://127.0.0.1:*`）——否则装好也用不了原生功能。
2. **P0-2 / P0-3**：在 CI 接通 mac 公证（`sign-mac.sh`→`notarize-mac.sh`）与 win Authenticode（`sign-win.ps1`）——否则下载后打不开。
3. **P1-1**：给卸载弹窗加 `/SD IDNO`，避免自动更新误删数据。
4. **P1-2**：切换 .NET 运行时下载源，恢复可重复构建。
5. **P1-3**：密钥不再明文落盘 / Windows 加 ACL。
6. **P2-3**：增加"签名+quarantine"冒烟，让 P0-2/P0-3 这类问题在 CI 可见。
7. 其余 P2/P3 按排期处理；**P2-1 需先用真实 Garnet 日志验证**再决定是否调整匹配串。

---

## 附录：关键证据文件索引

| 主题 | 文件 |
|------|------|
| 远程导航 / 启动 | `apps/desktop/src/main.rs`（`:1005-1009`、`:1185-1198`、`:632-715`、`:1120-1183`） |
| capabilities 现状 | `apps/desktop/tauri.conf.json`、`gen/schemas/capabilities.json`、`gen/schemas/acl-manifests.json` |
| Web 桥接 | `apps/web/src/lib/desktop/runtime.ts` |
| 密钥存储 | `apps/desktop/src/secrets.rs` |
| Sidecar / AOF 恢复 | `apps/desktop/src/sidecar.rs` |
| 打包（mac） | `apps/desktop/packaging/scripts/build-mac.sh`、`sign-mac.sh`、`notarize-mac.sh`、`lumen.entitlements` |
| 打包（win） | `apps/desktop/packaging/scripts/build-win.ps1`、`sign-win.ps1`、`apps/desktop/windows/hooks.nsh` |
| PyInstaller | `apps/desktop/packaging/pyinstaller/lumen-api.spec`、`lumen-worker.spec` |
| 冒烟测试 | `apps/desktop/packaging/scripts/smoke-mac.sh`、`smoke-win.ps1` |
| CI 发布 | `.github/workflows/desktop-release.yml` |
| 更新清单 | `apps/desktop/packaging/scripts/create-updater-manifest.py` |
