from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import main, middlewares  # noqa: E402
from app.config import Settings  # noqa: E402


def test_settings_allow_db_managed_bot_token() -> None:
    settings = Settings(telegram_bot_shared_secret="s" * 32, telegram_bot_token="")

    assert settings.telegram_bot_token == ""
    assert settings.telegram_bot_shared_secret == "s" * 32


class _HealthRecorder:
    def __init__(self) -> None:
        self.transitions: list[tuple[main.TgbotRuntimeStatus, str]] = []

    def transition(
        self,
        status: main.TgbotRuntimeStatus,
        reason: str,
    ) -> None:
        self.transitions.append((status, reason))


def _private_message(user_id: int = 42) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=user_id, type="private")
    msg.from_user = SimpleNamespace(id=user_id)
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_access_gate_fails_closed_on_cold_start_500() -> None:
    class FailingApi:
        async def get_access_config(self) -> dict[str, object]:
            raise middlewares.ApiError("http_error", "server error", 500)

    gate = middlewares.AccessGate(FailingApi())  # type: ignore[arg-type]
    msg = _private_message()
    handler = AsyncMock()

    await gate(handler, msg, {})

    handler.assert_not_awaited()
    msg.answer.assert_awaited_once_with("机器人访问配置暂时不可用，请稍后重试。")


@pytest.mark.asyncio
async def test_access_gate_retains_last_known_good_after_refresh_500() -> None:
    class FlakyApi:
        def __init__(self) -> None:
            self.calls = 0

        async def get_access_config(self) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                return {"bot_enabled": True, "allowed_user_ids": "42"}
            raise middlewares.ApiError("http_error", "server error", 500)

    api = FlakyApi()
    gate = middlewares.AccessGate(api)  # type: ignore[arg-type]
    msg = _private_message()
    handler = AsyncMock(return_value="handled")

    assert await gate(handler, msg, {}) == "handled"
    gate._last_refresh = 0.0  # noqa: SLF001
    assert await gate(handler, msg, {}) == "handled"

    assert api.calls == 2
    assert handler.await_count == 2
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_gate_rejects_group_chat_before_handler() -> None:
    gate = middlewares.AccessGate()
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=-100123, type="group")
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = SimpleNamespace(id=42)
    cb.answer = AsyncMock()
    handler = AsyncMock()

    await gate(handler, cb, {})

    handler.assert_not_awaited()
    cb.answer.assert_awaited_once_with("仅支持私聊使用。", show_alert=True)


@pytest.mark.asyncio
async def test_paused_lifecycle_waits_for_admin_restart_and_logs_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control_started = asyncio.Event()
    send_restart = asyncio.Event()

    async def fake_control_listener(
        stop_event: asyncio.Event,
        **_kwargs: object,
    ) -> None:
        control_started.set()
        await send_restart.wait()
        stop_event.set()

    monkeypatch.setattr(main, "_run_control_listener", fake_control_listener)
    monkeypatch.setattr(main, "_install_stop_signal_handlers", lambda _event: None)

    logger = logging.getLogger("test-tgbot-lifecycle")
    caplog.set_level(logging.ERROR, logger=logger.name)
    task = asyncio.create_task(
        main._pause_until_restart_or_stop(
            logger,
            "configuration error: missing test secret",
            level=logging.ERROR,
        )
    )

    await asyncio.wait_for(control_started.wait(), timeout=1)
    assert not task.done()

    send_restart.set()
    await asyncio.wait_for(task, timeout=1)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == logger.name and "configuration error" in record.getMessage()
    ]
    assert messages == [
        "configuration error: missing test secret; "
        "bot polling is paused until configuration recovery, "
        "an admin restart, or service stop"
    ]


@pytest.mark.asyncio
async def test_paused_lifecycle_rechecks_config_until_it_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_started = asyncio.Event()
    control_stopped = asyncio.Event()
    checks = 0

    async def fake_control_listener(
        stop_event: asyncio.Event,
        **_kwargs: object,
    ) -> None:
        control_started.set()
        try:
            await stop_event.wait()
        finally:
            control_stopped.set()

    async def recovery_check() -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("api temporarily unavailable")
        return checks >= 3

    monkeypatch.setattr(main, "_run_control_listener", fake_control_listener)
    monkeypatch.setattr(main, "_install_stop_signal_handlers", lambda _event: None)

    await asyncio.wait_for(
        main._pause_until_restart_or_stop(
            logging.getLogger("test-tgbot-recovery"),
            "telegram bot disabled",
            level=logging.INFO,
            recovery_check=recovery_check,
            refresh_interval_sec=0.01,
        ),
        timeout=1,
    )

    assert control_started.is_set()
    assert control_stopped.is_set()
    assert checks == 3


