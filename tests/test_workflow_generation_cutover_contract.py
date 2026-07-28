from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_APP = ROOT / "apps" / "api" / "app"
WORKFLOWS = API_APP / "workflows"
GENERATION = ROOT / "apps" / "worker" / "app" / "tasks" / "generation_parts"
WORKER_APP = ROOT / "apps" / "worker" / "app"
UPSTREAM_PARTS = WORKER_APP / "upstream_parts"


def _python_files(root: Path, *, include_tests: bool = False) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
        and (include_tests or "tests" not in path.parts)
    )


def _module_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _module_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return _module_name(node.value)
    return ""


def _annotation_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    return ast.unparse(node)


def test_workflow_legacy_packages_are_retired() -> None:
    assert not (API_APP / "workflow_services").exists()
    assert not (API_APP / "workflow_domain").exists()


def test_workflow_production_has_no_legacy_imports() -> None:
    violations: list[str] = []
    for path in _python_files(API_APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if "workflow_services" in module or "workflow_domain" in module:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{module}"
                    )
    assert violations == []


def test_workflow_domain_and_application_keep_framework_boundaries() -> None:
    violations: list[str] = []
    banned_domain_roots = {
        "fastapi",
        "httpx",
        "redis",
        "sqlalchemy",
        "starlette",
    }
    banned_application_roots = {
        "fastapi",
        "httpx",
        "redis",
        "sqlalchemy",
        "starlette",
    }
    banned_adapter_roots = {"fastapi", "starlette"}
    for layer, banned in (
        (WORKFLOWS / "domain", banned_domain_roots),
        (WORKFLOWS / "application", banned_application_roots),
        (WORKFLOWS / "adapters", banned_adapter_roots),
    ):
        for path in _python_files(layer):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    root = module.split(".", 1)[0]
                    if root in banned or (
                        layer == WORKFLOWS / "application"
                        and (
                            "adapters" in module.split(".")
                            or module
                            in {
                                "lumen_core.models",
                                "lumen_core.model_entities",
                                "lumen_core.providers",
                            }
                        )
                    ):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:{module}"
                        )
    assert violations == []


def test_workflow_application_has_no_infrastructure_operation_modules() -> None:
    operations = WORKFLOWS / "application" / "operations"
    assert not operations.exists() or not list(operations.glob("*.py"))
    adapter_operations = WORKFLOWS / "adapters" / "operations"
    assert {path.name for path in adapter_operations.glob("*.py")} == {
        "__init__.py",
        "apparel.py",
        "model_library.py",
        "poster.py",
        "projects.py",
    }


def test_workflow_operation_adapters_have_no_compatibility_forwarders() -> None:
    violations: list[str] = []
    operation_root = WORKFLOWS / "adapters" / "operations"
    for path in _python_files(operation_root):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        if "F401" in source or "F405" in source:
            violations.append(f"{path.relative_to(ROOT)}:compatibility-noqa")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:"
                            f"private-import:{alias.name}"
                        )
            elif (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
                and isinstance(node.value, ast.Name)
                and node.value.id.startswith("_")
            ):
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:"
                    f"private-backread:{node.value.id}.{node.attr}"
                )
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, (ast.Name, ast.Attribute)):
                continue
            targets = [
                target.id for target in node.targets if isinstance(target, ast.Name)
            ]
            if targets:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:"
                    f"forwarding-assignment:{','.join(targets)}"
                )
    assert violations == []


def test_workflow_http_routes_do_not_construct_infrastructure_adapters() -> None:
    route_roots = (
        API_APP / "routes" / "workflow_routes",
        WORKFLOWS / "transport" / "http",
    )
    violations: list[str] = []
    for route_root in route_roots:
        for path in _python_files(route_root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                if (
                    "workflows.adapters" in module
                    or module == "adapters"
                    or module.startswith("adapters.")
                ):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{module}"
                    )
    assert violations == []


