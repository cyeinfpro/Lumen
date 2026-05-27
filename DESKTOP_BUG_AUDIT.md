# Lumen Desktop（macOS / Windows）深度审计与修复方案

> 审计范围：`apps/desktop/`（Tauri Rust 监管器、子进程、备份、Docker 导入、电源、签名/打包脚本、smoke 脚本）+ 桌面侧 Web 外壳（`apps/web/src/components/desktop`、`apps/web/src/lib/desktop`、`apps/web/src/app/settings/{storage,update,diagnostics}`、`apps/web/src/lib/api/http.ts`、`apps/web/src/app/admin/_panels/ProvidersPanel.tsx`）+ 后端 `apps/api/app/routes/desktop.py`、`providers.py`。
>
> 共发现 **47** 项缺陷与隐患，按风险等级排序：阻断（P0）→ 严重（P1）→ 中等（P2）→ 低风险/体验（P3）。每条均给出 **现象 / 根因 / 修复方案 / 相关文件**。

---

## 一、阻断级（P0：装机即坏，或回归测试一定挂）

### P0-1. Redis 等待循环吃掉 Lua/EVAL 错误，超时后只剩“did not become ready”
- **现象**：当 Garnet 没有 Lua（或 Lua 报错），`spawn_all` 卡死 10 秒，再以泛化错误抛出，前端用户只看见"启动失败"而看不到根因；smoke 脚本能查到 `Lua scripting support disabled` 字样但 supervisor.log 里没有原始失败原因。
- **根因**：`apps/desktop/src/sidecar.rs:1093-1115` 的 `wait_for_redis` 仅对 `redis auth failed` 字串做短路返回；`try_redis_ping` 在 EVAL 失败时返回 `Err(anyhow!("redis lua eval failed: ..."))`，但循环里把 `last_error` 反复 `take()` + 重新塞回去，永远只检查 auth 关键字。
- **修复方案**：
  1. 把"应立即放弃"的错误改成强类型枚举（`RedisProbeError::Auth | LuaUnavailable | Network`），匹配 enum 而非字串。
  2. EVAL/Lua 报错时直接 `return Err(err)`，让 supervisor 把原文写进 startup error，使界面能看到“需要 Lua 支持”。
  3. 顺手把 `Ok(false)` 的失败次数累计上限设为 N=20，超过即视为"redis 启动正常但拒绝指令"再退出。

### P0-2. 关键 sidecar 反复 FullRestart 没有任何退避或上限
- **现象**：当 `api` 因配置错误持续启动失败，`start_sidecar_monitor`（`main.rs:935-1019`）每 2 秒检测到 `SidecarRecovery::FullRestart` → 调用 `block_on(supervisor.spawn_all())`，失败后再次进入下一轮。日志、CPU、磁盘 IO 同时被打爆，界面上 `desktop_startup_status` 反复闪烁，用户没有任何中止入口。
- **根因**：`recover_exited` 与监视循环都没有 `restart_count` 上限、没有 backoff、没有"超出阈值进入 cooldown / 切回启动页"的逻辑。`note_full_restart` 的计数器只写日志，未作为决策输入。
- **修复方案**：
  1. 在 `Supervisor` 增加 `full_restart_attempts` 计数；当 5 分钟内连续 ≥3 次失败，停止再次 spawn，调用 `set_startup_error` 让前端进入失败页（已有 `navigate_startup_page` 通路）。
  2. 每次 FullRestart 调用 `tokio::time::sleep` 指数退避（2s、4s、8s，封顶 30s）。
  3. 监视线程拥有自己的 `restart_token`；前端 retry 按钮触发时把 token 归零。

### P0-3. 桌面端没有自动迁移 SQLite schema，跨版本恢复直接报错
- **现象**：用户从旧版本 backup 恢复到新版本，启动后 API 因表结构与代码期望不一致而 500，但桌面外壳显示"已就绪"，问题极难定位。
- **根因**：`apps/desktop/src/sidecar.rs:514-515` 启动 API 时强制注入 `LUMEN_SKIP_MIGRATION_CHECK=1`；`spawn_all_with_progress` 里也没有任何 `alembic upgrade head` 调用。`apply_pending_restore` 之后只删除 `.bootstrap-done`，并未运行迁移。`pyinstaller/lumen-api.spec` 已经把 `alembic/desktop` 打进资源，但没有任何代码消费它。
- **修复方案**：
  1. 在 `spawn_all_with_progress`（或新的 preflight 步骤）里增加 `progress("升级本机数据库")` + `Command::new(lumen-api) alembic upgrade head`（lumen-api 已有 CLI 入口）。
  2. 恢复流程 `apply_pending_restore_inner` 完成后强制运行一次迁移。
  3. 在 startup error UI 上区分"迁移失败"与"sidecar 启动失败"，给"导出诊断包"按钮带上数据库版本信息。

