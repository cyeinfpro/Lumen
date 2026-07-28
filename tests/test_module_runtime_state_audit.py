from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "module_runtime_state_audit",
    ROOT / "scripts" / "module_runtime_state_audit.py",
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ModuleRuntimeFinding = MODULE.ModuleRuntimeFinding
audit_runtime_state = MODULE.audit_runtime_state
collect_module_runtime_findings = MODULE.collect_module_runtime_findings
load_ledger = MODULE.load_ledger


def _ledger(tmp_path: Path, modules: list[dict], *, max_total: int = 10) -> dict:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {"version": 1, "max_total": max_total, "modules": modules},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return load_ledger(path)


def test_detects_mutable_dataclass_instance_and_ignores_frozen(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        """
from dataclasses import dataclass, field

@dataclass
class RuntimeState:
    values: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class ImmutableConfig:
    value: int = 1

_runtime = RuntimeState()
_config = ImmutableConfig()
""".lstrip(),
        encoding="utf-8",
    )

    findings = collect_module_runtime_findings((package,), root=tmp_path)

    assert list(findings.values()) == [
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=11,
            symbol="_runtime",
            class_name="RuntimeState",
        )
    ]


def test_detects_lifecycle_owned_local_and_imported_instances(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        """
from runtime_support import HttpClient as ImportedClient

class ManagedRuntime:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

_runtime = ManagedRuntime()
_client = ImportedClient()
""".lstrip(),
        encoding="utf-8",
    )

    findings = collect_module_runtime_findings((package,), root=tmp_path)

    assert list(findings.values()) == [
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=10,
            symbol="_runtime",
            class_name="ManagedRuntime",
        ),
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=11,
            symbol="_client",
            class_name="runtime_support.HttpClient",
        ),
    ]


def test_ignores_stateless_and_immutable_configuration_instances(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        """
from dataclasses import dataclass

@dataclass(frozen=True)
class ImmutableConfig:
    retries: int = 3

class StatelessAdapter:
    def convert(self, value: str) -> str:
        return value.strip()

CONFIG = ImmutableConfig()
ADAPTER = StatelessAdapter()
""".lstrip(),
        encoding="utf-8",
    )

    assert collect_module_runtime_findings((package,), root=tmp_path) == {}


def test_detects_module_locks_clients_and_semaphores(tmp_path: Path) -> None:
    package = tmp_path / "app"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        """
import asyncio
from anyio import Semaphore as CapacitySemaphore
from httpx import AsyncClient

_lock = asyncio.Lock()
_client = AsyncClient()
_capacity = CapacitySemaphore(3)
""".lstrip(),
        encoding="utf-8",
    )

    findings = collect_module_runtime_findings((package,), root=tmp_path)

    assert list(findings.values()) == [
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=5,
            symbol="_lock",
            class_name="asyncio.Lock",
        ),
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=6,
            symbol="_client",
            class_name="httpx.AsyncClient",
        ),
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=7,
            symbol="_capacity",
            class_name="anyio.Semaphore",
        ),
    ]


def test_detects_cache_decorators_and_aliases(tmp_path: Path) -> None:
    package = tmp_path / "app"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        """
from functools import cache as memoize, lru_cache

@memoize
def cached_value() -> int:
    return 1

@lru_cache(maxsize=1)
def cached_other() -> int:
    return 2
""".lstrip(),
        encoding="utf-8",
    )

    findings = collect_module_runtime_findings((package,), root=tmp_path)

    assert list(findings.values()) == [
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=3,
            symbol="cached_value",
            class_name="functools.cache",
        ),
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=7,
            symbol="cached_other",
            class_name="functools.lru_cache",
        ),
    ]