def test_workflow_http_routes_do_not_own_infrastructure() -> None:
    route_roots = (
        API_APP / "routes" / "workflow_routes",
        WORKFLOWS / "transport" / "http",
    )
    violations: list[str] = []
    for route_root in route_roots:
        for path in _python_files(route_root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    if module.split(".", 1)[0] in {"httpx", "sqlalchemy"} or module in {
                        "lumen_core.models",
                        "lumen_core.model_entities",
                    }:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:{module}"
                        )
    assert violations == []


def test_workflow_http_transport_files_are_bounded() -> None:
    transport_root = WORKFLOWS / "transport" / "http"
    violations = [
        f"{path.relative_to(ROOT)}:{len(path.read_text(encoding='utf-8').splitlines())}"
        for path in _python_files(transport_root)
        if len(path.read_text(encoding="utf-8").splitlines()) > 800
    ]
    assert violations == []


def test_workflow_public_router_has_no_compatibility_reexports() -> None:
    path = API_APP / "routes" / "workflows.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exported: list[str] | None = None
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, (ast.List, ast.Tuple)):
            exported = [
                element.value
                for element in statement.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
    assert exported == ["router"]


def test_workflow_application_is_composed_in_production() -> None:
    dependencies_path = WORKFLOWS / "transport" / "http" / "dependencies.py"
    dependencies_source = dependencies_path.read_text(encoding="utf-8")
    dependencies_tree = ast.parse(
        dependencies_source,
        filename=str(dependencies_path),
    )
    assert "_APPLICATION" not in dependencies_source
    assert "build_workflow_application" not in dependencies_source
    assert "request.app.state.workflow_application" in dependencies_source
    assert not any(
        isinstance(node, ast.Call)
        and _module_name(node.func) == "build_workflow_application"
        for node in ast.walk(dependencies_tree)
    )

    main_path = API_APP / "main.py"
    main_source = main_path.read_text(encoding="utf-8")
    main_tree = ast.parse(main_source, filename=str(main_path))
    builder_calls = [
        node
        for node in ast.walk(main_tree)
        if isinstance(node, ast.Call)
        and _module_name(node.func) == "build_workflow_application"
    ]
    assert len(builder_calls) == 1
    assert any(
        keyword.arg == "include_http"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in builder_calls[0].keywords
    )
    assert "app.state.workflow_application =" in main_source
    assert "build_workflow_http_application" not in dependencies_source
    assert "WorkflowHttpHandlers" not in dependencies_source


def test_workflow_composition_has_no_module_bag_or_global_runtime() -> None:
    path = WORKFLOWS / "composition.py"
    source = path.read_text(encoding="utf-8")
    violations = [
        token
        for token in (
            "ModuleType",
            "WorkflowInfrastructure",
            "_WORKFLOW_INFRASTRUCTURE",
            "workflow_infrastructure",
        )
        if token in source
    ]
    assert violations == []


def test_workflow_application_has_named_use_cases_not_dynamic_handler_registry() -> (
    None
):
    path = WORKFLOWS / "transport" / "http" / "use_cases.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    assert "WorkflowEndpoint" not in source
    assert "WorkflowHttpHandlers" not in source
    assert "WorkflowAction" not in source
    assert "Callable[" not in source
    assert "Any" not in source
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.args.vararg is not None or node.args.kwarg is not None)
        for node in ast.walk(tree)
    )
    workflow_use_cases = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WorkflowHttpUseCases"
    )
    method_names = {
        node.name
        for node in workflow_use_cases.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "execute" not in method_names
    assert {
        "get_workflow",
        "generate_apparel_model_library_job",
        "inpaint_poster_render",
    } <= method_names
    assert {
        "list_runs",
        "create_apparel_model_showcase",
        "create_poster_design_workflow",
    }.isdisjoint(method_names)


def test_workflow_legacy_action_facades_are_removed() -> None:
    forbidden_names = {
        "ApparelWorkflowUseCases",
        "ModelLibraryWorkflowUseCases",
        "PosterWorkflowUseCases",
        "ProjectWorkflowUseCases",
        "WorkflowAction",
    }
    violations: list[str] = []
    for path in _python_files(WORKFLOWS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.id}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name in forbidden_names:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}"
                        )
    assert violations == []