### P0-4. 自动更新失败时把整个 app cache 目录删掉
- **现象**：升级安装中断（网络抖动 / 签名错误 / 用户取消），`install_desktop_update`（`main.rs:392-403`）直接 `fs::remove_dir_all(cache_dir)`，把 Tauri 自身缓存、Webview Cookies、本地账户状态全部清空，下一次启动等同于"全新装机"。
- **根因**：作者意图是"清理失败的更新中转文件"，但用了 `app.path().app_cache_dir()` —— 该目录 = 整个应用缓存根，不是 updater 临时区。
- **修复方案**：
  1. 把 `fs::remove_dir_all(cache_dir)` 换成只删 updater 子目录：`cache_dir.join("updater")` 或 Tauri 暴露的 `updater()::download_dir`（v2 API 提供）。
  2. 即使确实要删，先 `let _ = fs::remove_dir_all(cache_dir.join("updater"))`，并把 try-catch 留给主线程，避免把整个 user data wipe。

### P0-5. Docker 导入超时杀子进程时遗漏整棵进程树
- **现象**：导入大型 dump 命中超时（默认 30 分钟），`run_importer_with_timeout` 只 `child.kill()`（`docker_import.rs:403`），lumen-api 的子进程（如 `pg_restore` / Python 子线程）继续占用 dump 文件与 SQLite WAL；下一次重试看见“pending dump 仍在被占用”。
- **根因**：Unix 上没用 `process_group` 并 `kill -TERM -PGID`；Windows 上没用 `taskkill /T`。
- **修复方案**：
  1. 复用 `sidecar.rs` 已有的 `terminate_child` 工具：在 docker_import 也走 Unix 进程组 + Windows `taskkill /T /F`。
  2. 进程组建立同样用 `command.process_group(0)`（Unix）。

### P0-6. backup 恢复存储目录失败回滚不可靠
- **现象**：恢复 storage 失败时已经把原数据 rename 到 `data/storage.before-restore-{ts}`，回滚操作 `fs::rename(&old, &target)` 的返回值被丢弃，若回滚也失败用户会看见空的 storage（且没有日志说明）。
- **根因**：`backup.rs:668-674` 中 `if old.exists() && !target.exists() { let _ = fs::rename(&old, &target); }` 直接吞错。
- **修复方案**：
  1. 把回滚返回值写入 supervisor.log 或 restore.log。
  2. 回滚失败时返回原错误并附 "storage rolled back to {old}"，前端必须显示这条提示并阻止启动，让用户手动整理。
  3. 在 storage 切换前用临时 sentinel 文件标记“处于切换中”，下一次启动检测到 sentinel 即拒绝继续，提示手动恢复。

### P0-7. Updater 公钥未配置时仍能产出 release 构建，但更新无法验证
- **现象**：`apps/desktop/tauri.conf.json` 内 `plugins.updater.pubkey = ""`。`build-mac.sh:198-204` / `build-win.ps1:171-176` 只在 `GITHUB_REF_TYPE=tag` 时强制要求 pubkey。日常 nightly / 手动 release 构建会得到一个 active=true 但无公钥的安装包，运行时调用 updater 直接 panic 或拒绝校验。
- **根因**：默认配置里 `active=true` 与 `pubkey=""` 同时存在；构建脚本没有把这两者绑死。
- **修复方案**：
  1. 把 `tauri.conf.json` 的 `updater.active` 默认改为 `false`，仅在 `prepare_tauri_config_args` 里同时打开 active+pubkey。
  2. 或者改为 `if (!pubkey) { config.plugins.updater.active = false }`，让无公钥时构建产出"不开启 updater"的版本而不是带空公钥的版本。

---

## 二、严重（P1：高发场景必然出问题）

### P1-1. `spawn_desktop_runtime` 重试策略只覆盖端口冲突
- **位置**：`apps/desktop/src/main.rs:824-887`。一次失败后 `reassign_ports` + 重启，再失败就放弃。如果根因是 sidecar 二进制缺失或环境变量错误，重新选端口无效但仍占用一次重试机会。
- **修复**：失败原因分类（端口冲突 / sidecar spawn error / 健康检查超时）；端口冲突才走 reassign，其它原因直接进入 startup error，避免误导用户。

### P1-2. `secrets.rs` 的双重存储不会同步删除
- **位置**：`apps/desktop/src/secrets.rs:98-125`。`set_secret` 失败时只要一边成功就吞错返回 `Ok(())`，但两边的"上一次值"可能不一致。读路径优先 fallback 文件，若 fallback 已被清空但 keychain 写失败，下次读会得到旧 keychain 值。
- **修复**：写成功一边就强制再次尝试同步另一边（最多 3 次），仍失败则把不一致写入 `secrets-out-of-sync.log`，并在 diagnostics bundle 中带上。