def test_paused_config_refresh_interval_is_bounded() -> None:
    assert 15 <= main._PAUSED_CONFIG_REFRESH_INTERVAL_SEC <= 30


def test_polling_error_classification_is_explicit() -> None:
    assert (
        main._classify_polling_error(main.TokenValidationError("bad token"))
        is main.PollingTerminationClass.INVALID_CONFIGURATION
    )
    assert (
        main._classify_polling_error(OSError("network down"))
        is main.PollingTerminationClass.RECOVERABLE_NETWORK
    )
    assert (
        main._classify_polling_error(RuntimeError("bug"))
        is main.PollingTerminationClass.UNKNOWN
    )


@pytest.mark.asyncio
async def test_long_backoff_is_interrupted_by_stop_event() -> None:
    stop_event = asyncio.Event()
    task = asyncio.create_task(main._sleep_or_stop(stop_event, 60.0))
    await asyncio.sleep(0)

    stop_event.set()

    assert await asyncio.wait_for(task, timeout=0.2) is True


@pytest.mark.asyncio
async def test_polling_supervisor_treats_stop_as_normal() -> None:
    stop_event = asyncio.Event()
    health = _HealthRecorder()

    async def start_polling() -> None:
        main._set_runtime_health(
            health,
            main.TgbotRuntimeStatus.POLLING,
            "telegram_identity_verified",
        )
        await asyncio.Event().wait()

    async def pause_invalid(_error: BaseException) -> None:
        pytest.fail("normal stop must not pause configuration")

    task = asyncio.create_task(
        main._run_polling_supervisor(
            start_polling=start_polling,
            stop_event=stop_event,
            logger=logging.getLogger("test-polling-stop"),
            runtime_health=health,
            pause_invalid_configuration=pause_invalid,
        )
    )
    for _attempt in range(10):
        await asyncio.sleep(0)
        if (
            main.TgbotRuntimeStatus.POLLING,
            "telegram_identity_verified",
        ) in health.transitions:
            break
    stop_event.set()

    assert await asyncio.wait_for(task, timeout=1) is (
        main.PollingTerminationClass.NORMAL_STOP
    )
    assert (
        main.TgbotRuntimeStatus.POLLING,
        "telegram_identity_verified",
    ) in health.transitions
    assert health.transitions[-1] == (
        main.TgbotRuntimeStatus.STOPPING,
        "polling_stop_requested",
    )