def test_workflow_production_transport_calls_named_application_methods() -> None:
    violations: list[str] = []
    route_roots = (
        API_APP / "routes" / "workflow_routes",
        WORKFLOWS / "transport" / "http",
    )
    for route_root in route_roots:
        for path in _python_files(route_root):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "execute":
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:.execute"
                    )
            if "build_workflow_run_list_query" in source:
                violations.append(
                    f"{path.relative_to(ROOT)}:build_workflow_run_list_query"
                )
    assert violations == []


def test_workflow_application_has_no_runtime_module_bag() -> None:
    assert not (WORKFLOWS / "application" / "library_sync_operation.py").exists()
    violations: list[str] = []
    for path in _python_files(WORKFLOWS / "application"):
        source = path.read_text(encoding="utf-8")
        for token in ("runtime: Any", "runtime.httpx", "runtime._"):
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}:{token}")
    assert violations == []


def test_workflow_transport_depends_on_application_api_or_composition() -> None:
    violations: list[str] = []
    for path in _python_files(WORKFLOWS / "transport"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if "adapters" in module.split(".") or "application.operations" in module:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{module}")
    assert violations == []


def test_workflow_services_production_calls_are_zero() -> None:
    violations: list[str] = []
    for path in _python_files(API_APP):
        source = path.read_text(encoding="utf-8")
        if "workflow_services" in source:
            violations.append(path.relative_to(ROOT).as_posix())
    assert violations == []


def test_workflow_cutover_keeps_cross_slice_behavior_parity_coverage() -> None:
    path = ROOT / "apps" / "api" / "tests" / "test_workflow_http_cutover_parity.py"
    source = path.read_text(encoding="utf-8")
    assert {
        "test_project_http_cutover_preserves_adapter_result_and_arguments",
        "test_apparel_http_cutover_preserves_runtime_adapter_semantics",
        "test_model_library_http_cutover_preserves_adapter_contract",
        "test_poster_http_cutover_preserves_adapter_contract",
    } <= {
        node.name
        for node in ast.parse(source, filename=str(path)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_generation_runtime_has_no_ambient_runtime_state() -> None:
    violations: list[str] = []
    forbidden_names = {
        "DEFAULT_GENERATION_RUNTIME",
        "QUEUE_RUNTIME",
        "RuntimeSlot",
        "install_generation_ports",
        "generation_ports",
        "generation_domain_ports",
        "generation_persistence_ports",
        "generation_queue_ports",
        "generation_billing_ports",
        "generation_events_ports",
        "generation_provider_ports",
        "generation_lease_ports",
    }
    for path in _python_files(GENERATION):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.id}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in forbidden_names:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}"
                        )
            elif isinstance(node, ast.Call) and _module_name(node.func) == "ContextVar":
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:ContextVar")
    assert violations == []


def test_generation_upstream_chain_has_no_contextvar_or_push_pop_bridge() -> None:
    violations: list[str] = []
    forbidden_names = {
        "ContextVar",
        "push_image_quota_context",
        "pop_image_quota_context",
        "push_image_trace_id",
        "pop_image_trace_id",
        "push_image_retry_attempt",
        "pop_image_retry_attempt",
        "_image_trace_id_ctx",
        "_image_quota_scope_ctx",
        "_image_quota_reservation_ctx",
        "_image_retry_attempt_ctx",
    }
    for root in (GENERATION, UPSTREAM_PARTS):
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in forbidden_names:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.id}"
                    )
                elif isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.attr}"
                    )
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = (
                        node.module
                        if isinstance(node, ast.ImportFrom)
                        else ",".join(alias.name for alias in node.names)
                    )
                    if module and "contextvars" in module:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:{module}"
                        )
    assert violations == []


def test_generation_upstream_runtime_has_no_process_registry() -> None:
    provider_runtime = WORKER_APP / "provider_runtime"
    forbidden_names = {
        "_PROCESS_SERVICES",
        "_install_legacy_process_services",
        "install_upstream_services",
        "upstream_services",
    }
    violations: list[str] = []
    for root in (provider_runtime, UPSTREAM_PARTS):
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in forbidden_names:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.id}"
                    )
                elif isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.attr}"
                    )
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if alias.name in forbidden_names:
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}"
                            )
    assert violations == []


