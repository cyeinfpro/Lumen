# Lumen 版本号管理

Lumen 产品版本的唯一源是仓库根目录 `VERSION`，格式为不带 `v` 前缀的 SemVer：

```text
1.0.0
1.2.3
1.3.0-rc.1
```

## 同步版本

修改 `VERSION` 后执行：

```bash
python3 scripts/version.py sync
python3 scripts/version.py check
uv lock
```

`sync` 会把版本同步到：

- `pyproject.toml`
- `apps/api/pyproject.toml`
- `apps/worker/pyproject.toml`
- `apps/tgbot/pyproject.toml`
- `packages/core/pyproject.toml`
- `packages/core/lumen_core/__init__.py`
- `apps/web/package.json`
- `apps/web/package-lock.json`

## 发布 Tag

正式发布 tag 必须和 `VERSION` 一致：

```bash
VERSION=1.2.3
git tag v1.2.3
git push origin v1.2.3
```

CI 会检查：

```bash
python3 scripts/version.py check
python3 scripts/version.py assert-tag "$GITHUB_REF_NAME"
```

## Docker Tag 规则

正式发布时使用：

```bash
python3 scripts/version.py docker-tags
```

输出示例：

```text
v1.2.3
v1.2
v1
latest
```

预发布版本（例如 `1.3.0-rc.1`）只输出精确 tag：

```text
v1.3.0-rc.1
```

预发布版本不能更新 `latest`、`v1`、`v1.3` 这类稳定指针。

每次 push 到 `main` 的预构建镜像应额外使用：

```text
sha-<short-sha>
main
```

`latest` 只能由正式版本 tag 更新，不能由普通 main push 覆盖。
