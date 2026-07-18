from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pytest

from lumen_core import byok


def _patch_randbelow(
    monkeypatch: pytest.MonkeyPatch,
    values: list[int],
) -> None:
    remaining: Iterator[int] = iter(values)

    def fake_randbelow(_upper: int) -> int:
        return next(remaining)

    monkeypatch.setattr(byok.secrets, "randbelow", fake_randbelow)


@pytest.mark.parametrize(
    ("rand_values", "expression", "expected", "operator", "operands"),
    [
        # multiplication, two-digit × two-digit branch (sub-branch picker = 0)
        ([0, 0, 0, 1], "11 * 12", 132, "*", (11, 12)),
        # multiplication, one-digit × three-digit branch (sub-branch picker = 1)
        ([0, 1, 0, 0], "2 * 100", 200, "*", (2, 100)),
        ([1, 0, 0], "1000 + 100", 1100, "+", (1000, 100)),
        # subtraction now guarantees b >= 1
        ([2, 0, 0], "1000 - 1", 999, "-", (1000, 1)),
    ],
)
def test_generate_arithmetic_challenge_builds_expected_answer(
    monkeypatch: pytest.MonkeyPatch,
    rand_values: list[int],
    expression: str,
    expected: int,
    operator: str,
    operands: tuple[int, int],
) -> None:
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    _patch_randbelow(monkeypatch, rand_values)

    challenge = byok.generate_arithmetic_challenge(now=now)

    assert challenge.expression == expression
    assert challenge.expected == expected
    assert challenge.operator == operator
    assert challenge.operands == operands
    assert challenge.as_json()["created_at"] == now.isoformat()


@pytest.mark.parametrize(
    ("text", "expected", "matches"),
    [
        ("42", 42, True),
        ("  +4,200  ", 4200, True),
        ("-1 200", -1200, True),
        ("42.", 42, False),
        ("answer: 42", 42, False),
        ("", 0, False),
    ],
)
def test_answer_matches_expected_accepts_only_plain_integers(
    text: str,
    expected: int,
    matches: bool,
) -> None:
    assert byok.answer_matches_expected(text, expected) is matches


@pytest.mark.parametrize(
    ("text", "expected", "ok"),
    [
        ("123", 123, True),
        ("1,234", 1234, True),
        ("1234", 1234, True),
        ("-12", -12, True),
        ("0", 0, True),
        # Bug coverage: multi-separator forms must be rejected.
        ("1,2,3", 123, False),
        ("1 2 3", 123, False),
        ("12 34", 1234, False),  # not a standard 1-3 + group-of-3 form
        ("1,23", 123, False),  # group must be exactly 3 digits
        ("abc", 0, False),
        ("12.5", 12, False),
        ("--1", -1, False),
        ("", 0, False),
    ],
)
def test_answer_matches_expected_strict(
    text: str,
    expected: int,
    ok: bool,
) -> None:
    assert byok.answer_matches_expected(text, expected) is ok


def test_extract_response_output_text_handles_responses_shapes() -> None:
    assert byok.extract_response_output_text({"output_text": "123"}) == "123"
    assert (
        byok.extract_response_output_text(
            {
                "output": [
                    {"content": [{"type": "output_text", "text": "12"}]},
                    {"content": [{"type": "output_text", "output_text": "3"}]},
                ]
            }
        )
        == "123"
    )


def test_build_provider_probe_request_preserves_fixed_payload() -> None:
    assert byok.build_provider_probe_request() == {
        "model": "gpt-5.4-mini",
        "instructions": (
            "You are a precise calculator. Return only the final integer."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "What is 99 times 99? Reply with only the integer result, "
                            "no words, no explanation."
                        ),
                    }
                ],
            }
        ],
        "stream": False,
        "store": False,
    }


def test_extract_sse_output_text_handles_delta_and_nested_responses() -> None:
    raw = "\n\n".join(
        [
            'data: {"delta":"12"}',
            'data: {"response":{"output_text":"3"}}',
            "data: [DONE]",
        ]
    )

    assert byok.extract_sse_output_text(raw) == "123"


def test_extract_sse_prefers_output_text_done_over_streamed_deltas() -> None:
    raw = "\n\n".join(
        [
            (
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"12"}'
            ),
            (
                "event: response.output_text.done\n"
                'data: {"type":"response.output_text.done","text":"123"}'
            ),
        ]
    )

    assert byok.extract_sse_output_text(raw) == "123"