def test_generation_tests_do_not_patch_compatibility_runtime_facades() -> None:
    tests_root = ROOT / "apps" / "worker" / "tests"
    violations: list[str] = []
    for path in _python_files(tests_root, include_tests=True):
        source = path.read_text(encoding="utf-8")
        if "_RuntimePortsProxy" in source:
            violations.append(f"{path.relative_to(ROOT)}:_RuntimePortsProxy")
        if (
            "synchronize_module_ports" in source
            and "app.tasks.generation_parts" in source
            and path.name != "task_parts_runtime_testing.py"
        ):
            violations.append(f"{path.relative_to(ROOT)}:synchronize_module_ports")
    assert violations == []


def test_generation_tests_do_not_import_private_runtime_facades() -> None:
    tests_root = ROOT / "apps" / "worker" / "tests"
    violations: list[str] = []
    for path in _python_files(tests_root, include_tests=True):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        support_aliases: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == "app.tasks.generation_parts":
                for alias in node.names:
                    if alias.name == "default_runtime":
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:default_runtime"
                        )
                    if alias.name == "composition_support":
                        support_aliases.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in support_aliases:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:"
                    f"{node.value.id}.{node.attr}"
                )
    assert violations == []


def test_generation_has_no_runtime_state_ledger_exemption() -> None:
    ledger_path = ROOT / "docs" / "refactors" / "module-runtime-state-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert isinstance(ledger, dict)
    modules = ledger.get("modules")
    assert isinstance(modules, list)
    violations = [
        entry["path"]
        for entry in modules
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and entry["path"].startswith("apps/worker/app/tasks/generation_parts/")
    ]
    assert violations == []


def test_generation_runtime_ports_are_typed() -> None:
    violations: list[str] = []
    for runtime_path in (
        GENERATION / "runtime.py",
        GENERATION / "services.py",
        GENERATION / "composition_ports.py",
    ):
        tree = ast.parse(
            runtime_path.read_text(encoding="utf-8"),
            filename=str(runtime_path),
        )
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if any(
                isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name == "__getattr__"
                for statement in node.body
            ):
                violations.append(
                    f"{runtime_path.relative_to(ROOT)}:{node.lineno}:"
                    f"{node.name}.__getattr__"
                )
            for statement in node.body:
                if not isinstance(statement, ast.AnnAssign):
                    continue
                annotation = _annotation_text(statement.annotation)
                field_name = _module_name(statement.target)
                if (
                    "Any" in annotation
                    or "ModuleType" in annotation
                    or "Callable[..., Any]" in annotation
                ):
                    violations.append(
                        f"{runtime_path.relative_to(ROOT)}:{statement.lineno}:"
                        f"{node.name}.{field_name}:{annotation}"
                    )
    assert violations == []


def test_generation_callable_dependencies_have_explicit_signatures() -> None:
    violations: list[str] = []
    forbidden_fragments = (
        "Callable[...,",
        "Awaitable[Any]",
    )
    for root in (GENERATION, UPSTREAM_PARTS):
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                annotations: list[ast.AST | None] = []
                if isinstance(node, ast.AnnAssign):
                    annotations.append(node.annotation)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    annotations.extend(
                        argument.annotation
                        for argument in (
                            *node.args.posonlyargs,
                            *node.args.args,
                            *node.args.kwonlyargs,
                        )
                    )
                    if node.args.vararg is not None:
                        annotations.append(node.args.vararg.annotation)
                    if node.args.kwarg is not None:
                        annotations.append(node.args.kwarg.annotation)
                    annotations.append(node.returns)
                for annotation in annotations:
                    text = _annotation_text(annotation)
                    if any(fragment in text for fragment in forbidden_fragments):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:{text}"
                        )
    assert violations == []


def test_generation_dependencies_are_semantic_services() -> None:
    services_path = GENERATION / "services.py"
    tree = ast.parse(
        services_path.read_text(encoding="utf-8"),
        filename=str(services_path),
    )
    run_generation_deps = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RunGenerationDeps"
    )
    fields = [
        _module_name(statement.target)
        for statement in run_generation_deps.body
        if isinstance(statement, ast.AnnAssign)
    ]
    assert fields == [
        "store",
        "artifacts",
        "billing",
        "events",
        "provider",
        "queue",
        "lease",
        "credentials",
        "workflows",
    ]