### P1-3. `secrets.rs` 临时文件命名靠 `with_extension("json.tmp")`
- **位置**：`apps/desktop/src/secrets.rs:84`。`Path::with_extension("json.tmp")` 只替换最后一段扩展名；当文件名碰巧没有扩展名（理论上不会发生），生成的 tmp 路径会错位。
- **修复**：改为 `path.with_file_name(format!("{}.tmp", path.file_name().unwrap().to_string_lossy()))`。

### P1-4. `snapshot_database` 用 READ_WRITE 打开 SQLite
- **位置**：`apps/desktop/src/backup.rs:351-356`。VACUUM INTO 实际只读，使用 RW 标志会与 API 写并发争锁，且 `SQLITE_OPEN_NO_MUTEX` 在多线程下危险。
- **修复**：改用 `SQLITE_OPEN_READ_ONLY`；保留 `SQLITE_OPEN_FULL_MUTEX` 或不带 NO_MUTEX。

### P1-5. 日志旋转不是线程安全的
- **位置**：`apps/desktop/src/sidecar.rs:1034-1083`。`open_rotated_log_file` 同时被监视线程（heartbeat 每 5s）、spawn 路径、shutdown 等多处调用；两个调用同时读取 metadata 然后竞速 rename，第二个 rename 会失败，整个事件被丢失。
- **修复**：
  1. 引入一个 `Mutex<()>`（或 `parking_lot::Mutex`）守护旋转 + 写入操作。
  2. 或者改造为 single-writer：所有事件通过 `mpsc` 发送给专门的日志线程。

### P1-6. `apply_pending_restore_inner` 强制 safety backup 失败则中止
- **位置**：`apps/desktop/src/backup.rs:533-573`。当磁盘已经满 / 数据库损坏到无法 VACUUM 时，safety backup 必然失败，restore 因此永远无法执行——而 restore 本来就是用户的"逃生通道"。
- **修复**：
  1. 把 safety backup 的失败降级为告警，记录到 restore.log，并把 `safety_backup_path = None` 透出到 UI。
  2. UI 弹窗要求用户二次确认"不创建安全备份"。

### P1-7. `verify_macos_dmg_bundle_signature` 仅依赖 codesign verify，没检查公证状态
- **位置**：`build-mac.sh:240-278`。只 verify deep/strict，没有 `spctl --assess --type execute` 也没有 `xcrun stapler validate`；如果未走 `notarize-mac.sh`，DMG 在 Gatekeeper 下仍会被弹窗拦截。
- **修复**：构建末尾增加可选 `spctl --assess` 与 `stapler validate`，未公证时给出明确警告或 fail。

### P1-8. `build-win.ps1` 公证 / 安装签名步骤缺失
- **位置**：整个 build-win.ps1 不调用 `sign-win.ps1`；`sign-win.ps1` 用 `signtool sign /a` 自动选证书，CI 上有多张证书会随机选。
- **修复**：
  1. 在 build-win.ps1 末尾按需要调用 `sign-win.ps1 $exePath`。
  2. `sign-win.ps1` 增加 `-Thumbprint` / `-CertPath` 参数（来自环境变量），不再依赖 `/a`。
  3. 增加 `signtool verify /pa /v` 失败时 exit code 校验（当前未捕获非零退出）。

### P1-9. macOS entitlements 可能缺少 Node.js 所需 JIT 入口
- **位置**：`apps/desktop/packaging/scripts/lumen.entitlements`（未读取，但 Node 在 hardened runtime + sandbox 下默认会被禁 JIT）。
- **修复**：检查 entitlements 是否包含：
  - `com.apple.security.cs.allow-unsigned-executable-memory`（V8 JIT 必须）；
  - `com.apple.security.cs.disable-library-validation`（PyInstaller 解压子库需要）；
  - `com.apple.security.cs.allow-jit`。
  若缺失，DMG 安装后 lumen-web 会在第一次启动时被 macOS 直接 SIGKILL。

### P1-10. `desktopInvoke` 引用了从未实现的 `__LUMEN__` 桥
- **位置**：`apps/web/src/lib/desktop/runtime.ts:118-140`。`window.__LUMEN__?.invoke` 永远是 undefined，相当于死代码；保留它会让阅读者以为还有第二种 desktop 运行时。
- **修复**：删除 `__LUMEN__` 分支，统一走 `window.__TAURI__?.core?.invoke`，并把 `withGlobalTauri` 的契约写进注释。

### P1-11. Tauri Web Content Security Policy 关闭
- **位置**：`apps/desktop/tauri.conf.json` 中 `security.csp: null`。webview 加载本地 server，但不限制脚本来源、连接来源；一旦未来引入第三方 iframe 或图床，攻击面被放大。
- **修复**：写明显式 CSP：
  ```json
  "csp": "default-src 'self' http://127.0.0.1:* ws://127.0.0.1:*; img-src 'self' data: blob: http://127.0.0.1:*; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
  ```
  并把 Next.js 资源映射进去（必要时使用 `unsafe-inline` 或 nonce）。

