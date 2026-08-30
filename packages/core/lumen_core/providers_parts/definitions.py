from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field


IMAGE_EDIT_INPUT_TRANSPORT_VALUES = ("url", "file")
DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT = "url"
PROVIDER_PURPOSE_VALUES = ("chat", "image", "embedding")
DEFAULT_PROVIDER_PURPOSES = ("chat", "image")
IMAGE_JOBS_ENDPOINT_VALUES = ("auto", "generations", "responses")
AGENT_API_VALUES = (
    "openai-responses",
    "openai-completions",
    "anthropic-messages",
)
DEFAULT_LEGACY_PROVIDER_BASE_URL = "https://api.example.com"
MAX_PROVIDER_WEIGHT = 1000
SSH_HOST_KEY_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")


@dataclass(frozen=True)
class ProviderProxyDefinition:
    name: str
    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False, compare=False)
    private_key_path: str | None = None
    enabled: bool = True
    known_hosts_path: str | None = None
    host_key_fingerprint: str | None = field(
        default=None,
        repr=False,
    )

    @property
    def known_hosts_file(self) -> str | None:
        return self.known_hosts_path

    @property
    def known_hosts(self) -> str | None:
        return self.known_hosts_path

    @property
    def fingerprint(self) -> str | None:
        return self.host_key_fingerprint


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    base_url: str
    api_key: str
    priority: int = 0
    weight: int = 1
    enabled: bool = True
    purposes: tuple[str, ...] = DEFAULT_PROVIDER_PURPOSES
    proxy_name: str | None = None
    proxy: ProviderProxyDefinition | None = field(
        default=None, repr=False, compare=False
    )
    image_rate_limit: str | None = None
    image_daily_quota: int | None = None
    image_jobs_enabled: bool = False
    image_streaming_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_edit_input_transport: str = DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT
    image_concurrency: int = 1
    responses_supported: bool | None = None
    vision_supported: bool | None = None
    agent_api: str = "openai-responses"
    agent_base_url: str = ""
    agent_models: tuple[str, ...] = ()
    agent_context_window: int = 128000
    agent_max_output_tokens: int = 16384
    agent_reasoning_supported: bool = True
    agent_thinking_level_map: dict[str, str | None] | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None


@dataclass
class RoundRobinState:
    counters: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def advance(self, priority: int) -> int:
        with self._lock:
            counter = self.counters.get(priority, 0)
            self.counters[priority] = counter + 1
            return counter