def test_generation_use_cases_receive_workflow_hooks_through_typed_services() -> None:
    violations: list[str] = []
    for path in _python_files(GENERATION):
        if path.name in {"composition_ports.py", "workflow_service.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == "workflow_service" or module.endswith(".workflow_service"):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{module}")
    assert violations == []


def test_generation_use_cases_do_not_reach_into_legacy_port_bags() -> None:
    violations: list[str] = []
    service_prefixes = (
        "services.",
        "g.",
        "ports.",
        "state.services.",
        "context.services.",
    )
    legacy_namespaces = {"domain", "persistence", "lease"}
    for path in _python_files(GENERATION):
        if path.name in {
            "composition.py",
            "composition_ports.py",
            "default_runtime.py",
            "runtime.py",
            "services.py",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            attribute = _module_name(node)
            if not attribute.startswith(service_prefixes):
                continue
            parts = attribute.split(".")
            service_index = parts.index("services") + 1 if "services" in parts else 1
            if service_index >= len(parts):
                continue
            namespace = parts[service_index]
            accessed_name = (
                parts[service_index + 1] if service_index + 1 < len(parts) else ""
            )
            if namespace in legacy_namespaces or accessed_name.startswith("_"):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{attribute}")
    assert violations == []


def test_generation_runtime_facades_do_not_use_dynamic_attribute_forwarding() -> None:
    violations: list[str] = []
    for runtime_path in (
        GENERATION / "default_runtime.py",
        GENERATION / "runtime.py",
        GENERATION / "services.py",
        GENERATION / "composition_ports.py",
    ):
        tree = ast.parse(
            runtime_path.read_text(encoding="utf-8"),
            filename=str(runtime_path),
        )
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == "__getattr__"
            ):
                violations.append(
                    f"{runtime_path.relative_to(ROOT)}:{node.lineno}:__getattr__"
                )
    assert violations == []


def test_generation_legacy_ports_aggregate_is_removed() -> None:
    violations: list[str] = []
    for path in _python_files(GENERATION):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "GenerationPorts":
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_generation_use_cases_do_not_accept_untyped_runtime_facades() -> None:
    violations: list[str] = []
    for filename in ("failure.py", "progress.py", "runner.py", "success.py"):
        path = GENERATION / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for argument in (*node.args.posonlyargs, *node.args.args):
                if argument.arg not in {"g", "ports", "runtime", "deps"}:
                    continue
                if _annotation_text(argument.annotation) in {"", "Any"}:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{argument.lineno}:"
                        f"{node.name}({argument.arg})"
                    )
    assert violations == []


def test_generation_composition_root_is_bounded() -> None:
    budgets = {
        "default_runtime.py": 50,
        "composition.py": 150,
        "composition_ports.py": 650,
        "composition_support.py": 350,
    }
    violations = [
        f"{path.relative_to(ROOT)}:{len(path.read_text(encoding='utf-8').splitlines())}"
        for name, budget in budgets.items()
        for path in (GENERATION / name,)
        if len(path.read_text(encoding="utf-8").splitlines()) > budget
    ]
    assert violations == []


def test_generation_composition_support_is_not_a_private_alias_facade() -> None:
    path = GENERATION / "composition_support.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if isinstance(value, ast.Attribute):
                for target in targets:
                    name = _module_name(target)
                    if name.startswith("_"):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:{name}"
                        )
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name in {
            "__getattr__",
            "_upstream_impl",
        }:
            violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}")
    assert violations == []


def test_generation_has_small_fake_service_runtime_coverage() -> None:
    path = ROOT / "apps" / "worker" / "tests" / "test_task_parts_runtime.py"
    source = path.read_text(encoding="utf-8")
    assert "def _fake_generation_deps()" in source
    assert "RunGenerationDeps(" in source
    assert "test_generation_arq_entry_invokes_injected_fake_services" in source