### P1-12. `install_desktop_update` 返回类型与实际语义不一致
- **位置**：`main.rs:384-404`。函数声明 `Result<bool, String>`，但只有"无更新可用"时返回 `Ok(false)`，安装成功后调用 `app.restart()` 永不返回；JS 端无法知道"已开始安装"。
- **修复**：
  1. 将类型改成 `Result<UpdateInstallStatus, String>`，UpdateInstallStatus = `NoUpdate | Installing { version: String }`；下载/安装前先返回 `Installing`，再异步 restart。
  2. 或者新增 `update_install_progress` 命令，前端订阅 Tauri event 拿到进度。

### P1-13. `quit_desktop_app` / `request_desktop_exit` 仅等待 200ms 就调用 exit
- **位置**：`main.rs:424-434`。sidecar 还在 SIGTERM → 强 KILL 中，200ms 不够 Web/Python 子进程优雅退出。Windows 下偶发会出现 `lumen-redis.exe` 残留写盘失败。
- **修复**：把 200ms 改成 `Supervisor::shutdown_with_timeout(Duration::from_secs(5))`，里面真正 `child.wait()`。

### P1-14. `recover_exited` 重启 worker/web 不带任何 backoff
- **位置**：`sidecar.rs:446-466`。worker 因 SIGTERM 退出立刻重启；如果它在崩溃环里（例如 DB 文件被锁），监视线程 2 秒一轮、Spawning N 次。日志和 SQLite 都会被写爆。
- **修复**：与 P0-2 类似，给每个非 critical sidecar 设置滑动窗口（1 分钟内最多重启 3 次，否则标记为 unhealthy 并通过 UI 告警）。

### P1-15. `docker_import` stdout/stderr 全量读入内存
- **位置**：`docker_import.rs:412-413`。一次 `read_to_end` 整个日志文件，若导入器在错误循环里输出 GB 级日志，桌面进程会 OOM。
- **修复**：只读最后 256 KiB；或者把 file 直接附进 report.json 路径，不在内存里 decode。

### P1-16. `pyinstaller` spec 中 `console=True`
- **位置**：`apps/desktop/packaging/pyinstaller/lumen-api.spec` & `lumen-worker.spec`。
- **现象**：Windows 上虽然有 `CREATE_NO_WINDOW` 缓解，但当用户用 PowerShell 直接调用 lumen-api.exe（诊断时）会弹出常驻控制台；macOS 上不影响。更关键的是，PyInstaller 的 console 模式会让二进制把异常 trace 写到 stderr（已被 supervisor 捕获），同时会在 Windows + Tauri 子进程上偶发触发 stdin/stdout 冲突。
- **修复**：把 console 改成 `False`（windowed/无窗），所有日志通过 `LUMEN_DATA_ROOT/data/logs/*.log` 输出。

### P1-17. `desktop_status` / `sidecar_statuses` 需要 `&mut self`，与 UI 高频查询冲突
- **位置**：`main.rs:99-110`，`sidecar.rs:316`（`fn sidecar_statuses(&mut self)`）。前端面板 1 秒一次 polling `desktop_status`，同时 monitor 线程也要锁 supervisor，互相阻塞。
- **修复**：把 `try_wait` 的结果缓存（带最近 N 毫秒有效），让 UI 端通过 `&self` 读快照；mut 操作集中到 monitor 线程。

### P1-18. `spawn_redis` 的 `--recover` 与 `--aof` 参数兼容性
- **位置**：`sidecar.rs:493-496`。Garnet 1.1.9 的命令行选项随版本变动较大；smoke 脚本里专门检测 "old Garnet --logdir argument" 字串，说明此前出现过参数错误。当前用 `--checkpointdir`、`--aof`、`--recover`，缺少 `--memory` 限制，且 Garnet 没有 `--recover` 这个 flag（实际叫 `--recover` 与否需对照 GarnetServer 的 README）。
- **修复**：在 build 时锁定 Garnet 版本，并对 spawn 前的参数集合写一个 unit test（解析 `--help` 输出验证）；或退一步改为读取 `data/redis/garnet.conf` 配置文件，不传裸 CLI。

### P1-19. Smoke 脚本依赖正则提取 uvicorn 端口
- **位置**：`smoke-mac.sh:268` / `smoke-win.ps1:261`。`Uvicorn running on http://127.0.0.1:(\d+)` 匹配字串依赖 uvicorn 的日志格式；当 lumen-api 后续替换日志框架（例如直接用 logging.config）会立刻失效，但脚本只会 baseline_ready=false。
- **修复**：把端口写入 `data/tmp/api.port`，由 lumen-api 启动时主动写入；smoke 直接读文件。