def test_detects_typescript_lifecycle_singletons_not_function_locals(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    source = source_root / "runtime.ts"
    source.write_text(
        """
let sharedSocket: WebSocket | null = null;
const httpClient = new HttpClient();

function buildLocal() {
  let localSocket: WebSocket | null = null;
  const localClient = new HttpClient();
  return { localSocket, localClient };
}
""".lstrip(),
        encoding="utf-8",
    )

    findings = collect_module_runtime_findings((source_root,), root=tmp_path)

    assert list(findings.values()) == [
        ModuleRuntimeFinding(
            path="src/runtime.ts",
            line=1,
            symbol="sharedSocket",
            class_name="typescript:mutable WebSocket",
        ),
        ModuleRuntimeFinding(
            path="src/runtime.ts",
            line=2,
            symbol="httpClient",
            class_name="typescript:new HttpClient",
        ),
    ]


def test_ignores_typescript_read_only_constants_and_react_context(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    source = source_root / "constants.ts"
    source.write_text(
        """
import { createContext } from "react";

const STATUSES = new Set(["ready", "done"]);
const AppContext = createContext(null);
const IMMUTABLE_CONFIG = Object.freeze({ retries: 3 });
const CONFIG = { retries: 3 } as const;
""".lstrip(),
        encoding="utf-8",
    )

    assert collect_module_runtime_findings((source_root,), root=tmp_path) == {}


def test_unowned_module_runtime_state_fails(tmp_path: Path) -> None:
    finding = ModuleRuntimeFinding(
        path="app/new_runtime.py",
        line=10,
        symbol="_runtime",
        class_name="RuntimeState",
    )
    ledger = _ledger(tmp_path, [], max_total=1)

    assert audit_runtime_state({finding.key: finding}, ledger) == [
        "unowned module runtime state: app/new_runtime.py|_runtime|RuntimeState"
    ]


def test_owned_runtime_state_may_only_shrink(tmp_path: Path) -> None:
    known = ModuleRuntimeFinding(
        path="app/runtime.py",
        line=10,
        symbol="_runtime",
        class_name="RuntimeState",
    )
    extra = ModuleRuntimeFinding(
        path="app/runtime.py",
        line=11,
        symbol="_other",
        class_name="RuntimeState",
    )
    ledger = _ledger(
        tmp_path,
        [
            {
                "path": "app/runtime.py",
                "owner": "api",
                "max_instances": 1,
                "symbols": ["_runtime"],
                "retirement_condition": "move to lifespan",
            }
        ],
        max_total=1,
    )

    assert audit_runtime_state({known.key: known}, ledger) == []
    assert audit_runtime_state(
        {known.key: known, extra.key: extra},
        ledger,
    ) == [
        "module runtime state total grew: current=2 budget=1",
        "module runtime state budget grew: app/runtime.py current=2 budget=1",
        "unexpected module runtime symbol: app/runtime.py|_other",
    ]


def test_shrunk_runtime_state_requires_ledger_ratchet(tmp_path: Path) -> None:
    remaining = ModuleRuntimeFinding(
        path="app/runtime.py",
        line=10,
        symbol="_remaining",
        class_name="RuntimeState",
    )
    ledger = _ledger(
        tmp_path,
        [
            {
                "path": "app/runtime.py",
                "owner": "api",
                "max_instances": 2,
                "symbols": ["_remaining", "_retired"],
                "retirement_condition": "move to lifespan",
            },
            {
                "path": "app/retired.py",
                "owner": "api",
                "max_instances": 1,
                "symbols": ["_retired_module"],
                "retirement_condition": "move to lifespan",
            },
        ],
        max_total=3,
    )

    assert audit_runtime_state({remaining.key: remaining}, ledger) == [
        "module runtime state total baseline is stale: current=1 budget=3",
        "module runtime state budget is stale: app/runtime.py current=1 budget=2",
        "stale module runtime symbol: app/runtime.py|_retired",
        "stale module runtime state entry: app/retired.py",
    ]


def test_total_budget_prevents_cross_module_growth(tmp_path: Path) -> None:
    first = ModuleRuntimeFinding("app/a.py", 1, "_a", "State")
    second = ModuleRuntimeFinding("app/b.py", 1, "_b", "State")
    ledger = _ledger(
        tmp_path,
        [
            {
                "path": "app/a.py",
                "owner": "api",
                "max_instances": 1,
                "symbols": ["_a"],
                "retirement_condition": "remove",
            },
            {
                "path": "app/b.py",
                "owner": "api",
                "max_instances": 1,
                "symbols": ["_b"],
                "retirement_condition": "remove",
            },
        ],
        max_total=1,
    )

    assert audit_runtime_state(
        {first.key: first, second.key: second},
        ledger,
    ) == ["module runtime state total grew: current=2 budget=1"]


def test_repository_ledger_matches_current_total_ceiling() -> None:
    ledger = load_ledger()
    findings = collect_module_runtime_findings()

    assert ledger["max_total"] == 21
    assert sum(entry["max_instances"] for entry in ledger["modules"].values()) == 21
    assert len(findings) == 21
    assert any(finding.path.startswith("image-job/") for finding in findings.values())
    assert audit_runtime_state(findings, ledger) == []
