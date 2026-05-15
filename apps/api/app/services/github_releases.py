"""GitHub Releases client used by the admin update check endpoint."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field


TAG_RE = re.compile(r"^(?:v[0-9]+(?:\.[0-9]+){0,2}(?:-[0-9A-Za-z.-]+)?|main|latest)$")
_ALLOWED_RELEASE_HOSTS = {"github.com", "api.github.com"}


class GitHubRelease(BaseModel):
    tag: str
    name: str | None = None
    body_md: str = ""
    html_url: str | None = None
    published_at: str | None = None
    is_prerelease: bool = False
    assets: list[dict[str, Any]] = Field(default_factory=list)


def validate_update_tag(tag: str) -> str:
    value = (tag or "").strip()
    if not TAG_RE.fullmatch(value):
        raise ValueError("invalid update tag")
    return value


def _validate_release_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_RELEASE_HOSTS:
        raise ValueError("release URL is not an allowed GitHub URL")
    return url


def _release_from_payload(payload: dict[str, Any]) -> GitHubRelease:
    tag = validate_update_tag(str(payload.get("tag_name") or ""))
    assets_raw = payload.get("assets")
    assets = assets_raw if isinstance(assets_raw, list) else []
    return GitHubRelease(
        tag=tag,
        name=str(payload.get("name") or tag),
        body_md=str(payload.get("body") or ""),
        html_url=_validate_release_url(
            str(payload.get("html_url")) if payload.get("html_url") else None
        ),
        published_at=(
            str(payload.get("published_at")) if payload.get("published_at") else None
        ),
        is_prerelease=bool(payload.get("prerelease")),
        assets=[item for item in assets if isinstance(item, dict)],
    )


class GitHubReleasesClient:
    def __init__(
        self,
        *,
        repo: str = "cyeinfpro/Lumen",
        proxy_url: str | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self.repo = repo
        self.proxy_url = proxy_url
        self.timeout_s = timeout_s

    async def _get_json(self, path: str) -> Any:
        url = f"https://api.github.com/repos/{self.repo}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "lumen-admin-update-check",
        }
        kwargs: dict[str, Any] = {
            "timeout": self.timeout_s,
            "headers": headers,
            "follow_redirects": False,
        }
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def fetch_latest(
        self,
        *,
        channel: str = "stable",
        allow_prerelease: bool = False,
    ) -> GitHubRelease:
        if channel == "main":
            return GitHubRelease(
                tag="main",
                name="main",
                body_md="Rolling main image tag.",
                published_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            )
        if channel in {"pinned", "minor", "major"}:
            raise ValueError(f"channel={channel} requires a current tag context")
        if allow_prerelease:
            payload = await self._get_json("/releases?per_page=10")
            if not isinstance(payload, list):
                raise ValueError("unexpected GitHub releases response")
            for item in payload:
                if not isinstance(item, dict) or item.get("draft"):
                    continue
                return _release_from_payload(item)
            raise ValueError("no GitHub release found")
        payload = await self._get_json("/releases/latest")
        if not isinstance(payload, dict):
            raise ValueError("unexpected GitHub latest release response")
        return _release_from_payload(payload)

    async def fetch_tag(self, tag: str) -> GitHubRelease:
        clean = validate_update_tag(tag)
        payload = await self._get_json(f"/releases/tags/{clean}")
        if not isinstance(payload, dict):
            raise ValueError("unexpected GitHub tag release response")
        return _release_from_payload(payload)
