#!/usr/bin/env bash
# 跑全套后端测试。
#
# 为什么不用一条 `pytest apps/worker/tests apps/api/tests packages/core/tests`：
# apps/api 与 apps/worker 各有一个名为 `app` 的顶层 package；同进程合跑时
# Python module cache、Prometheus 默认 registry、PIL.Image.MAX_IMAGE_PIXELS
# 等全局状态会跨 app 污染，导致 30+ 测试在合跑下假阴性。
#
# 标准做法是按 app 分子进程跑（CI 常拆 job）。本脚本统一这一惯例：
# 任一子集失败立即退出。

set -euo pipefail

cd "$(dirname "$0")/.."

: "${STORAGE_ROOT:=/tmp/lumen-test-storage}"
export STORAGE_ROOT

echo "==> apps/worker/tests"
uv run pytest apps/worker/tests "$@"

echo
echo "==> apps/api/tests"
uv run pytest apps/api/tests "$@"

echo
echo "==> packages/core/tests"
uv run pytest packages/core/tests "$@"

echo
echo "==> all suites passed"
