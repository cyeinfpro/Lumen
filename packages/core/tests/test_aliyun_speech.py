"""Contract tests: use HTTP MockTransport, no credentials or upstream calls."""

import base64
import io
import json
import unittest
import wave

import httpx

from lumen_core.aliyun_speech import (
    MODEL,
    AliyunSpeechClient,
    SpeechProviderError,
    SpeechReceipt,
)

VOICE_ID = f"{MODEL}-test123-ab01"
SIGNED = "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.wav?token=private"
HOST = "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"


def wave_bytes(seconds: int = 1, sample_rate: int = 24_000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * seconds * sample_rate)
    return stream.getvalue()


async def public_dns(host):
    assert host == HOST
    return ["1.1.1.1"]


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, resolver=public_dns):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return AliyunSpeechClient("beijing", "llm-test", "unit-only", http=http, resolver=resolver)

    async def test_default_workspace_uses_fixed_regional_native_hosts(self):
        for region, host in (
            ("cn-beijing", "dashscope.aliyuncs.com"),
            ("ap-southeast-1", "dashscope-intl.aliyuncs.com"),
        ):
            calls = []

            def respond(request):
                self.assertEqual((request.url.scheme, request.url.host, request.url.port), ("https", host, None))
                self.assertEqual(request.headers["authorization"], "Bearer unit-only")
                calls.append(request.url.path)
                if request.url.path.endswith("/customization"):
                    return httpx.Response(200, json={"output": {"voice_id": VOICE_ID}})
                return httpx.Response(200, json={
                    "request_id": "req",
                    "output": {"finish_reason": "stop", "audio": {"url": SIGNED}},
                    "usage": {"characters": 4},
                })

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                adapter = AliyunSpeechClient(region, "", "unit-only", http=client)
                self.assertEqual(await adapter.create_voice(wave_bytes(3), "test123"), VOICE_ID)
                self.assertEqual((await adapter.synthesize("你好", VOICE_ID)).characters, 4)
            self.assertEqual(calls, [
                "/api/v1/services/audio/tts/customization",
                "/api/v1/services/audio/tts/SpeechSynthesizer",
            ])

    async def test_create_query_and_delete_contract(self):
        wav = wave_bytes(3)
        calls = []

        def respond(request):
            self.assertEqual(request.url.host, "llm-test.cn-beijing.maas.aliyuncs.com")
            self.assertTrue(request.url.path.endswith("/customization"))
            self.assertEqual(request.headers["authorization"], "Bearer unit-only")
            data = json.loads(request.content)
            self.assertEqual(data["model"], "voice-enrollment")
            fields = data["input"]
            action = fields["action"]
            calls.append(action)
            if action == "create_voice":
                expected = "data:audio/wav;base64," + base64.b64encode(wav).decode()
                self.assertEqual(fields["audio"], expected)
                self.assertEqual(fields["max_prompt_audio_length"], 20.0)
                self.assertEqual(fields["language_hints"], ["zh"])
                self.assertEqual(fields["target_model"], MODEL)
                return httpx.Response(200, json={"output": {"voice_id": VOICE_ID}})
            self.assertEqual(fields["voice_id"], VOICE_ID)
            if action == "query_voice":
                return httpx.Response(
                    200, json={"output": {"target_model": MODEL, "status": "OK"}}
                )
            return httpx.Response(200, json={"output": {}})

        client = self.client(respond)
        self.assertEqual(await client.create_voice(wav, "test123", "zh"), VOICE_ID)
        self.assertEqual(await client.query_voice(VOICE_ID), "OK")
        self.assertIsNone(await client.delete_voice(VOICE_ID))
        self.assertEqual(calls, ["create_voice", "query_voice", "delete_voice"])

    async def test_list_filters_by_exact_prefix_and_model(self):
        def respond(request):
            data = json.loads(request.content)
            self.assertEqual(data["input"]["action"], "list_voice")
            return httpx.Response(200, json={"output": {"voice_list": [
                {"voice_id": f"{MODEL}-test1234-ab01", "target_model": MODEL},
                {"voice_id": f"{MODEL}-test123-not-ours", "target_model": "other"},
                {"voice_id": VOICE_ID, "target_model": MODEL},
            ]}})

        self.assertEqual(await self.client(respond).find_voice("test123"), VOICE_ID)

    async def test_multiple_found_not_adopted(self):
        client = self.client(lambda _: httpx.Response(200, json={"output": {"voice_list": [
            {"voice_id": VOICE_ID, "target_model": MODEL},
            {"voice_id": VOICE_ID + "x", "target_model": MODEL},
        ]}}))
        with self.assertRaises(SpeechProviderError) as caught:
            await client.find_voice("test123")
        self.assertEqual(caught.exception.category, "ambiguous")

    async def test_synthesis_and_download_separation(self):
        def respond(request):
            if request.method == "POST":
                data = json.loads(request.content)
                self.assertTrue(request.url.path.endswith("/SpeechSynthesizer"))
                self.assertEqual(data["model"], MODEL)
                self.assertEqual(data["input"], {
                    "voice": VOICE_ID, "text": "你好", "format": "wav",
                    "sample_rate": 24000, "rate": 0.9, "instruction": "平静", "enable_aigc_tag": True,
                })
                return httpx.Response(200, json={
                    "request_id": "req-01",
                    "output": {"finish_reason": "stop", "audio": {"url": SIGNED}},
                    "usage": {"characters": 4},
                })
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, "1.1.1.1")
            self.assertEqual(request.headers["host"], HOST)
            self.assertEqual(request.extensions["sni_hostname"], HOST)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, content=wave_bytes())

        client = self.client(respond)
        receipt = await client.synthesize("你好", VOICE_ID, rate=.9, instruction="平静")
        self.assertEqual(receipt.characters, 4)
        self.assertNotIn("private", repr(receipt))
        self.assertEqual(await client.download(receipt), wave_bytes())

    async def test_no_automatic_replay_after_possible_submission(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("private request can be here", request=request)

        with self.assertRaises(SpeechProviderError) as caught:
            await self.client(respond).synthesize("你好", VOICE_ID)
        self.assertEqual(attempts, 1)
        self.assertEqual(caught.exception.category, "ambiguous")
        self.assertNotIn("private", str(caught.exception))

    async def test_429_retries_can_be_bounded(self):
        client = self.client(lambda _: httpx.Response(429, headers={"Retry-After": "780"}))
        with self.assertRaises(SpeechProviderError) as caught:
            await client.synthesize("你好", VOICE_ID)
        self.assertEqual((caught.exception.category, caught.exception.retry_after), ("retryable", 300))

    async def test_bad_url_private_destinations_and_redirect_blocked(self):
        calls = 0

        def respond(_):
            nonlocal calls
            calls += 1
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

        async def private_dns(_):
            return ["1.1.1.1", "127.0.0.1"]

        for invalid in (
            "https://127.0.0.1/a.wav", f"https://{HOST}.example.com/a",
            f"https://{HOST}@example.com/a", f"https://{HOST}:443/a",
        ):
            with self.assertRaises(SpeechProviderError):
                await self.client(respond).download(SpeechReceipt(invalid, "req", 4))
        with self.assertRaises(SpeechProviderError):
            await self.client(respond, private_dns).download(SpeechReceipt(SIGNED, "req", 4))
        self.assertEqual(calls, 0)
        with self.assertRaises(SpeechProviderError):
            await self.client(respond).download(SpeechReceipt(SIGNED, "req", 4))
        self.assertEqual(calls, 1)

    async def test_unsuccessful_output_not_accepted_as_success(self):
        client = self.client(lambda _: httpx.Response(200, json={
            "request_id": "one", "output": {"finish_reason": "stop", "audio": {"url": SIGNED}},
        }))
        with self.assertRaises(SpeechProviderError) as caught:
            await client.synthesize("你好", VOICE_ID)
        self.assertEqual(caught.exception.category, "ambiguous")

    async def test_unexpected_mutation_status_never_proves_rejection(self):
        for status in (201, 202, 204, 302, 409):
            with self.subTest(status=status):
                with self.assertRaises(SpeechProviderError) as caught:
                    await self.client(lambda _: httpx.Response(status)).synthesize("你好", VOICE_ID)
                self.assertEqual(caught.exception.category, "ambiguous")

    async def test_malformed_success_never_becomes_billable_receipt(self):
        for content in (
            b'{"output":{},"output":{}}',
            b'{"output":{},"code":"ContradictoryStatus"}',
            b'{"output":{},"nested":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}",
        ):
            with self.subTest(payload_size=len(content)):
                with self.assertRaises(SpeechProviderError) as caught:
                    await self.client(lambda _: httpx.Response(200, content=content)).delete_voice(VOICE_ID)
                self.assertEqual(caught.exception.category, "ambiguous")
                self.assertEqual(caught.exception.code, "unexpected_response")

    async def test_download_fails_over_only_before_connect(self):
        calls = []

        async def public_dns_pair(host):
            self.assertEqual(host, HOST)
            return ["2606:4700:4700::1111", "1.1.1.1"]

        def respond(request):
            calls.append(request.url.host)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.extensions["sni_hostname"], HOST)
            self.assertEqual(request.headers["host"], HOST)
            self.assertNotIn("authorization", request.headers)
            if len(calls) == 1:
                raise httpx.ConnectTimeout("no IPv6 route", request=request)
            return httpx.Response(200, content=wave_bytes())

        result = await self.client(respond, public_dns_pair).download(SpeechReceipt(SIGNED, "req", 4))
        self.assertEqual(result, wave_bytes())
        self.assertEqual(calls, ["2606:4700:4700::1111", "1.1.1.1"])

    async def test_invalid_download_is_distinct_from_synthesis(self):
        client = self.client(lambda _: httpx.Response(200, content=wave_bytes()[:-1]))
        with self.assertRaises(SpeechProviderError) as caught:
            await client.download(SpeechReceipt(SIGNED, "req", 4))
        self.assertEqual(caught.exception.code, "invalid_wav")


if __name__ == "__main__":
    unittest.main()