def test_extract_sse_prefers_completed_response_over_intermediate_snapshots() -> None:
    raw = "\n\n".join(
        [
            (
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"12"}'
            ),
            (
                "event: response.output_text.done\n"
                'data: {"type":"response.output_text.done","text":"123"}'
            ),
            (
                "event: response.completed\n"
                'data: {"type":"response.completed","response":'
                '{"output_text":"123"}}'
            ),
        ]
    )

    assert byok.extract_sse_output_text(raw) == "123"


def test_validate_api_key_shape_strips_and_caps_length() -> None:
    assert byok.validate_api_key_shape("  sk-test  ") == "sk-test"
    with pytest.raises(ValueError):
        byok.validate_api_key_shape(" ")
    with pytest.raises(ValueError):
        byok.validate_api_key_shape("x" * (byok.BYOK_MAX_API_KEY_LEN + 1))


def test_encrypt_api_key_roundtrips_when_crypto_dependency_is_present() -> None:
    pytest.importorskip("cryptography")
    secret = "x" * 32

    ciphertext = byok.encrypt_api_key("sk-live-test", secret)

    assert ciphertext.startswith(f"{byok.BYOK_ENCRYPTION_VERSION}:")
    assert byok.decrypt_api_key(ciphertext, secret) == "sk-live-test"
    assert byok.hash_api_key("sk-live-test", secret) == byok.hash_api_key(
        "sk-live-test",
        secret,
    )


def test_crypto_helpers_require_a_strong_master_secret() -> None:
    pytest.importorskip("cryptography")
    with pytest.raises(byok.ByokCryptoError):
        byok.encrypt_api_key("sk-test", "too-short")


def test_hash_outputs_carry_version_prefix() -> None:
    pytest.importorskip("cryptography")
    secret = "x" * 32
    assert byok.hash_api_key("sk-x", secret).startswith(
        f"{byok.BYOK_ENCRYPTION_VERSION}:"
    )
    assert byok.hash_verification_token("tok", secret).startswith(
        f"{byok.BYOK_ENCRYPTION_VERSION}:"
    )


def test_challenges_are_diverse() -> None:
    seen: set[str] = set()
    for _ in range(100):
        c = byok.generate_arithmetic_challenge()
        seen.add(c.expression)
    assert len(seen) >= 50, f"too few distinct challenges: {len(seen)}"


def test_subtraction_challenge_no_zero_subtrahend() -> None:
    for _ in range(200):
        c = byok.generate_arithmetic_challenge()
        if c.operator == "-":
            assert c.operands[1] >= 1, c


def test_decrypt_rejects_tampered_ciphertext() -> None:
    pytest.importorskip("cryptography")
    secret = "x" * 32
    ct = byok.encrypt_api_key("sk-test", secret)
    tampered = ct[:-2] + ("AA" if ct[-2:] != "AA" else "BB")
    with pytest.raises(byok.ByokCryptoError):
        byok.decrypt_api_key(tampered, secret)


def test_decrypt_rejects_wrong_secret() -> None:
    pytest.importorskip("cryptography")
    ct = byok.encrypt_api_key("sk-test", "x" * 32)
    with pytest.raises(byok.ByokCryptoError):
        byok.decrypt_api_key(ct, "y" * 32)


def test_decrypt_rejects_unknown_version() -> None:
    with pytest.raises(byok.ByokCryptoError):
        byok.decrypt_api_key("v9:abc:def", "x" * 32)


def test_decrypt_rejects_malformed() -> None:
    with pytest.raises(byok.ByokCryptoError):
        byok.decrypt_api_key("not-a-cipher", "x" * 32)


def test_extract_sse_handles_no_trailing_blank() -> None:
    raw = 'data: {"output_text": "42"}'
    assert byok.extract_sse_output_text(raw) == "42"


def test_extract_sse_tolerates_extra_spaces_after_data_prefix() -> None:
    raw = 'data:    {"output_text": "ok"}\n\n'
    assert byok.extract_sse_output_text(raw) == "ok"


def test_extract_sse_ignores_done_marker() -> None:
    raw = "data: [DONE]\n\n"
    assert byok.extract_sse_output_text(raw) == ""


def test_extract_sse_returns_empty_for_empty_buffer() -> None:
    assert byok.extract_sse_output_text("") == ""
