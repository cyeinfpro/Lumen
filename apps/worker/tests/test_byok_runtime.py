from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import byok_runtime
from app.upstream import UpstreamError
from lumen_core.constants import GenerationErrorCode as EC


class _Result:
    """Mimic SQLAlchemy Result for both ``.first()`` and ``.scalar_one_or_none``."""

    def __init__(self, value: Any = None) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Db:
    """Async session stub returning queued ``_Result`` objects in order.

    ``get`` returns the value of ``raw_credential`` (used to drive the second
    branch in ``resolve_user_credential_runtime`` that re-fetches the row to
    distinguish rate-limited vs not-active).
    """

    def __init__(
        self,
        results: list[_Result] | None = None,
        raw_credential: Any | None = None,
    ) -> None:
        self.results = list(results or [])
        self.raw_credential = raw_credential

    async def execute(self, _statement: Any) -> _Result:
        return self.results.pop(0) if self.results else _Result()

    async def get(self, _model: Any, _credential_id: str) -> Any:
        return self.raw_credential


# ---------------------------------------------------------------------------
# classify / mapping helpers — pure function tests
# ---------------------------------------------------------------------------


def test_classify_user_credential_error_handles_auth_and_rate_limit() -> None:
    invalid, code = byok_runtime.classify_user_credential_error(
        UpstreamError("unauthorized", status_code=401),
    )
    assert invalid is True
    assert code == "invalid_api_key"

    limited, limit_code = byok_runtime.classify_user_credential_error(
        UpstreamError(
            "quota exceeded",
            status_code=429,
            error_code=EC.RATE_LIMIT_ERROR.value,
        ),
    )
    assert limited is True
    assert limit_code == "key_rate_limited"

    unrelated, unrelated_code = byok_runtime.classify_user_credential_error(
        RuntimeError("network failed"),
    )
    assert unrelated is False
    assert unrelated_code is None


def test_classify_user_credential_error_does_not_use_message_when_status_known() -> (
    None
):
    """Status 200 + 'rate limit' in message must NOT escalate (review #21).

    Heuristic message matching only kicks in when status_code AND error_code
    are both unknown — otherwise the upstream classification wins.
    """
    body_only, body_code = byok_runtime.classify_user_credential_error(
        UpstreamError("server hint: rate limit nearby", status_code=200),
    )
    assert body_only is False
    assert body_code is None

    # message heuristic still works for raw exceptions without status.
    plain, plain_code = byok_runtime.classify_user_credential_error(
        RuntimeError("rate limit hit"),
    )
    assert plain is True
    assert plain_code == "key_rate_limited"


def test_classify_user_credential_error_recognizes_decrypt_mismatch() -> None:
    terminal, code = byok_runtime.classify_user_credential_error(
        UpstreamError(
            "cannot decrypt",
            status_code=500,
            error_code="byok_master_secret_mismatch",
        ),
    )
    assert terminal is True
    assert code == "byok_master_secret_mismatch"


def test_byok_error_to_generation_code_maps_user_key_failures() -> None:
    assert byok_runtime.byok_error_to_generation_code("invalid_api_key") == (
        EC.UPSTREAM_AUTH_ERROR.value
    )
    assert byok_runtime.byok_error_to_generation_code("key_rate_limited") == (
        EC.UPSTREAM_RATE_LIMITED.value
    )
    assert (
        byok_runtime.byok_error_to_generation_code("other") == EC.UPSTREAM_ERROR.value
    )
    assert (
        byok_runtime.byok_error_to_generation_code("byok_master_secret_mismatch")
        == EC.UPSTREAM_ERROR.value
    )


def test_is_byok_provider_recognizes_user_prefix() -> None:
    """Admin pool guard: only ``user:*`` runtimes count as BYOK."""
    assert byok_runtime.is_byok_provider(SimpleNamespace(name="user:openai:abc"))
    assert byok_runtime._is_byok_provider(SimpleNamespace(name="user:openai:abc"))
    assert not byok_runtime.is_byok_provider(SimpleNamespace(name="openai-paid"))
    assert not byok_runtime.is_byok_provider(SimpleNamespace(name=""))
    assert not byok_runtime.is_byok_provider(SimpleNamespace())


def test_base_url_validation_cache_clear_uses_shared_lock() -> None:
    cache_key = ("https://upstream.example", False)
    with byok_runtime._BASE_URL_VALIDATION_CACHE_LOCK:
        byok_runtime._BASE_URL_VALIDATION_CACHE[cache_key] = (999999.0, "cached")

    byok_runtime.clear_base_url_validation_cache()

    assert byok_runtime._BASE_URL_VALIDATION_CACHE == {}
    assert hasattr(byok_runtime._BASE_URL_VALIDATION_CACHE_LOCK, "acquire")


