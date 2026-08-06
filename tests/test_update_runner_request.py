from __future__ import annotations

import importlib.util
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_runner() -> ModuleType:
    path = ROOT / "scripts" / "update_runner.py"
    spec = importlib.util.spec_from_file_location("lumen_update_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "target_tag": "v1.2.3",
        "channel": "stable",
        "force_redeploy": False,
        "idempotency_key": "idem-123",
        "proxy_url": None,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


def _journal_request(payload: dict[str, object]) -> dict[str, object]:
    return {
        "channel": payload["channel"],
        "force_redeploy": payload["force_redeploy"],
        "idempotency_key_sha256": hashlib.sha256(
            str(payload["idempotency_key"]).encode("utf-8")
        ).hexdigest(),
        "resolved_tag": payload["target_tag"],
    }


def test_update_runner_builds_fixed_environment_without_path_overrides(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request()), encoding="utf-8")

    request = runner.load_request(request_path)
    env = runner.build_environment(request)

    assert env["LUMEN_IMAGE_TAG"] == "v1.2.3"
    assert env["LUMEN_VERSION"] == "1.2.3"
    assert env["LUMEN_UPDATE_BUILD"] == "0"
    assert env["LUMEN_UPDATE_FAST_BACKUP"] == "1"
    assert env["LUMEN_UPDATE_REQUIRE_MIGRATION_BACKUP"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "LUMEN_UPDATE_SKIP_BACKUP" not in env
    assert "LUMEN_UPDATE_ROOT" not in env
    assert "LUMEN_REPO_DIR" not in env
    assert "LUMEN_SOURCE_ROOT" not in env


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"target_tag": "../../tmp/payload"}, "target_tag"),
        ({"target_tag": "latest"}, "target_tag"),
        ({"channel": "shell"}, "channel"),
        ({"force_redeploy": "1"}, "force_redeploy"),
        ({"proxy_url": "http://proxy.example/path"}, "proxy_url"),
        ({"proxy_url": "http://proxy.example\nX=1"}, "proxy_url"),
        ({"issued_at": "2020-01-01T00:00:00+00:00"}, "stale"),
    ],
)
def test_update_runner_rejects_invalid_request_values(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    runner = _load_runner()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request(**overrides)), encoding="utf-8")

    with pytest.raises(runner.UpdateRequestError, match=message):
        runner.load_request(request_path)


def test_update_runner_rejects_unknown_fields_and_symlinks(tmp_path: Path) -> None:
    runner = _load_runner()
    real_path = tmp_path / "real.json"
    real_path.write_text(
        json.dumps(_request(LUMEN_UPDATE_ROOT="/tmp/attacker")),
        encoding="utf-8",
    )
    with pytest.raises(runner.UpdateRequestError, match="fields"):
        runner.load_request(real_path)

    link_path = tmp_path / "request.json"
    link_path.symlink_to(real_path)
    with pytest.raises(runner.UpdateRequestError, match="cannot read"):
        runner.load_request(link_path)


def test_active_journal_auto_resumes_with_preserved_stale_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            _request(issued_at="2020-01-01T00:00:00+00:00")
        ),
        encoding="utf-8",
    )
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps({"schema": 2, "status": "running"}),
        encoding="utf-8",
    )
    update_script = tmp_path / "update.sh"
    update_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setenv("LUMEN_UPDATE_REQUEST", str(request_path))
    monkeypatch.setenv("LUMEN_UPDATE_JOURNAL", str(journal))
    monkeypatch.setenv("LUMEN_UPDATE_SCRIPT", str(update_script))
    monkeypatch.setenv(
        "LUMEN_UPDATE_RECOVERY_MARKER",
        str(tmp_path / "resume.marker"),
    )
    monkeypatch.setenv("LUMEN_UPDATE_CLAIM", str(tmp_path / "claim.json"))
    monkeypatch.setattr(
        runner.os,
        "execve",
        lambda executable, argv, env: captured.update(
            executable=executable,
            argv=argv,
            env=env,
        ),
    )

    assert runner.main([]) == 127
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["LUMEN_UPDATE_RESUME"] == "1"
    assert child_env["LUMEN_IMAGE_TAG"] == "v1.2.3"
    assert child_env["LUMEN_UPDATE_JOURNAL"] == str(journal)
    assert child_env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_active_post_check_journal_without_request_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema": 2,
                "status": "failed",
                "operation_id": "update-20260803",
                "completed_phases": ["lock", "self_update_scripts", "check"],
                "request": {
                    "channel": "stable",
                    "resolved_tag": "v1.2.3",
                    "force_redeploy": False,
                },
            }
        ),
        encoding="utf-8",
    )
    update_script = tmp_path / "update.sh"
    update_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    executed = False
    monkeypatch.setenv("LUMEN_UPDATE_REQUEST", str(tmp_path / "missing.json"))
    monkeypatch.setenv("LUMEN_UPDATE_JOURNAL", str(journal))
    monkeypatch.setenv("LUMEN_UPDATE_SCRIPT", str(update_script))
    monkeypatch.setenv("LUMEN_UPDATE_CLAIM", str(tmp_path / "claim.json"))

    def fake_execve(*_args: object) -> None:
        nonlocal executed
        executed = True

    monkeypatch.setattr(runner.os, "execve", fake_execve)

    assert runner.main([]) == 2
    assert not executed
    assert journal.exists()