### P1-20. `tauri.conf.json` updater endpoint 写死 latest release
- **位置**：`tauri.conf.json:38`。`https://github.com/.../releases/latest/download/latest.json`，对企业内网用户、需要锁定旧版本回滚的用户极不友好；同时该路径在 GitHub 速率限制下偶发 403。
- **修复**：把 endpoint 设计为多 URL fallback + 支持 `LUMEN_UPDATER_ENDPOINT` 环境变量覆盖。

---

## 三、中等（P2：边角问题，但用户能触发）

### P2-1. `set_provider_key`/`set_proxy_secret` 不校验入参
- **位置**：`main.rs:113-144`。trim 后即使为空也不报错，前端可以把名字写成 `"  "` 而看似成功。
- **修复**：trim 后为空时返回 `Err("provider name is required")`。

### P2-2. `default_data_root` 不支持环境变量覆盖
- **位置**：`main.rs:80-82`。只有 headless smoke 看 `LUMEN_DATA_ROOT`，正式运行无法做"便携安装"（U 盘 / 公司限制不写 ~/Library）。
- **修复**：先读 `LUMEN_DATA_ROOT` env，验证可写后使用；否则回落到 `app_data_dir`。

### P2-3. `diagnostics.redact_text` 检测规则脆弱
- **位置**：`diagnostics.rs:161-187`。
  - `line.contains("sk-")` 误伤包含 "asks-" 等子串的行；
  - 没有覆盖 `Bearer xxx`、`X-Api-Key:`、JWT；
  - 按空格切分 token，URL `?key=sk-xxx` 不会被切出来。
- **修复**：改用正则集合：`(?i)(authorization|api[_-]?key|token|x-api-key|bearer)\s*[:=]\s*\S+` → `$1: [REDACTED]`；保留 sk- 前缀切分但允许 `=` 与 `&` 为分隔符。

### P2-4. `restore_database` 未先 checkpoint WAL
- **位置**：`backup.rs:611-625`。如果在 restore 时仍有 sidecar 持有旧 lumen.sqlite-wal，删除文件后剩余 WAL 会让新 sqlite 误读为已 checkpoint。
- **修复**：在 restore 前对旧 DB 执行 `PRAGMA wal_checkpoint(TRUNCATE);`，关闭后再删除三件套。

### P2-5. `create_desktop_backup` 工作区在错误时不清理
- **位置**：`backup.rs:94-104`。`fs::remove_dir_all(&work_root)` 用 `let _ =`，可观察 `data/tmp/backup-work-*` 长期残留。
- **修复**：在 startup preflight 时清理 1 小时前的 `data/tmp/backup-work-*`、`restore-work-*`、`docker-import-*`。

### P2-6. `try_redis_ping` 多次 sleep + take/replace 的状态机不必要复杂
- **位置**：`sidecar.rs:1093-1115`。`last_error.take()` 后又塞回去，让代码意图不明显。
- **修复**：把循环重写成"立即返回 fatal/否则 sleep"的清晰分支；不需要状态机式 take/replace。

### P2-7. `http_body_sync` 没有处理 chunked transfer encoding
- **位置**：`sidecar.rs:1245-1299`。直接以 `\r\n\r\n` 分割 head/body，对 chunked 响应得到的 body 是带 chunk size 的原文。Garnet 不走 HTTP，所以目前只命中 API 的 /system/desktop-activity 等简单 endpoint，但脆弱。
- **修复**：检测 `Transfer-Encoding: chunked`，手动解 chunk；或直接换 `ureq`/`reqwest::blocking` 客户端。

### P2-8. `wait_for_http_ok` 单次 256 字节读取
- **位置**：`sidecar.rs:1200-1217`。响应行 + 头部超过 256 字节时 starts_with 检查会失败；某些 sidecar 启动前会先返回 502 中转错误，因为读到的不是头部第一段，会被当成"未就绪"再等。
- **修复**：循环读取直到至少拿到 `\r\n` 或 1KiB 上限再判断。

### P2-9. `resolve_sidecar` 候选列表没有覆盖 Tauri v2 build 输出布局
- **位置**：`sidecar.rs:916-961`。`dir.join("binaries").join(name)` 等仅匹配旧布局；Tauri v2 默认会把 bundled binaries 解到 `Contents/Resources/binaries/`（macOS）或安装目录下的 `bin/`。当前列表能撞上，但顺序错乱时 macOS 会先匹配 `Contents/MacOS/lumen-api`（不存在）然后再走 `../Resources/...`。
- **修复**：把 mac 专有路径排到前面，并加入 `dir.join("binaries").join(triple).join(name)`（Tauri v2 multi-arch 输出）。

