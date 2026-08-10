from __future__ import annotations

from dataclasses import fields
import inspect
from pathlib import Path

from app.tasks import generation_parts
from app.tasks.generation_parts import queue_claim, runner
from app.tasks.generation_parts.runner_phase_services import (
    ClaimGenerationServices,
    DispatchGenerationServices,
)


PARTS_ROOT = Path(__file__).resolve().parents[1] / "app" / "tasks" / "generation_parts"


def _line_count(name: str) -> int:
    return len((PARTS_ROOT / name).read_text(encoding="utf-8").splitlines())


def test_generation_scheduler_orchestrators_fit_wave4_budgets() -> None:
    assert _line_count("queue_claim.py") <= 800
    assert _line_count("runner.py") <= 900


def test_queue_claim_delegates_each_scheduler_control() -> None:
    source = inspect.getsource(queue_claim.reserve_image_queue_slot)

    assert "ready_queue_rank(" in source
    assert "select_provider_candidates(" in source
    assert "filter_avoided_providers(" in source
    assert "reserve_from_provider_candidates(" in source
    assert "reserve_dual_race_slot(" in source


def test_runner_delegates_claim_and_dispatch_phases() -> None:
    wrapper_source = inspect.getsource(runner.run_generation)
    source = inspect.getsource(runner._run_generation_scoped)
    prepare_source = inspect.getsource(runner._prepare_generation_attempt)
    result_source = inspect.getsource(runner._obtain_generation_result)

    assert "await _run_generation_scoped(state)" in wrapper_source
    assert "await _load_initial_generation(state)" in source
    assert "await _prepare_generation_attempt(state)" in source
    assert "await _prepare_provider_reservation(state)" in prepare_source
    assert "await _start_generation_attempt(state)" in prepare_source
    assert "await _obtain_generation_result(state)" in source
    assert "await _prepare_upstream_request(state)" in result_source
    assert "await _dispatch_upstream_request(state)" in result_source


def test_generation_phase_service_views_are_narrow() -> None:
    assert tuple(field.name for field in fields(ClaimGenerationServices)) == (
        "store",
        "artifacts",
        "billing",
        "events",
    )
    assert tuple(field.name for field in fields(DispatchGenerationServices)) == (
        "store",
        "events",
        "provider",
    )


def test_scheduler_public_contracts_are_exported_from_index() -> None:
    expected = {
        "ClaimGenerationServices",
        "DispatchGenerationServices",
        "GenerationResourceLease",
        "release_generation_runtime_resources",
        "release_image_queue_slot",
        "reserve_image_queue_slot",
        "run_generation",
    }

    assert expected <= set(generation_parts.__all__)
    for name in expected:
        assert getattr(generation_parts, name) is not None


def test_new_scheduler_modules_do_not_reach_composition_roots() -> None:
    for name in (
        "queue_candidate.py",
        "queue_fairness.py",
        "queue_provider.py",
        "runner_claim_phase.py",
        "runner_dispatch_phase.py",
        "runner_phase_services.py",
    ):
        source = (PARTS_ROOT / name).read_text(encoding="utf-8")
        assert "default_runtime" not in source
        assert "composition" not in source
