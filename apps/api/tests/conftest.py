"""
合并跑 apps/api/tests 与 apps/worker/tests 时，两边各有一个名叫 `app` 的顶层
package。pytest 同 session 收集两套测试，sys.modules 里 `app.*` 会按收集/运行顺序
被先到的版本占据，导致后到方拿不到自己的子模块。

本 conftest 在以下三个时机强制把 apps/api 提到 sys.path 最前并清空异源 app.* 缓存，
让 collection / setup / 运行期 import 都拿到 apps/api/app：
  - module 加载时（处理 pytest 启动期 sys.path 顺序）
  - pytest_collectstart（处理 collection 阶段的 import）
  - pytest_runtest_setup（处理 test function 体内的 inline import）
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from types import ModuleType

API_ROOT = str(Path(__file__).resolve().parents[1])
TESTS_DIR = str(Path(__file__).resolve().parent)
APP_ORIGIN = "api"
APP_MARKER = "/apps/api/"
_APP_CACHE_ATTR = "_lumen_test_app_module_caches"

os.environ.setdefault(
    "BYOK_API_KEY_MASTER_SECRET",
    "test-byok-master-secret-0123456789-test",
)
os.environ.setdefault("APP_ENV", "test")


def _app_module_cache() -> dict[str, dict[str, ModuleType]]:
    cache = getattr(sys, _APP_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(sys, _APP_CACHE_ATTR, cache)
    return cache


def _loaded_app_modules() -> dict[str, ModuleType]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }


def _app_origin(modules: dict[str, ModuleType]) -> str | None:
    root = modules.get("app")
    module_file = str(getattr(root, "__file__", "") or "")
    if "/apps/api/" in module_file:
        return "api"
    if "/apps/worker/" in module_file:
        return "worker"
    return None


def _switch_to_api_app() -> None:
    if not sys.path or sys.path[0] != API_ROOT:
        if API_ROOT in sys.path:
            sys.path.remove(API_ROOT)
        sys.path.insert(0, API_ROOT)
    loaded = _loaded_app_modules()
    origin = _app_origin(loaded)
    cache = _app_module_cache()
    if origin == APP_ORIGIN:
        cache[APP_ORIGIN] = loaded
        return
    if origin is not None:
        cache[origin] = loaded
    for name in loaded:
        sys.modules.pop(name, None)
    sys.modules.update(cache.get(APP_ORIGIN, {}))


def _is_api_test(node) -> bool:
    fspath = getattr(node, "fspath", None) or getattr(node, "path", None)
    return fspath is not None and str(fspath).startswith(TESTS_DIR)


def pytest_collectstart(collector):
    if _is_api_test(collector):
        _switch_to_api_app()


def pytest_runtest_setup(item):
    if _is_api_test(item):
        _switch_to_api_app()


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    for name in _loaded_app_modules():
        sys.modules.pop(name, None)
    if API_ROOT in sys.path:
        sys.path.remove(API_ROOT)
    if hasattr(sys, _APP_CACHE_ATTR):
        delattr(sys, _APP_CACHE_ATTR)


_switch_to_api_app()