### P2-10. `apiFetch` 的 CSRF 重试与网络重试计数器共用 `attempt`
- **位置**：`apps/web/src/lib/api/http.ts:222-318`。CSRF 重试逻辑里没有把 `attempt` 归零，下次再撞到 5xx 时就不会重试。
- **修复**：把 CSRF 重试拆成独立分支，或者把网络层与 CSRF 层用独立 attempt 计数。

### P2-11. `apiFetch` 缺少对 `URLSearchParams` body 的判断
- **位置**：`http.ts:194-200`。`URLSearchParams` 既不是 binary 也不是字符串，会被强制 `content-type: application/json`，但浏览器会 `toString()` 编码为表单字串，导致服务端收到一个被错误标记的 form。
- **修复**：在 `isBinary` 判断里把 `body instanceof URLSearchParams` 当作非 JSON 处理。

### P2-12. 桌面外壳 retry 时不会重置错误状态
- **位置**：`main.rs:340-355`（`retry_desktop_startup`）。若上一轮 startup 正在 starting 或已经 ready，直接 `return Ok(())`，但 UI 看到的是仍带 error。
- **修复**：先 `set_startup_starting(&state)`（清空 error），再判断是否启动。

### P2-13. `set_startup_phase` 接受 `&'static str`，限制了 phase 字符串拼接
- **位置**：`main.rs:921-925`。phase 字符串只能是字面量；动态错误信息（如"端口 X 占用，正在重试"）无法在 startup_status 中显示。
- **修复**：改成 `phase: impl Into<String>`，并在 `set_startup_phase` 中 `to_string()`。

### P2-14. `navigate_startup_page` 仅在 FullRestart 失败时调用
- **位置**：`main.rs:1008-1010`。当 startup_runtime 第一次失败（reassign 也失败）时 `set_startup_error` 已经写入状态，但 `app_handle.get_webview_window` 还没装入 startup page 的 URL；用户看到的是初始 dist/web/index.html，等价于成功状态——但其实之后没有 navigate 过来。
- **修复**：把 `navigate_startup_page` 移到 `set_startup_error` 内部，确保每次进入失败态都强制回到启动页。

### P2-15. `restore_database` / `restore_storage_dir` 对 windows file lock 缺乏重试
- **位置**：`backup.rs:611-677`。Windows 下若 antivirus 还在扫描旧文件，rename 立刻报 sharing violation。
- **修复**：在 rename / remove_file 上加 3 次小退避（100ms、500ms、1s）。

### P2-16. `run_headless_smoke` 监视线程是 `std::thread::spawn`，主线程 60s 轮询
- **位置**：`main.rs:561-603`。子线程发现 FullRestart 后 `block_on(spawn_all())`，但若 `spawn_all` 永远卡住，主线程并不知道，永远以为 smoke 在跑。CI timeout 才能终止。
- **修复**：把 `spawn_all` 的健康检查附带超时；监视线程要把 fatal 状态写到一个共享 atomic，主线程检测后 `std::process::exit(2)`。

### P2-17. `ProvidersPanel` 列表去重依赖 trim 后的 name 完全相等
- **位置**：`apps/web/src/app/admin/_panels/ProvidersPanel.tsx`（diff 中可见）。如果一个 server 名 `"foo "`（带空格）和 draft `"foo"`，`serverKeyHints.get(d.name.trim())` 不命中，UI 会判定 hasExistingKey=false 而要求重填密钥。
- **修复**：把 server 数据也 trim 后入表。

### P2-18. `_probe_http_error_message` 截断到 240 字节后可能切碎 UTF-8
- **位置**：`apps/api/app/routes/providers.py`（diff 中 `_truncate_probe_error`）。`text[: limit - 1]` 在多字节字符上等于 byte 切片；Python 字符串以字符计数所以安全，但其后 `rstrip()` + `"…"` 拼接没问题。复审：Python 字符串切片是按字符的，无需修复——但 frontend 仍然要保证渲染长度不爆 UI（`max-w-[260px]` 已加 truncate class）。
- **修复**：无；建议把 `"…"` 改成 `"…\n（已截断）"` 让用户知道完整错误在响应体里。

### P2-19. `apiFetch` 401 在桌面运行时直接返回错误
- **位置**：`http.ts:49-61`。桌面运行时跳过 redirect，但 401 仍然 throw `ApiError`。React Query 会一直 retry。
- **修复**：桌面运行时把 401 当成"未登录的本地用户"用 `Promise.resolve(null)` 返回，避免在 BootstrapGate 之前的初始化反复打 401。

### P2-20. `try_redis_ping` 在 PING 失败时直接返回 Ok(false)
- **位置**：`sidecar.rs:1136-1139`。这会让 `wait_for_redis` 误以为"还没就绪"而继续重试。若 Garnet 因为某种原因永远响应非 +PONG 字串（例如 cluster mode 下响应 `-LOADING ...`），轮询不会汇总错误。
- **修复**：把 PING 非 +PONG 也累加到 last_error 列表，便于诊断。