def test_runner_cleanup_preserves_request_until_journal_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    request = tmp_path / "request.json"
    journal = tmp_path / "journal.json"
    marker = tmp_path / "resume.marker"
    trigger = tmp_path / "trigger"
    running = tmp_path / "running"
    claim = tmp_path / "claim.json"
    request_payload = _request()
    request.write_text(json.dumps(request_payload), encoding="utf-8")
    marker.write_text("update-cleanup\n", encoding="utf-8")
    trigger.write_text("trigger-state\n", encoding="utf-8")
    running.write_text("running-state\n", encoding="utf-8")
    journal.write_text(
        json.dumps({"schema": 2, "status": "running"}),
        encoding="utf-8",
    )
    for key, path in (
        ("LUMEN_UPDATE_REQUEST", request),
        ("LUMEN_UPDATE_JOURNAL", journal),
        ("LUMEN_UPDATE_RECOVERY_MARKER", marker),
        ("LUMEN_UPDATE_TRIGGER", trigger),
        ("LUMEN_UPDATE_RUNNING", running),
        ("LUMEN_UPDATE_CLAIM", claim),
    ):
        monkeypatch.setenv(key, str(path))
    runner.write_runtime_claim(
        claim,
        runner.load_request(request),
        request,
        trigger,
        running,
    )

    assert runner.cleanup_runtime_files() == 0
    assert request.exists()
    assert marker.exists()
    assert trigger.exists()
    assert running.exists()
    assert claim.exists()

    journal.write_text(
        json.dumps(
            {
                "schema": 2,
                "status": "complete",
                "operation_id": "update-cleanup",
                "request": _journal_request(request_payload),
            }
        ),
        encoding="utf-8",
    )
    assert runner.cleanup_runtime_files() == 0
    assert not request.exists()
    assert not marker.exists()
    assert not trigger.exists()
    assert not running.exists()
    assert not claim.exists()


def test_manual_required_journal_preserves_request_trigger_and_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    request = tmp_path / "request.json"
    journal = tmp_path / "journal.json"
    marker = tmp_path / "resume.marker"
    trigger = tmp_path / "trigger"
    running = tmp_path / "running"
    claim = tmp_path / "claim.json"
    request_payload = _request()
    request.write_text(json.dumps(request_payload), encoding="utf-8")
    marker.write_text("manual-required\n", encoding="utf-8")
    trigger.write_text("trigger-state\n", encoding="utf-8")
    running.write_text("running-state\n", encoding="utf-8")
    journal.write_text(
        json.dumps(
            {
                "schema": 2,
                "status": "manual_required",
                "operation_id": "update-manual-required",
                "request": _journal_request(request_payload),
            }
        ),
        encoding="utf-8",
    )
    for key, path in (
        ("LUMEN_UPDATE_REQUEST", request),
        ("LUMEN_UPDATE_JOURNAL", journal),
        ("LUMEN_UPDATE_RECOVERY_MARKER", marker),
        ("LUMEN_UPDATE_TRIGGER", trigger),
        ("LUMEN_UPDATE_RUNNING", running),
        ("LUMEN_UPDATE_CLAIM", claim),
    ):
        monkeypatch.setenv(key, str(path))
    runner.write_runtime_claim(
        claim,
        runner.load_request(request),
        request,
        trigger,
        running,
    )

    assert runner.cleanup_runtime_files() == 0
    assert request.exists()
    assert journal.exists()
    assert marker.exists()
    assert trigger.exists()
    assert running.exists()
    assert claim.exists()