@pytest.mark.asyncio
async def test_polling_supervisor_does_not_assume_pending_task_is_ready() -> None:
    stop_event = asyncio.Event()
    health = _HealthRecorder()

    async def start_polling() -> None:
        await asyncio.Event().wait()

    async def pause_invalid(_error: BaseException) -> None:
        pytest.fail("normal stop must not pause configuration")

    task = asyncio.create_task(
        main._run_polling_supervisor(
            start_polling=start_polling,
            stop_event=stop_event,
            logger=logging.getLogger("test-polling-pending"),
            runtime_health=health,
            pause_invalid_configuration=pause_invalid,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert health.transitions == [
        (
            main.TgbotRuntimeStatus.POLLING_STARTING,
            "polling_attempt_start",
        )
    ]

    stop_event.set()
    assert await asyncio.wait_for(task, timeout=1) is (
        main.PollingTerminationClass.NORMAL_STOP
    )


@pytest.mark.asyncio
async def test_restart_readiness_requires_identity_polling_and_health() -> None:
    events: list[str] = []
    polling_started = asyncio.Event()
    polling_release = asyncio.Event()
    restart_ready = asyncio.Event()
    health = _HealthRecorder()

    class Bot:
        async def me(self) -> None:
            events.append("identity")

    class Dispatcher:
        @staticmethod
        def resolve_used_update_types() -> list[str]:
            return ["message"]

        async def start_polling(self, *_args: object, **_kwargs: object) -> None:
            events.append("polling")
            polling_started.set()
            await polling_release.wait()

    task = asyncio.create_task(
        main._run_ready_polling(
            bot=Bot(),  # type: ignore[arg-type]
            dispatcher=Dispatcher(),  # type: ignore[arg-type]
            runtime_health=health,  # type: ignore[arg-type]
            restart_ready=restart_ready,
        )
    )
    await polling_started.wait()
    await asyncio.sleep(0)

    assert events == ["identity", "polling"]
    assert restart_ready.is_set()
    assert health.transitions == [
        (
            main.TgbotRuntimeStatus.POLLING,
            "telegram_identity_verified",
        )
    ]

    polling_release.set()
    await task
    assert not restart_ready.is_set()


@pytest.mark.asyncio
async def test_polling_supervisor_retries_recoverable_network_failure() -> None:
    stop_event = asyncio.Event()
    attempts = 0
    sleeps: list[float] = []
    health = _HealthRecorder()

    async def start_polling() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary network failure")
        stop_event.set()

    async def pause_invalid(_error: BaseException) -> None:
        pytest.fail("network failure must not pause configuration")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await main._run_polling_supervisor(
        start_polling=start_polling,
        stop_event=stop_event,
        logger=logging.getLogger("test-polling-network"),
        runtime_health=health,
        pause_invalid_configuration=pause_invalid,
        sleep=fake_sleep,
    )

    assert result is main.PollingTerminationClass.NORMAL_STOP
    assert attempts == 2
    assert sleeps == [1.0]
    assert (
        main.TgbotRuntimeStatus.POLLING_BACKOFF,
        "polling_network_backoff",
    ) in health.transitions


@pytest.mark.asyncio
async def test_polling_supervisor_pauses_invalid_configuration() -> None:
    paused: list[BaseException] = []
    health = _HealthRecorder()
    counter = main.TGBOT_POLLING_FAILURES.labels(
        main.PollingTerminationClass.INVALID_CONFIGURATION.value
    )
    before = counter._value.get()

    async def start_polling() -> None:
        raise main.TokenValidationError("invalid token")

    async def pause_invalid(error: BaseException) -> None:
        paused.append(error)

    result = await main._run_polling_supervisor(
        start_polling=start_polling,
        stop_event=asyncio.Event(),
        logger=logging.getLogger("test-polling-config"),
        runtime_health=health,
        pause_invalid_configuration=pause_invalid,
    )

    assert result is main.PollingTerminationClass.INVALID_CONFIGURATION
    assert len(paused) == 1
    assert counter._value.get() == before + 1
    assert health.transitions[-1] == (
        main.TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
        "polling_invalid_configuration",
    )


@pytest.mark.asyncio
async def test_polling_supervisor_reraises_unknown_failure_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test-polling-unknown")
    caplog.set_level(logging.ERROR, logger=logger.name)
    health = _HealthRecorder()

    async def start_polling() -> None:
        raise RuntimeError("polling bug")

    async def pause_invalid(_error: BaseException) -> None:
        pytest.fail("unknown failure must not pause configuration")

    with pytest.raises(main.PollingSupervisorFailure) as exc:
        await main._run_polling_supervisor(
            start_polling=start_polling,
            stop_event=asyncio.Event(),
            logger=logger,
            runtime_health=health,
            pause_invalid_configuration=pause_invalid,
        )

    assert isinstance(exc.value.__cause__, RuntimeError)
    assert any(record.exc_info for record in caplog.records)
    assert health.transitions[-1] == (
        main.TgbotRuntimeStatus.FAILED,
        "polling_unknown_failure",
    )


@pytest.mark.asyncio
async def test_unexpected_polling_cancellation_is_unhealthy() -> None:
    health = _HealthRecorder()

    async def start_polling() -> None:
        raise asyncio.CancelledError

    async def pause_invalid(_error: BaseException) -> None:
        pytest.fail("unexpected cancellation is not a configuration pause")

    with pytest.raises(main.PollingSupervisorFailure):
        await main._run_polling_supervisor(
            start_polling=start_polling,
            stop_event=asyncio.Event(),
            logger=logging.getLogger("test-polling-cancel"),
            runtime_health=health,
            pause_invalid_configuration=pause_invalid,
        )

    assert health.transitions[-1] == (
        main.TgbotRuntimeStatus.FAILED,
        "polling_unknown_failure",
    )


@pytest.mark.asyncio
async def test_main_pauses_before_api_client_without_shared_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[tuple[str, int, object]] = []
    health = _HealthRecorder()

    async def fake_pause(
        _logger: logging.Logger,
        diagnostic: str,
        *,
        level: int,
        recovery_check=None,
        refresh_interval_sec: float = main._PAUSED_CONFIG_REFRESH_INTERVAL_SEC,
        **_kwargs: object,
    ) -> None:
        del refresh_interval_sec
        pauses.append((diagnostic, level, recovery_check))

    monkeypatch.setattr(main.settings, "telegram_bot_shared_secret", "")
    monkeypatch.setattr(main, "_pause_until_restart_or_stop", fake_pause)
    monkeypatch.setattr(
        main,
        "LumenApi",
        lambda: pytest.fail("LumenApi must not be constructed without shared secret"),
    )

    await main._amain(health)

    assert pauses == [
        (
            "configuration error: TELEGRAM_BOT_SHARED_SECRET is empty",
            logging.ERROR,
            None,
        )
    ]
    assert health.transitions == [
        (
            main.TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
            "missing_shared_secret",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (401, 403))
async def test_main_treats_rejected_shared_secret_as_unhealthy_configuration(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    api = _FakeApi()
    health = _HealthRecorder()
    pauses: list[tuple[str, int]] = []

    class RejectingProxyManager:
        def __init__(self, received_api: _FakeApi) -> None:
            assert received_api is api

        async def initial_load(self) -> dict[str, object]:
            raise main.ApiError(
                "bot_unauthorized",
                "invalid bot token",
                status,
            )

    async def fake_pause(
        _logger: logging.Logger,
        diagnostic: str,
        *,
        level: int,
        recovery_check=None,
        refresh_interval_sec: float = main._PAUSED_CONFIG_REFRESH_INTERVAL_SEC,
        **_kwargs: object,
    ) -> None:
        del recovery_check, refresh_interval_sec
        pauses.append((diagnostic, level))

    monkeypatch.setattr(main.settings, "telegram_bot_shared_secret", "wrong-secret")
    monkeypatch.setattr(main.settings, "telegram_bot_token", "123:test-token")
    monkeypatch.setattr(main, "LumenApi", lambda: api)
    monkeypatch.setattr(main, "ProxyManager", RejectingProxyManager)
    monkeypatch.setattr(main, "_pause_until_restart_or_stop", fake_pause)

    await main._amain(health)

    assert api.closed is True
    assert pauses == [
        (
            "configuration error: TELEGRAM_BOT_SHARED_SECRET was rejected by lumen-api",
            logging.ERROR,
        )
    ]
    assert health.transitions == [
        (
            main.TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
            "shared_secret_rejected",
        )
    ]


class _FakeApi:
    def __init__(
        self,
        *,
        access_configs: list[dict[str, object]] | None = None,
        runtime_configs: list[dict[str, object]] | None = None,
    ) -> None:
        self.closed = False
        self.access_configs = list(access_configs or [])
        self.runtime_configs = list(runtime_configs or [])

    async def aclose(self) -> None:
        self.closed = True

    async def get_access_config(self) -> dict[str, object]:
        return self.access_configs.pop(0)

    async def get_runtime_config(
        self,
        avoid: list[str] | None = None,
    ) -> dict[str, object]:
        assert avoid == []
        return self.runtime_configs.pop(0)


@pytest.mark.asyncio
async def test_main_closes_api_when_runtime_bootstrap_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi()

    class CrashingProxyManager:
        def __init__(self, received_api: _FakeApi) -> None:
            assert received_api is api

        async def initial_load(self) -> dict[str, object]:
            raise RuntimeError("unexpected bootstrap failure")

    monkeypatch.setattr(main.settings, "telegram_bot_shared_secret", "s" * 32)
    monkeypatch.setattr(main, "LumenApi", lambda: api)
    monkeypatch.setattr(main, "ProxyManager", CrashingProxyManager)

    with pytest.raises(RuntimeError, match="unexpected bootstrap failure"):
        await main._amain(_HealthRecorder())

    assert api.closed is True


@pytest.mark.asyncio
async def test_main_closes_bot_and_fsm_when_dispatcher_setup_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi()

    class FakeProxyManager:
        current_name = None

        def __init__(self, received_api: _FakeApi) -> None:
            assert received_api is api

        async def initial_load(self) -> dict[str, object]:
            return {
                "bot_enabled": True,
                "bot_token": "test-token",
                "proxy": {},
            }

    class FakeSession:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeBot:
        instance = None

        def __init__(self, **_kwargs: object) -> None:
            self.session = FakeSession()
            FakeBot.instance = self

    class FakeRedis:
        closed = False

        async def ping(self) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True

    redis = FakeRedis()

    def crash_dispatcher(**_kwargs: object) -> None:
        raise RuntimeError("dispatcher setup failed")

    monkeypatch.setattr(main.settings, "telegram_bot_shared_secret", "s" * 32)
    monkeypatch.setattr(main.settings, "telegram_bot_token", "")
    monkeypatch.setattr(main.settings, "telegram_proxy_url", "")
    monkeypatch.setattr(main, "LumenApi", lambda: api)
    monkeypatch.setattr(main, "ProxyManager", FakeProxyManager)
    monkeypatch.setattr(main, "Bot", FakeBot)
    monkeypatch.setattr(main, "Dispatcher", crash_dispatcher)
    monkeypatch.setattr(
        main.aioredis,
        "from_url",
        lambda *_args, **_kwargs: redis,
    )

    with pytest.raises(RuntimeError, match="dispatcher setup failed"):
        await main._amain(_HealthRecorder())

    assert FakeBot.instance is not None
    assert FakeBot.instance.session.closed is True
    assert redis.closed is True
    assert api.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access_config", "runtime_config", "env_token", "expected", "runtime_reads"),
    [
        (
            {"bot_enabled": False},
            {"bot_enabled": False, "bot_token": "db-token"},
            "",
            False,
            0,
        ),
        (
            {"bot_enabled": True},
            {"bot_enabled": True, "bot_token": ""},
            "",
            False,
            1,
        ),
        (
            {"bot_enabled": True},
            {"bot_enabled": True, "bot_token": "db-token"},
            "",
            True,
            1,
        ),
        (
            {"bot_enabled": True},
            {"bot_enabled": True, "bot_token": ""},
            "env-token",
            True,
            0,
        ),
    ],
)
async def test_runtime_config_runnable_requires_enabled_bot_and_token(
    monkeypatch: pytest.MonkeyPatch,
    access_config: dict[str, object],
    runtime_config: dict[str, object],
    env_token: str,
    expected: bool,
    runtime_reads: int,
) -> None:
    api = _FakeApi(
        access_configs=[access_config],
        runtime_configs=[runtime_config],
    )
    monkeypatch.setattr(main.settings, "telegram_bot_token", env_token)

    assert await main._runtime_config_is_runnable(api) is expected
    assert len(api.runtime_configs) == 1 - runtime_reads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_config", "expected_diagnostic", "expected_level"),
    [
        (
            {"bot_enabled": False, "bot_token": "db-token"},
            "telegram.bot_enabled=0 in runtime configuration",
            logging.INFO,
        ),
        (
            {"bot_enabled": True, "bot_token": ""},
            "configuration error: bot token is empty in runtime configuration and "
            "TELEGRAM_BOT_TOKEN",
            logging.ERROR,
        ),
    ],
)
async def test_main_pauses_for_non_runnable_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
    runtime_config: dict[str, object],
    expected_diagnostic: str,
    expected_level: int,
) -> None:
    api = _FakeApi(
        access_configs=[{"bot_enabled": True}],
        runtime_configs=[{"bot_enabled": True, "bot_token": "recovered-token"}],
    )
    pauses: list[tuple[str, int, bool]] = []
    health = _HealthRecorder()

    class FakeProxyManager:
        def __init__(self, received_api: _FakeApi) -> None:
            assert received_api is api

        async def initial_load(self) -> dict[str, object]:
            return runtime_config

    async def fake_pause(
        _logger: logging.Logger,
        diagnostic: str,
        *,
        level: int,
        recovery_check=None,
        refresh_interval_sec: float = main._PAUSED_CONFIG_REFRESH_INTERVAL_SEC,
        **_kwargs: object,
    ) -> None:
        del refresh_interval_sec
        assert recovery_check is not None
        pauses.append((diagnostic, level, await recovery_check()))

    monkeypatch.setattr(main.settings, "telegram_bot_shared_secret", "s" * 32)
    monkeypatch.setattr(main.settings, "telegram_bot_token", "")
    monkeypatch.setattr(main, "LumenApi", lambda: api)
    monkeypatch.setattr(main, "ProxyManager", FakeProxyManager)
    monkeypatch.setattr(main, "_pause_until_restart_or_stop", fake_pause)

    await main._amain(health)

    assert api.closed is True
    assert pauses == [(expected_diagnostic, expected_level, True)]
    expected_health = (
        (
            main.TgbotRuntimeStatus.PAUSED_INTENTIONAL,
            "runtime_disabled",
        )
        if runtime_config["bot_enabled"] is False
        else (
            main.TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
            "empty_bot_token",
        )
    )
    assert health.transitions == [expected_health]


def test_systemd_restart_contract_starts_python_without_config_guard() -> None:
    service = (
        Path(__file__).resolve().parents[3] / "deploy/systemd/lumen-tgbot.service"
    ).read_text(encoding="utf-8")
    directives = {
        line.strip()
        for line in service.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "ExecStart=/opt/lumen/current/.venv/bin/python -m app.main" in directives
    assert "Restart=always" in directives
    assert not any(line.startswith("ExecStartPre=") for line in directives)
    assert not any(line.startswith("RestartPreventExitStatus=") for line in directives)