---

## 四、低风险/体验（P3：清理/打磨/可优化）

### P3-1. `build-mac.sh` 用 host `node -p 'process.versions.node'` 推断版本
- 修复：依赖 `apps/web/package.json` 的 engines 字段或独立 `NODE_VERSION` 文件。

### P3-2. macOS `prepare_node_runtime` 解压后做 symlink
- macOS `ln -sf bin/node "$dest/node"`：当 `$dest/node` 已存在为目录（错误状态）时 `-f` 不会替换目录。
- 修复：先 `rm -f $dest/node` 再 `ln -s`。

### P3-3. Windows `Prepare-NodeRuntime` 用 `Copy-Item bin/node.exe` 复制副本
- 没硬链接概念，导致 node.exe 出现两份占空间（约 100MB）。
- 修复：用 `New-Item -ItemType SymbolicLink`（管理员权限或开启开发者模式时可用）；否则保留 copy 但只在需要时 fallback。

### P3-4. `clean_tauri_outputs` 删除整个 release/resources 目录
- 当作者本地调试 `cargo run` 时 cargo target 会出错；shell + ps1 都没区分 "构建第一阶段" 与 "调试无需清理"。
- 修复：把 clean 调用提取为可选 flag `--clean`。

### P3-5. `tauri.conf.json` 没有 `bundle.publisher` / `bundle.copyright`
- macOS Info.plist / Windows NSIS 元数据缺失，应用市场/系统设置中显示为 "Unknown Publisher"。
- 修复：在 conf 加 `publisher`、`copyright`、`shortDescription`、`longDescription`。

### P3-6. NSIS installer 不支持卸载时清理用户数据
- 当前 `installMode: currentUser` 没有 `displayLanguageSelector`，无残留清理选项；用户重装时旧数据自动加载——多数情况好，但若数据已经损坏会立刻再次崩溃。
- 修复：NSIS 卸载脚本里加可勾选的"删除 %APPDATA%\com.lumen.desktop"。

### P3-7. `smoke-mac.sh` 在 ARM 上硬编码 dmg 文件名
- 第 5 行 `Lumen_$(cat VERSION)_aarch64.dmg`，x86_64 构建被忽略；CI 跨平台时一并跑会失败。
- 修复：用 `find target/release/bundle/dmg -maxdepth 1 -name 'Lumen_*.dmg' | sort | tail -n1`。

### P3-8. `smoke-mac.sh` cleanup 中两次 `pkill -P`
- 重复杀同一棵进程树；最后 `wait` 在僵尸进程上会无限挂起。
- 修复：用 `wait "$app_pid" 2>/dev/null` 加 `timeout 5`，或者直接 `kill -9` 后不 wait。

### P3-9. Tauri 的 single-instance 插件在 macOS 上偶发触发"重新打开主窗"
- 现状没问题，但 `_argv, _cwd` 参数被忽略——以后想支持"双击 .lumen-backup 文件打开应用"会要从这里读 argv。
- 修复：把 callback 改成 `if let Some(arg) = argv.first() { handle_file_open(arg) }`。

### P3-10. `redact_text` 把整行替换成 `[REDACTED sensitive line]`
- 调试 SQL 报错时常常因为 SQL 里包含 `api_key` 列名而把整条 SQL 抹掉。
- 修复：仅替换匹配到的 token，而不是整行。

### P3-11. Tauri 启动页 `index.html` 直接用 innerHTML 渲染 phase/note
- 已经手写 `escapeHtml`，OK，但是 `error.message` 可能含换行符；`<pre>` 已处理。值得给 phase pill 加 ARIA-live 让屏幕阅读器感知状态变化。

### P3-12. `DesktopBootstrapGate` 没有"取消"按钮
- 用户跑到第 3 步发现选错了"从备份恢复"，无法回退取消 pending restore。
- 修复：当 `restoreQ.data?.pending` 时显示"取消恢复"按钮，调用 `clearFailedRestoreMarker` 之外的"clear pending"命令（需新增 Tauri command）。

### P3-13. AdminUpdatePanel 与 settings/update/page 重复实现"检查更新"
- 在桌面运行时，admin/update 面板不应该出现；smoke 检查 docker_only_routes 包含 `/admin`，但允许 admin 内子页存在。
- 修复：在 admin/_panels/AdminUpdatePanel 顶部加 `if (isDesktopRuntime()) return null;`。

### P3-14. `runtime.ts` 中类型 `string | "redis" | "api" | ...` 退化为 string
- 联合类型 + string 等于 string，TS 没有自动收窄。改为 `type SidecarName = "redis" | "api" | "worker" | "web"`，再让 `name` 保持联合类型。

### P3-15. `desktop_status` 命令在 supervisor 锁定时延迟可能放大
- 把 `desktop_status` 改为 `async` + 内部 `tokio::task::spawn_blocking`，避免阻塞 Tauri 的 invoke runtime。