def test_terminal_journal_is_archived_before_a_different_request_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    old_request = _request(idempotency_key="old-request")
    new_request = _request(
        target_tag="v1.2.4",
        idempotency_key="new-request",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(new_request), encoding="utf-8")
    trigger = tmp_path / "trigger"
    trigger.write_text("new-trigger\n", encoding="utf-8")
    running = tmp_path / "running"
    running.write_text("new-running\n", encoding="utf-8")
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema": 2,
                "status": "complete",
                "operation_id": "update-old-operation",
                "request": _journal_request(old_request),
            }
        ),
        encoding="utf-8",
    )
    update_script = tmp_path / "update.sh"
    update_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    archive = tmp_path / "journal-archive"
    claim = tmp_path / "claim.json"
    captured: dict[str, object] = {}
    for key, path in (
        ("LUMEN_UPDATE_REQUEST", request_path),
        ("LUMEN_UPDATE_JOURNAL", journal),
        ("LUMEN_UPDATE_TRIGGER", trigger),
        ("LUMEN_UPDATE_RUNNING", running),
        ("LUMEN_UPDATE_CLAIM", claim),
        ("LUMEN_UPDATE_JOURNAL_ARCHIVE", archive),
        ("LUMEN_UPDATE_SCRIPT", update_script),
    ):
        monkeypatch.setenv(key, str(path))
    monkeypatch.setattr(
        runner.os,
        "execve",
        lambda executable, argv, env: captured.update(
            executable=executable,
            argv=argv,
            env=env,
        ),
    )

    assert runner.main([]) == 127
    assert not journal.exists()
    archived = list(archive.glob("update-old-operation.*.complete.*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["operation_id"] == (
        "update-old-operation"
    )
    assert request_path.exists()
    assert trigger.exists()
    assert running.exists()
    assert claim.exists()
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["LUMEN_UPDATE_IDEMPOTENCY_KEY"] == "new-request"


def test_terminal_cleanup_preserves_replacement_request_and_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    consumed_payload = _request(idempotency_key="consumed-request")
    request = tmp_path / "request.json"
    trigger = tmp_path / "trigger"
    running = tmp_path / "running"
    claim = tmp_path / "claim.json"
    journal = tmp_path / "journal.json"
    request.write_text(json.dumps(consumed_payload), encoding="utf-8")
    trigger.write_text("consumed-trigger\n", encoding="utf-8")
    running.write_text("consumed-running\n", encoding="utf-8")
    runner.write_runtime_claim(
        claim,
        runner.load_request(request),
        request,
        trigger,
        running,
    )
    journal.write_text(
        json.dumps(
            {
                "schema": 2,
                "status": "complete",
                "operation_id": "update-consumed",
                "request": _journal_request(consumed_payload),
            }
        ),
        encoding="utf-8",
    )
    replacement = _request(
        target_tag="v1.2.4",
        idempotency_key="replacement-request",
    )
    request.write_text(json.dumps(replacement), encoding="utf-8")
    trigger.write_text("replacement-trigger\n", encoding="utf-8")
    running.write_text("replacement-running\n", encoding="utf-8")
    for key, path in (
        ("LUMEN_UPDATE_REQUEST", request),
        ("LUMEN_UPDATE_JOURNAL", journal),
        ("LUMEN_UPDATE_TRIGGER", trigger),
        ("LUMEN_UPDATE_RUNNING", running),
        ("LUMEN_UPDATE_CLAIM", claim),
        ("LUMEN_UPDATE_RECOVERY_MARKER", tmp_path / "resume.marker"),
    ):
        monkeypatch.setenv(key, str(path))

    assert runner.cleanup_runtime_files() == 0
    assert json.loads(request.read_text(encoding="utf-8"))["idempotency_key"] == (
        "replacement-request"
    )
    assert trigger.read_text(encoding="utf-8") == "replacement-trigger\n"
    assert running.read_text(encoding="utf-8") == "replacement-running\n"
    assert not claim.exists()


def test_update_systemd_consumer_watches_resume_marker_and_restarts() -> None:
    service = (
        ROOT / "deploy/systemd/lumen-update-runner.service"
    ).read_text(encoding="utf-8")
    path_unit = (
        ROOT / "deploy/systemd/lumen-update.path"
    ).read_text(encoding="utf-8")

    assert "Restart=on-failure" in service
    assert "update_runner.py --cleanup" in service
    assert "LUMEN_UPDATE_CLAIM=/opt/lumen/shared/.update-claim.json" in service
    assert "PathExists=/opt/lumen/shared/.update-resume" in path_unit