# ---------------------------------------------------------------------------
# resolve_user_credential_runtime — happy path + new branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_user_credential_runtime_builds_resolved_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cryptography")
    from lumen_core.byok import encrypt_api_key

    secret = "x" * 32
    monkeypatch.setattr(byok_runtime.settings, "byok_api_key_master_secret", secret)
    credential = SimpleNamespace(
        id="cred-1234567890abcdef",
        key_ciphertext=encrypt_api_key("sk-user-runtime", secret),
    )
    supplier = SimpleNamespace(
        slug="openai",
        base_url="https://upstream.example",
        proxy_name=None,
        image_concurrency_per_key=3,
        purposes=["chat"],
        capabilities_jsonb={
            "image_jobs_enabled": True,
            "image_jobs_endpoint": "responses",
        },
    )
    db = _Db([_Result((credential, supplier))])

    provider = await byok_runtime.resolve_user_credential_runtime(
        db,  # type: ignore[arg-type]
        credential.id,
    )

    assert provider.name == "user:openai:cred12345678"
    assert provider.base_url == "https://upstream.example"
    assert provider.api_key == "sk-user-runtime"
    assert provider.image_concurrency == 3
    assert provider.purposes == ("chat",)
    # capabilities_jsonb 应当被透传到 ResolvedProvider，而不再被一刀切成 False/auto。
    assert provider.image_jobs_enabled is True
    assert provider.image_jobs_endpoint == "responses"


@pytest.mark.asyncio
async def test_resolve_user_credential_runtime_parses_string_false_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cryptography")
    from lumen_core.byok import encrypt_api_key

    secret = "x" * 32
    monkeypatch.setattr(byok_runtime.settings, "byok_api_key_master_secret", secret)
    credential = SimpleNamespace(
        id="cred-string-false",
        key_ciphertext=encrypt_api_key("sk-user-runtime", secret),
    )
    supplier = SimpleNamespace(
        slug="openai",
        base_url="https://upstream.example",
        proxy_name=None,
        image_concurrency_per_key=1,
        purposes=["image"],
        capabilities_jsonb={
            "image_jobs_enabled": "false",
            "image_jobs_endpoint": "responses",
        },
    )
    db = _Db([_Result((credential, supplier))])

    provider = await byok_runtime.resolve_user_credential_runtime(
        db,  # type: ignore[arg-type]
        credential.id,
    )

    assert provider.image_jobs_enabled is False
    assert provider.image_jobs_endpoint == "responses"


@pytest.mark.asyncio
async def test_resolve_user_credential_runtime_disables_invalid_boolean_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cryptography")
    from lumen_core.byok import encrypt_api_key

    secret = "x" * 32
    monkeypatch.setattr(byok_runtime.settings, "byok_api_key_master_secret", secret)
    credential = SimpleNamespace(
        id="cred-invalid-capability",
        key_ciphertext=encrypt_api_key("sk-user-runtime", secret),
    )
    supplier = SimpleNamespace(
        slug="openai",
        base_url="https://upstream.example",
        proxy_name=None,
        image_concurrency_per_key=1,
        purposes=["image"],
        capabilities_jsonb={"image_jobs_enabled": "sometimes"},
    )
    db = _Db([_Result((credential, supplier))])

    provider = await byok_runtime.resolve_user_credential_runtime(
        db,  # type: ignore[arg-type]
        credential.id,
    )

    assert provider.image_jobs_enabled is False


@pytest.mark.asyncio
async def test_supplier_base_url_validation_is_ttl_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_assert_public_http_target(raw: str, **kwargs: Any) -> str:
        calls.append((raw, kwargs))
        return raw.rstrip("/")

    byok_runtime.clear_base_url_validation_cache()
    monkeypatch.setattr(
        byok_runtime,
        "assert_public_http_target",
        fake_assert_public_http_target,
    )

    first = await byok_runtime._validate_supplier_base_url(
        "https://upstream.example/v1/"
    )
    second = await byok_runtime._validate_supplier_base_url(
        "https://upstream.example/v1/"
    )

    assert first == "https://upstream.example/v1"
    assert second == first
    assert len(calls) == 1
    byok_runtime.clear_base_url_validation_cache()


