"""One-click update status and release-note check service."""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lumen_core import __version__ as lumen_core_version
from pydantic import BaseModel, Field

from ..redis_client import get_redis
from .github_releases import GitHubRelease, GitHubReleasesClient, validate_update_tag


_SEMVER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class UpdateCacheOut(BaseModel):
    cached: bool
    fetched_at: str | None = None
    stale: bool = False
    ttl_remaining_sec: int = 0


class UpdateReleaseOut(BaseModel):
    tag: str
    name: str | None = None
    body_md: str = ""
    body_html: str = ""
    html_url: str | None = None
    published_at: str | None = None
    is_prerelease: bool = False
    assets: list[dict[str, Any]] = Field(default_factory=list)


class UpdateCheckOut(BaseModel):
    current_version: str
    latest_version: str
    has_update: bool | None
    release: UpdateReleaseOut | None = None
    cache: UpdateCacheOut
    channel: str
    resolved_image_tag: str
    build_type: str
    warning: str | None = None
    warm_pull: dict[str, Any] = Field(default_factory=dict)


class UpdateVersionOut(BaseModel):
    version: str
    image_tag: str
    release_id: str | None = None
    sha: str | None = None
    channel: str
    build_type: str
    degraded: list[str] = Field(default_factory=list)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _shared_env_path() -> Path:
    configured = os.environ.get("LUMEN_SHARED_ENV", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = os.environ.get("LUMEN_ROOT", str(_project_root())).strip() or str(
        _project_root()
    )
    candidate = Path(root).expanduser() / "shared" / ".env"
    return candidate


def _read_dotenv_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    prefix = f"{key}="
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value or None
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _release_html(body_md: str) -> str:
    lines = []
    for raw in (body_md or "").splitlines():
        line = raw.rstrip()
        escaped = html.escape(line)
        if not line:
            lines.append("<p></p>")
        elif line.startswith("### "):
            lines.append(f"<h3>{escaped[4:]}</h3>")
        elif line.startswith("## "):
            lines.append(f"<h2>{escaped[3:]}</h2>")
        elif line.startswith("# "):
            lines.append(f"<h1>{escaped[2:]}</h1>")
        elif line.startswith("- "):
            lines.append(f"<li>{escaped[2:]}</li>")
        else:
            lines.append(f"<p>{escaped}</p>")
    return "\n".join(lines) if lines else "<p></p>"


def _version_tuple(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    m = _SEMVER_RE.match(raw.strip())
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    return major, minor, patch


def _compare_versions(current: str, latest: str) -> bool | None:
    cur = _version_tuple(current)
    nxt = _version_tuple(latest)
    if cur is None or nxt is None:
        return None
    return nxt > cur


def _build_release_out(release: GitHubRelease) -> UpdateReleaseOut:
    return UpdateReleaseOut(
        tag=release.tag,
        name=release.name,
        body_md=release.body_md,
        body_html=_release_html(release.body_md),
        html_url=release.html_url,
        published_at=release.published_at,
        is_prerelease=release.is_prerelease,
        assets=release.assets,
    )


def _current_release_info(root: Path) -> tuple[str | None, str | None, str | None]:
    release_file = root / "current" / ".lumen_release.json"
    data = _read_json(release_file)
    return (
        str(data.get("id")) if data.get("id") else None,
        str(data.get("sha")) if data.get("sha") else None,
        str(data.get("branch")) if data.get("branch") else None,
    )


def _build_type(root: Path) -> str:
    if (root / "current" / "docker-compose.yml").is_file():
        return "docker"
    if os.environ.get("LUMEN_UPDATE_VIA_TRIGGER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return "docker"
    if Path("/app/docker-compose.yml").is_file():
        return "docker"
    if (root / "current" / "pyproject.toml").is_file():
        return "source"
    if (root / "pyproject.toml").is_file():
        return "source"
    return "unknown"


def _current_version(root: Path) -> str:
    for version_file in (root / "current" / "VERSION", root / "VERSION"):
        try:
            value = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    env_version = os.environ.get("LUMEN_VERSION", "").strip()
    if env_version:
        return env_version[1:] if env_version.startswith("v") else env_version
    return lumen_core_version or "0.0.0"


def _current_image_tag(root: Path) -> str:
    env = _shared_env_path()
    value = _read_dotenv_value(env, "LUMEN_IMAGE_TAG")
    if value:
        return value
    env_value = os.environ.get("LUMEN_IMAGE_TAG", "").strip()
    if env_value:
        return env_value
    version = _current_version(root)
    return f"v{version}"


def _resolve_target_tag(
    *,
    channel: str,
    current_tag: str,
    latest_release: GitHubRelease | None,
) -> str:
    normalized_channel = channel or "stable"
    if normalized_channel == "main":
        return "main"
    if normalized_channel == "pinned":
        return validate_update_tag(current_tag)
    if normalized_channel == "minor":
        match = _SEMVER_RE.match(current_tag)
        if match and match.group(2):
            return f"v{match.group(1)}.{match.group(2)}"
        return validate_update_tag(current_tag)
    if normalized_channel == "major":
        match = _SEMVER_RE.match(current_tag)
        if match:
            return f"v{match.group(1)}"
        return validate_update_tag(current_tag)
    if latest_release is not None:
        return latest_release.tag
    return validate_update_tag(current_tag)


class UpdateCheckService:
    def __init__(
        self,
        *,
        root: Path | None = None,
        redis: Any | None = None,
        ttl_sec: int = 1200,
    ) -> None:
        self.root = root or _project_root()
        self.redis = redis
        self.ttl_sec = max(0, int(ttl_sec))

    def _cache_key(
        self,
        channel: str,
        allow_prerelease: bool,
        *,
        current_version: str,
        current_tag: str,
        build_type: str,
        release_id: str | None,
    ) -> str:
        release_part = release_id or "none"
        return (
            "lumen:update:check:v2:"
            f"{channel}:{int(allow_prerelease)}:"
            f"{current_version}:{current_tag}:{build_type}:{release_part}"
        )

    async def _get_cache(self, key: str) -> UpdateCheckOut | None:
        try:
            client = self.redis or get_redis()
            raw = await client.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return UpdateCheckOut.model_validate_json(raw)
        except Exception:
            return None

    async def _set_cache(self, key: str, value: UpdateCheckOut) -> None:
        if self.ttl_sec <= 0:
            return
        try:
            client = self.redis or get_redis()
            await client.set(key, value.model_dump_json(), ex=self.ttl_sec)
        except Exception:
            return

    async def check(
        self,
        *,
        channel: str,
        allow_prerelease: bool = False,
        force: bool = False,
        proxy_url: str | None = None,
    ) -> UpdateCheckOut:
        current_version = _current_version(self.root)
        current_tag = _current_image_tag(self.root)
        build_type = _build_type(self.root)
        release_id, sha, _branch = _current_release_info(self.root)
        cache_key = self._cache_key(
            channel,
            allow_prerelease,
            current_version=current_version,
            current_tag=current_tag,
            build_type=build_type,
            release_id=release_id,
        )
        if not force:
            cached = await self._get_cache(cache_key)
            if cached is not None:
                ttl_remaining = self.ttl_sec
                cached.cache = UpdateCacheOut(
                    cached=True,
                    fetched_at=cached.cache.fetched_at if cached.cache else None,
                    stale=False,
                    ttl_remaining_sec=ttl_remaining,
                )
                return cached

        client = GitHubReleasesClient(proxy_url=proxy_url)
        latest_release: GitHubRelease | None = None
        latest_version = current_tag
        warning: str | None = None
        if channel not in {"pinned", "minor", "major"}:
            try:
                latest_release = await client.fetch_latest(
                    channel=channel, allow_prerelease=allow_prerelease
                )
                latest_version = latest_release.tag
            except Exception as exc:
                cached = await self._get_cache(cache_key)
                if cached is not None:
                    cached.cache = UpdateCacheOut(
                        cached=True,
                        fetched_at=cached.cache.fetched_at if cached.cache else None,
                        stale=True,
                        ttl_remaining_sec=0,
                    )
                    cached.warning = f"Using cached data: {exc}"
                    return cached
                warning = f"GitHub unavailable: {exc}"

        target_tag = _resolve_target_tag(
            channel=channel, current_tag=current_tag, latest_release=latest_release
        )
        has_update: bool | None
        if channel == "main":
            latest_version = "main"
            has_update = True if current_tag != target_tag else None
        elif channel in {"minor", "major"}:
            latest_version = target_tag
            has_update = True if current_tag != target_tag else None
        elif channel == "pinned":
            latest_version = target_tag
            has_update = False
        else:
            latest_version = latest_release.tag if latest_release else target_tag
            has_update = _compare_versions(current_version, latest_version)
            if has_update is None:
                has_update = current_tag != target_tag
            elif not has_update and current_tag != target_tag:
                has_update = True

        result = UpdateCheckOut(
            current_version=current_version,
            latest_version=latest_version,
            has_update=has_update,
            release=_build_release_out(latest_release) if latest_release else None,
            cache=UpdateCacheOut(
                cached=False,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                stale=False,
                ttl_remaining_sec=self.ttl_sec,
            ),
            channel=channel,
            resolved_image_tag=target_tag,
            build_type=build_type,
            warning=warning,
        )
        await self._set_cache(cache_key, result)
        return result

    async def version(self, *, channel: str) -> UpdateVersionOut:
        current_version = _current_version(self.root)
        current_tag = _current_image_tag(self.root)
        release_id, sha, _branch = _current_release_info(self.root)
        degraded: list[str] = []
        try:
            await get_redis().ping()
        except Exception:
            degraded.append("redis")
        return UpdateVersionOut(
            version=current_version,
            image_tag=current_tag,
            release_id=release_id,
            sha=sha,
            channel=channel,
            build_type=_build_type(self.root),
            degraded=degraded,
        )
