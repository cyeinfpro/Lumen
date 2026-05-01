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

from pathlib import Path
import sys

API_ROOT = str(Path(__file__).resolve().parents[1])
TESTS_DIR = str(Path(__file__).resolve().parent)


def _switch_to_api_app() -> None:
    if not sys.path or sys.path[0] != API_ROOT:
        if API_ROOT in sys.path:
            sys.path.remove(API_ROOT)
        sys.path.insert(0, API_ROOT)
    loaded = sys.modules.get("app")
    if loaded is not None:
        mod_file = getattr(loaded, "__file__", "") or ""
        if "/apps/api/" not in mod_file:
            for key in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
                del sys.modules[key]


def _is_api_test(node) -> bool:
    fspath = getattr(node, "fspath", None) or getattr(node, "path", None)
    return fspath is not None and str(fspath).startswith(TESTS_DIR)


def pytest_collectstart(collector):
    if _is_api_test(collector):
        _switch_to_api_app()


def pytest_runtest_setup(item):
    if _is_api_test(item):
        _switch_to_api_app()


_switch_to_api_app()