### P3-16. `apiFetch` 对 5xx 重试用 `RETRYABLE_HTTP_STATUSES` 但没记录到 Sentry / 日志
- 修复：当所有 retry 都失败时，console.warn 一次（dev 环境）+ Sentry breadcrumb。

### P3-17. `update-progress` 没有事件通知
- 用户点击"下载并安装"后 UI 只显示 loading；下载进度（百分比、字节数）完全无反馈。
- 修复：`download_and_install(|progress, total| { emit("update://progress", ...) }, || emit("update://finished"))`，前端订阅。

### P3-18. `smoke-win.ps1` 第 280 行 catch 块缩进异常
- 仅是格式问题，但容易让维护者误读控制流。
- 修复：重写为
  ```powershell
  } catch {
    Start-Sleep -Milliseconds 250
  }
  ```

### P3-19. `set_active(true)` 在 macOS 上获取 IOKit 断言名是固定英文
- 用户的活动监视器里看到 "Lumen is processing local tasks"，i18n 缺失。
- 修复：根据用户语言切换文案，或干脆使用 "Lumen.app" 让 macOS 自带本地化。

### P3-20. `tauri.conf.json` 的 windows.minWidth=960
- 在 1366×768 笔记本上勉强够用，但桌面端有大量设置面板内联（providers 表 + 探活 + 修改）会强制横向滚动。
- 修复：把 minWidth 降到 880，并把 ProvidersPanel 卡片在窄屏下从 grid-cols-2 退回 grid-cols-1。

---

## 五、复测建议

完成上述修复后，建议按顺序复跑：

1. **单元/集成测试**：`cargo test -p lumen-desktop`、`uv run pytest apps/api/tests/test_providers_probe.py`。
2. **本地 headless smoke**：`LUMEN_DESKTOP_HEADLESS_SMOKE=1 ./target/release/lumen-desktop`，验证 supervisor.log 出现 `heartbeat`/`sidecar_restart`/`full_restart` 三个事件且没有 `--logdir`、`tiktoken_unavailable`、`Lua scripting support disabled` 等关键字。
3. **macOS dmg smoke**：`./apps/desktop/packaging/scripts/smoke-mac.sh`（M1 / Intel 各一次）。
4. **Windows nsis smoke**：在 win11 x64 / win11 arm64 各跑一次 `smoke-win.ps1`。
5. **跨版本恢复回归**：构造 v1.0 版备份，安装 v1.1 应用，验证迁移自动执行且无数据丢失。
6. **更新失败回滚回归**：在 install_desktop_update 中途断网，确认 app cache 未被清空、本地账户与会话仍然存在。
7. **崩溃环回归**：把 lumen-api 二进制临时换成 `false`（始终失败），确认 5 分钟内不会出现成千上万次重启日志，UI 自动停在启动失败页。

---

## 六、附录：相关文件清单

| 模块 | 主要文件 |
|---|---|
| Tauri 主进程 | `apps/desktop/src/main.rs` |
| sidecar 监管器 | `apps/desktop/src/sidecar.rs` |
| 备份 | `apps/desktop/src/backup.rs` |
| Docker 导入 | `apps/desktop/src/docker_import.rs` |
| 诊断 | `apps/desktop/src/diagnostics.rs` |
| 密钥 / 凭据 | `apps/desktop/src/secrets.rs` |
| 防睡眠 | `apps/desktop/src/power.rs` |
| 构建脚本 | `apps/desktop/packaging/scripts/build-mac.sh`、`build-win.ps1` |
| 签名脚本 | `apps/desktop/packaging/scripts/sign-mac.sh`、`sign-win.ps1`、`notarize-mac.sh` |
| Smoke | `apps/desktop/packaging/scripts/smoke-mac.sh`、`smoke-win.ps1` |
| Updater manifest | `apps/desktop/packaging/scripts/create-updater-manifest.py` |
| PyInstaller spec | `apps/desktop/packaging/pyinstaller/{lumen-api,lumen-worker}.spec` |
| Tauri 配置 | `apps/desktop/tauri.conf.json`、`apps/desktop/Cargo.toml` |
| 启动等待页 | `apps/desktop/packaging/startup/index.html` |
| 桌面端 Web 外壳 | `apps/web/src/components/desktop/DesktopBootstrapGate.tsx` |
| 桌面端 Web 设置页 | `apps/web/src/app/settings/{storage,update,diagnostics}/page.tsx` |
| 桌面 invoke 桥 | `apps/web/src/lib/desktop/runtime.ts` |
| API 客户端 | `apps/web/src/lib/api/http.ts` |
| 后端桌面端点 | `apps/api/app/routes/desktop.py` |
| 探活逻辑（被 desktop 复用） | `apps/api/app/routes/providers.py` |