@pytest.mark.asyncio
async def test_resolve_user_credential_runtime_decrypt_mismatch_does_not_mark_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong master_secret must raise byok_master_secret_mismatch and NOT touch credential.status."""
    pytest.importorskip("cryptography")
    from lumen_core.byok import encrypt_api_key

    encrypt_secret = "a" * 32
    runtime_secret = "b" * 32  # different secret — decrypt will fail
    monkeypatch.setattr(
        byok_runtime.settings, "byok_api_key_master_secret", runtime_secret
    )
    credential = SimpleNamespace(
        id="cred-decrypt-fail",
        key_ciphertext=encrypt_api_key("sk-user-runtime", encrypt_secret),
    )
    supplier = SimpleNamespace(
        slug="openai",
        base_url="https://upstream.example",
        proxy_name=None,
        image_concurrency_per_key=1,
        purposes=["chat"],
        capabilities_jsonb={},
    )
    db = _Db([_Result((credential, supplier))])

    with pytest.raises(UpstreamError) as exc_info:
        await byok_runtime.resolve_user_credential_runtime(
            db,  # type: ignore[arg-type]
            credential.id,
        )
    assert exc_info.value.error_code == "byok_master_secret_mismatch"

    # record_user_credential_runtime_error must NOT change credential.status
    # for a decrypt mismatch (deployment error, not user error).
    captured: dict[str, Any] = {}

    class _CommitSession:
        def __init__(self) -> None:
            self.row = SimpleNamespace(
                status="active",
                last_failed_at=None,
                last_error_code=None,
                rate_limited_until=None,
            )

        async def get(self, _model: Any, _id: str) -> Any:
            return self.row

        async def commit(self) -> None:
            captured["committed"] = True

        async def rollback(self) -> None:
            captured["rolled_back"] = True

        async def __aenter__(self) -> "_CommitSession":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    session = _CommitSession()

    class _SessionFactory:
        def __call__(self) -> _CommitSession:
            return session

    import app.db as worker_db

    monkeypatch.setattr(worker_db, "SessionLocal", _SessionFactory())

    await byok_runtime.record_user_credential_runtime_error(
        credential.id, exc_info.value
    )

    # decrypt 失败路径直接返回，不应触发任何 commit / status 改写。
    assert "committed" not in captured
    assert session.row.status == "active"


@pytest.mark.asyncio
async def test_resolve_user_credential_runtime_rate_limited_in_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rate_limited_until 在未来 → SELECT 拿不到行 → 第二次 get 拿原始行 → 抛 key_rate_limited."""
    pytest.importorskip("cryptography")
    monkeypatch.setattr(byok_runtime.settings, "byok_api_key_master_secret", "y" * 32)
    raw_credential = SimpleNamespace(
        id="cred-rate-limited",
        rate_limited_until=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    db = _Db(results=[_Result(None)], raw_credential=raw_credential)

    with pytest.raises(UpstreamError) as exc_info:
        await byok_runtime.resolve_user_credential_runtime(
            db,  # type: ignore[arg-type]
            raw_credential.id,
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.error_code == EC.UPSTREAM_RATE_LIMITED.value
    # retry_after 通过 payload 传递（UpstreamError 没有专属字段），秒级正整数。
    assert exc_info.value.payload.get("retry_after", 0) > 0


@pytest.mark.asyncio
async def test_resolve_user_credential_runtime_inactive_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rate_limited_until 为 None 但 select 仍空（status invalid / supplier disabled） → 403."""
    pytest.importorskip("cryptography")
    monkeypatch.setattr(byok_runtime.settings, "byok_api_key_master_secret", "z" * 32)
    db = _Db(results=[_Result(None)], raw_credential=None)
    with pytest.raises(UpstreamError) as exc_info:
        await byok_runtime.resolve_user_credential_runtime(
            db,  # type: ignore[arg-type]
            "cred-missing",
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == EC.UPSTREAM_AUTH_ERROR.value


# ---------------------------------------------------------------------------
# Admin-pool isolation: BYOK provider must NOT touch pool.report_*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_byok_provider_drives_admin_pool_skip() -> None:
    """Sanity: the helper used by upstream._is_byok_provider matches the runtime helper.

    The full integration of guard-skipping admin pool calls lives in the
    failover loops; here we just lock in the prefix contract.
    """
    pool = MagicMock()
    pool.report_image_rate_limited = MagicMock()
    pool.report_image_failure = MagicMock()
    limiter = AsyncMock()

    byok_provider = SimpleNamespace(name="user:openai:abcdef")
    admin_provider = SimpleNamespace(name="openai-shared")

    # Simulate the inline guard pattern used in upstream.py.
    if not byok_runtime.is_byok_provider(byok_provider):  # pragma: no cover - guarded
        pool.report_image_failure(byok_provider.name)
        await limiter.record_image_call(None, byok_provider.name)
    if not byok_runtime.is_byok_provider(admin_provider):
        pool.report_image_failure(admin_provider.name)
        await limiter.record_image_call(None, admin_provider.name)

    pool.report_image_failure.assert_called_once_with("openai-shared")
    limiter.record_image_call.assert_awaited_once_with(None, "openai-shared")
