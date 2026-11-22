"""Server-only adapter for Model Studio Qwen-Audio-TTS, not a generic URL fetcher."""

from __future__ import annotations

import asyncio
import base64
import email.utils
import io
import ipaddress
import math
import re
import socket
import wave
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

MODEL = "qwen-audio-3.1-tts-plus"
REGIONS = {
    "beijing": "cn-beijing",
    "cn-beijing": "cn-beijing",
    "singapore": "ap-southeast-1",
    "ap-southeast-1": "ap-southeast-1",
}
# Fixed native DashScope hosts for the default workspace. Named workspaces
# use their MAAS hostname; callers cannot choose a hostname or custom URL.
DEFAULT_REGIONAL_HOSTS = {
    "cn-beijing": "dashscope.aliyuncs.com",
    "ap-southeast-1": "dashscope-intl.aliyuncs.com",
}
OUTPUT_HOST = re.compile(
    r"dashscope-result(?:-[a-z0-9-]{1,30})?\.oss-(?:cn-beijing|ap-southeast-1)"
    r"\.aliyuncs\.com\Z"
)
MAX_SAMPLE_BYTES = 10_000_000
MAX_RESULT_BYTES = 16_000_000
MAX_RESPONSE_BYTES = 256_000
SAMPLE_RATE = 24_000
REQUEST_TIMEOUT = 45.0
DOWNLOAD_TIMEOUT = 25.0


class SpeechProviderError(RuntimeError):
    """Never includes provider error body, credentials or signed URLs."""

    def __init__(self, category: str, code: str, retry_after: int | None = None):
        super().__init__(f"aliyun_speech:{code}")
        self.category = category
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class SpeechReceipt:
    audio_url: str = field(repr=False)
    request_id: str
    characters: int
    expires_at: int | None = None


def _retry_after(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        if raw.isascii() and raw.isdigit() and len(raw) < 12:
            return min(int(raw), 300)
        target = email.utils.parsedate_to_datetime(raw)
        if target.tzinfo is None:
            return None
        return max(0, min(math.ceil((target - datetime.now(timezone.utc)).total_seconds()), 300))
    except (TypeError, ValueError, OverflowError):
        return None


def _output_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or not OUTPUT_HOST.fullmatch(host)
            or parsed.netloc != host
            or not parsed.path.startswith("/")
            or parsed.fragment
            or any(ord(c) < 33 or c == "\\" for c in value)
        ):
            raise ValueError
    except ValueError:
        raise SpeechProviderError("rejected", "untrusted_audio_url") from None
    # Some published response examples use HTTP for OSS. Upgrade on this narrow
    # allowlist only; credentials and result audio never travel over plaintext.
    return urlunsplit(parsed._replace(scheme="https")), host


async def _resolve_public(host: str) -> list[str]:
    rows = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(row[4][0] for row in rows))


def _wav_properties(wav_bytes: bytes) -> tuple[int, int, int, int]:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            channels, width, rate, frames = (
                wav.getnchannels(),
                wav.getsampwidth(),
                wav.getframerate(),
                wav.getnframes(),
            )
            if frames <= 0 or len(wav.readframes(frames)) != channels * width * frames:
                raise ValueError
            return channels, width, rate, frames
    except (EOFError, ValueError, wave.Error, OverflowError):
        raise SpeechProviderError("rejected", "invalid_wav") from None


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and (
        not declared.isascii() or not declared.isdigit() or len(declared) > 12
        or int(declared) > limit
    ):
        raise SpeechProviderError("rejected", "response_too_large")
    # Request identity and reject compressed response bodies to bound decoding.
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise SpeechProviderError("rejected", "unexpected_content_encoding")
    data = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        if len(data) + len(chunk) > limit:
            raise SpeechProviderError("rejected", "response_too_large")
        data.extend(chunk)
    return bytes(data)


