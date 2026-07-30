"""Environment and proxy helpers for the admin update route."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def read_dotenv_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
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


def shared_env_path(
    script: Path | None,
    *,
    configured_path: str,
    lumen_root: Callable[[], Path],
) -> Path:
    if configured_path:
        return Path(configured_path).expanduser()
    root = lumen_root()
    candidate = root / "shared" / ".env"
    if candidate.is_file():
        return candidate
    if script is not None:
        release_env = script.parent.parent / ".env"
        try:
            if release_env.is_file():
                return release_env.resolve()
        except OSError:
            pass
    return candidate


def clean_proxy_env(env: dict[str, str]) -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)


def apply_proxy_env(env: dict[str, str], proxy_url: str) -> None:
    env["LUMEN_UPDATE_PROXY_URL"] = proxy_url
    env["LUMEN_HTTP_PROXY"] = proxy_url
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["ALL_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url
    env["all_proxy"] = proxy_url


def proxy_url_from_env_file(path: Path) -> str | None:
    for key in (
        "LUMEN_UPDATE_PROXY_URL",
        "LUMEN_HTTP_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        value = read_dotenv_value(path, key)
        if value:
            return value
    return None


def apply_dotenv_proxy_env(env: dict[str, str], env_file: Path) -> str | None:
    proxy_url = proxy_url_from_env_file(env_file)
    if not proxy_url:
        return None
    apply_proxy_env(env, proxy_url)
    no_proxy = (
        read_dotenv_value(env_file, "NO_PROXY")
        or read_dotenv_value(env_file, "no_proxy")
        or "127.0.0.1,localhost,::1"
    )
    env.setdefault("NO_PROXY", no_proxy)
    env.setdefault("no_proxy", no_proxy)
    return proxy_url


def mask_proxy_url(proxy_url: str) -> str:
    if "@" not in proxy_url:
        return proxy_url
    scheme, rest = proxy_url.split("://", 1) if "://" in proxy_url else ("", proxy_url)
    _auth, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"
