from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from app.upstream_parts.generated_payload import (
    GeneratedPayload,
    InlineImageBytes,
    RemoteImageResult,
    StagedImageFile,
    cleanup_owned_generated_payload,
    coerce_generated_payload,
    decode_inline_image_base64,
    generated_payload_size,
    generated_payload_to_base64,
    materialize_generated_payload,
    read_generated_payload_bytes,
    validate_remote_image_url,
)


def test_generated_payload_union_accepts_each_public_variant() -> None:
    payloads: tuple[GeneratedPayload, ...] = (
        InlineImageBytes(b"inline"),
        StagedImageFile(
            path=Path("/tmp/generated.part"),
            size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            owned=False,
        ),
        RemoteImageResult(
            url="https://cdn.example/generated.png",
            expected_mime=None,
        ),
    )

    assert tuple(type(payload) for payload in payloads) == (
        InlineImageBytes,
        StagedImageFile,
        RemoteImageResult,
    )


def test_legacy_bytes_are_wrapped_without_copying() -> None:
    raw = b"generated-image"

    payload = coerce_generated_payload(raw)

    assert payload == InlineImageBytes(raw)
    assert payload.data is raw


def test_materialize_inline_payload_reuses_bytes_without_base64() -> None:
    raw = b"direct-bytes"

    assert materialize_generated_payload(InlineImageBytes(raw)) is raw


def test_payload_size_is_known_only_after_remote_download() -> None:
    assert generated_payload_size(InlineImageBytes(b"123")) == 3
    assert (
        generated_payload_size(
            StagedImageFile(
                path=Path("/tmp/generated.part"),
                size=7,
                sha256=hashlib.sha256(b"").hexdigest(),
                owned=False,
            )
        )
        == 7
    )
    assert (
        generated_payload_size(
            RemoteImageResult(
                url="https://cdn.example/generated.png",
                expected_mime=None,
            )
        )
        is None
    )


def test_remote_url_defaults_to_https_with_explicit_http_allowlist() -> None:
    validate_remote_image_url("https://cdn.example/generated.png")
    validate_remote_image_url(
        "http://image-sidecar/generated.png",
        allowed_http_hosts=("IMAGE-SIDECAR",),
    )

    with pytest.raises(ValueError, match="HTTPS"):
        validate_remote_image_url("http://cdn.example/generated.png")


def test_inline_base64_is_decoded_to_bytes_with_data_url_and_whitespace() -> None:
    encoded = base64.b64encode(b"decoded-image").decode("ascii")

    payload = decode_inline_image_base64(
        f" data:image/png;base64,\n{encoded[:8]} \n{encoded[8:]} "
    )

    assert payload == InlineImageBytes(b"decoded-image")


@pytest.mark.parametrize("raw", [b"a", b"ab"])
def test_inline_base64_repairs_missing_trailing_padding(raw: bytes) -> None:
    encoded = base64.b64encode(raw).decode("ascii").rstrip("=")

    assert decode_inline_image_base64(encoded) == InlineImageBytes(raw)


def test_inline_base64_rejects_unrecoverable_length_and_invalid_characters() -> None:
    with pytest.raises(ValueError, match="invalid base64"):
        decode_inline_image_base64("A")

    with pytest.raises(ValueError, match="invalid base64"):
        decode_inline_image_base64("YWJ!")


def test_inline_base64_rejects_estimated_output_over_limit() -> None:
    encoded = base64.b64encode(b"12345").decode("ascii")

    with pytest.raises(ValueError, match="size limit"):
        decode_inline_image_base64(encoded, max_bytes=4)


def test_unpadded_inline_base64_still_enforces_decoded_size_limit() -> None:
    encoded = base64.b64encode(b"12345").decode("ascii").rstrip("=")

    with pytest.raises(ValueError, match="size limit"):
        decode_inline_image_base64(encoded, max_bytes=4)


def test_staged_payload_materialization_verifies_size_and_hash(
    tmp_path: Path,
) -> None:
    raw = b"staged-image"
    path = tmp_path / "generated.part"
    path.write_bytes(raw)
    payload = StagedImageFile(
        path=path,
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        owned=False,
    )

    assert read_generated_payload_bytes(payload) == raw
    assert generated_payload_to_base64(payload) == base64.b64encode(raw).decode("ascii")


def test_staged_payload_materialization_rejects_metadata_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "generated.part"
    path.write_bytes(b"changed")
    payload = StagedImageFile(
        path=path,
        size=7,
        sha256=hashlib.sha256(b"original").hexdigest(),
        owned=False,
    )

    with pytest.raises(ValueError, match="sha256"):
        read_generated_payload_bytes(payload)


@pytest.mark.parametrize("owned", [True, False])
def test_cleanup_deletes_only_owned_staging(
    tmp_path: Path,
    *,
    owned: bool,
) -> None:
    path = tmp_path / f"generated-{owned}.part"
    raw = b"staged"
    path.write_bytes(raw)
    payload = StagedImageFile(
        path=path,
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        owned=owned,
    )

    cleanup_owned_generated_payload(payload)

    assert path.exists() is (not owned)


def test_remote_payload_requires_streaming_before_materialization() -> None:
    payload = RemoteImageResult(
        url="https://cdn.example/generated.png",
        expected_mime="image/png",
    )

    with pytest.raises(ValueError, match="must be streamed"):
        read_generated_payload_bytes(payload)