class AliyunSpeechClient:
    def __init__(
        self,
        region: str,
        workspace_id: str,
        api_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        resolver: Callable[[str], Awaitable[list[str]]] = _resolve_public,
    ):
        region_host = REGIONS.get(region)
        if (
            region_host is None
            or (workspace_id != "" and not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", workspace_id,
            ))
            or not api_key
            or any(ord(c) < 33 or ord(c) >= 127 for c in api_key)
        ):
            raise SpeechProviderError("rejected", "provider_configuration")
        hostname = (
            f"{workspace_id}.{region_host}.maas.aliyuncs.com"
            if workspace_id else DEFAULT_REGIONAL_HOSTS[region_host]
        )
        self._base = f"https://{hostname}/api/v1/services/audio/tts"
        self._api_key = api_key
        self._http = http
        self._resolve = resolver

    async def _send(self, request: httpx.Request, *, limit: int, mutation: bool) -> bytes:
        import contextlib

        manager = (
            contextlib.nullcontext(self._http)
            if self._http is not None
            else httpx.AsyncClient(trust_env=False, timeout=REQUEST_TIMEOUT)
        )
        try:
            async with manager as client:
                response = await client.send(request, stream=True, follow_redirects=False, auth=None)
                try:
                    status = response.status_code
                    if status == 429:
                        raise SpeechProviderError(
                            "retryable", "rate_limit", _retry_after(response.headers.get("retry-after"))
                        )
                    if status in (408, 425) or status >= 500:
                        category = "ambiguous" if mutation else "retryable"
                        raise SpeechProviderError(category, "upstream_unavailable")
                    if status != 200:
                        # Accepted responses, redirects and conflicts provide no
                        # trustworthy proof that a POST did not take effect.
                        if mutation and (status < 400 or status == 409):
                            raise SpeechProviderError("ambiguous", "unexpected_upstream_status")
                        raise SpeechProviderError(
                            "rejected", "not_found" if status == 404 else "upstream_rejected"
                        )
                    try:
                        return await _read_limited(response, limit)
                    except SpeechProviderError as exc:
                        if mutation:
                            raise SpeechProviderError("ambiguous", exc.code) from None
                        raise
                finally:
                    await response.aclose()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise SpeechProviderError("retryable", "not_connected") from None
        except (httpx.RequestError, TimeoutError):
            category = "ambiguous" if mutation else "retryable"
            raise SpeechProviderError(category, "transport_uncertain") from None

    async def _json(
        self, route: str, model: str, fields: Mapping[str, object], *, mutation: bool,
    ) -> dict:
        import json

        request = httpx.Request(
            "POST",
            f"{self._base}/{route}",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            json={"model": model, "input": dict(fields)},
        )
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                raw = await self._send(request, limit=MAX_RESPONSE_BYTES, mutation=mutation)
        except TimeoutError:
            category = "ambiguous" if mutation else "retryable"
            raise SpeechProviderError(category, "upstream_deadline") from None

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate response key")
                result[key] = value
            return result

        try:
            payload = json.loads(raw, object_pairs_hook=unique_object)
            # Some Python builds accept much deeper nesting than others; the
            # provider's receipt schema is shallow on every supported build.
            pending: list[tuple[object, int]] = [(payload, 0)]
            while pending:
                node, depth = pending.pop()
                if depth > 32:
                    raise ValueError("provider response nesting exceeds limit")
                if isinstance(node, dict):
                    pending.extend((value, depth + 1) for value in node.values())
                elif isinstance(node, list):
                    pending.extend((value, depth + 1) for value in node)
        except (UnicodeError, ValueError, RecursionError):
            payload = None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("output"), dict)
            or payload.get("code") not in (None, "")
        ):
            category = "ambiguous" if mutation else "retryable"
            raise SpeechProviderError(category, "unexpected_response")
        return payload

    def _validate_prefix(self, prefix: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9]{1,10}", prefix):
            raise SpeechProviderError("rejected", "invalid_voice_prefix")

    def _validate_voice(self, voice_id: str, *, category: str = "rejected") -> str:
        if not isinstance(voice_id, str) or not re.fullmatch(
            re.escape(MODEL) + r"-[A-Za-z0-9]{1,10}-[A-Za-z0-9_-]{1,120}", voice_id,
        ):
            raise SpeechProviderError(category, "invalid_voice_id")
        return voice_id

    async def create_voice(
        self, wav: bytes, prefix: str, language: str | None = None,
    ) -> str:
        self._validate_prefix(prefix)
        if len(wav) > MAX_SAMPLE_BYTES:
            raise SpeechProviderError("rejected", "sample_too_large")
        channels, width, rate, frames = _wav_properties(wav)
        if channels != 1 or width != 2 or rate < 16_000 or not 3 * rate <= frames <= 60 * rate:
            raise SpeechProviderError("rejected", "invalid_sample_format")
        fields: dict[str, object] = {
            "action": "create_voice",
            "target_model": MODEL,
            "prefix": prefix,
            "audio": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii"),
            "max_prompt_audio_length": 20.0,
            "enable_preprocess": False,
        }
        if language is not None:
            if language not in {"zh", "en", "fr", "de", "ja", "ko", "ru", "pt", "th", "id", "vi", "it", "es", "ms", "fil", "ar"}:
                raise SpeechProviderError("rejected", "invalid_source_language")
            fields["language_hints"] = [language]
        result = await self._json("customization", "voice-enrollment", fields, mutation=True)
        voice = self._validate_voice(result["output"].get("voice_id"), category="ambiguous")
        if not voice.startswith(f"{MODEL}-{prefix}-"):
            raise SpeechProviderError("ambiguous", "voice_prefix_mismatch")
        return voice

    async def query_voice(self, voice_id: str) -> str:
        result = await self._json(
            "customization", "voice-enrollment",
            {"action": "query_voice", "voice_id": self._validate_voice(voice_id)}, mutation=False,
        )
        output = result["output"]
        status = output.get("status")
        if output.get("target_model") != MODEL or status not in ("DEPLOYING", "OK", "UNDEPLOYED"):
            raise SpeechProviderError("retryable", "unknown_voice_status")
        return status

    async def find_voice(self, prefix: str) -> str | None:
        """Reconcile lost creation acknowledgement; never create a replacement."""
        self._validate_prefix(prefix)
        matches: set[str] = set()
        for page in range(20):
            result = await self._json(
                "customization", "voice-enrollment",
                {"action": "list_voice", "prefix": prefix, "page_size": 20, "page_index": page},
                mutation=False,
            )
            voices = result["output"].get("voice_list")
            if not isinstance(voices, list) or len(voices) > 20:
                raise SpeechProviderError("retryable", "invalid_voice_list")
            for voice in voices:
                if not isinstance(voice, dict):
                    raise SpeechProviderError("retryable", "invalid_voice_list")
                value = voice.get("voice_id")
                if (
                    voice.get("target_model") == MODEL
                    and isinstance(value, str)
                    and value.startswith(f"{MODEL}-{prefix}-")
                ):
                    matches.add(self._validate_voice(value, category="ambiguous"))
            if len(matches) > 1:
                raise SpeechProviderError("ambiguous", "multiple_matching_voices")
            if len(voices) < 20:
                return next(iter(matches), None)
        raise SpeechProviderError("ambiguous", "voice_list_incomplete")

    async def delete_voice(self, voice_id: str) -> None:
        result = await self._json(
            "customization", "voice-enrollment",
            {"action": "delete_voice", "voice_id": self._validate_voice(voice_id)}, mutation=True,
        )
        if result["output"]:
            raise SpeechProviderError("ambiguous", "unexpected_delete_response")

    async def synthesize(
        self, text: str, voice_id: str, *, rate: float = 1.0, instruction: str | None = None,
    ) -> SpeechReceipt:
        if not text.strip() or not math.isfinite(rate) or not 0.5 <= rate <= 2.0:
            raise SpeechProviderError("rejected", "invalid_speech_input")
        fields: dict[str, object] = {
            "text": text,
            "voice": self._validate_voice(voice_id),
            "format": "wav",
            "sample_rate": SAMPLE_RATE,
            "rate": rate,
            "enable_aigc_tag": True,
        }
        if instruction is not None and instruction.strip():
            fields["instruction"] = instruction
        result = await self._json("SpeechSynthesizer", MODEL, fields, mutation=True)
        output = result["output"]
        audio, usage, request_id = output.get("audio"), result.get("usage"), result.get("request_id")
        if (
            output.get("finish_reason") != "stop"
            or not isinstance(audio, dict)
            or not isinstance(usage, dict)
            or not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
            or not isinstance(audio.get("url"), str)
            or type(usage.get("characters")) is not int
            or usage["characters"] <= 0
        ):
            raise SpeechProviderError("ambiguous", "incomplete_speech_receipt")
        # URL validates in download(), not here, so caller can durably record
        # provider success/usage before handling rejected or expired output URLs.
        expiry = audio.get("expires_at")
        return SpeechReceipt(
            audio["url"], request_id, usage["characters"], expiry if type(expiry) is int else None
        )

    async def download(self, receipt: SpeechReceipt) -> bytes:
        if receipt.expires_at is not None and receipt.expires_at <= datetime.now(timezone.utc).timestamp():
            raise SpeechProviderError("rejected", "audio_url_expired")
        url, host = _output_url(receipt.audio_url)
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                addresses = await self._resolve(host)
                try:
                    ips = [ipaddress.ip_address(address) for address in addresses]
                except ValueError:
                    raise SpeechProviderError("rejected", "invalid_audio_address") from None
                if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
                    raise SpeechProviderError("rejected", "private_audio_address")
                # OSS may expose both IPv4 and IPv6. A single unavailable
                # address must not lose a successful, already-billed result.
                # Retry connectivity only and only within this download's
                # original deadline; never repeat the synthesis POST.
                for ip in list(dict.fromkeys(ips))[:3]:
                    ip_netloc = f"[{ip}]" if ip.version == 6 else str(ip)
                    pinned = urlunsplit(urlsplit(url)._replace(netloc=ip_netloc))
                    request = httpx.Request(
                        "GET", pinned,
                        headers={"Host": host, "Accept": "audio/wav", "Accept-Encoding": "identity"},
                        # Verify the original hostname at TLS while the socket
                        # connects to this prevalidated numeric address.
                        extensions={
                            "sni_hostname": host,
                            "timeout": {
                                "connect": 5.0, "read": DOWNLOAD_TIMEOUT,
                                "write": 5.0, "pool": 5.0,
                            },
                        },
                    )
                    try:
                        wav = await self._send(request, limit=MAX_RESULT_BYTES, mutation=False)
                        break
                    except SpeechProviderError as exc:
                        if exc.code != "not_connected":
                            raise
                else:
                    raise SpeechProviderError("retryable", "audio_download_unavailable")
        except (OSError, TimeoutError):
            raise SpeechProviderError("retryable", "audio_download_unavailable") from None
        channels, width, sample_rate, _ = _wav_properties(wav)
        if channels != 1 or width != 2 or sample_rate != SAMPLE_RATE:
            raise SpeechProviderError("rejected", "unexpected_speech_format")
        return wav
