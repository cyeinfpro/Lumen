#!/usr/bin/env python3
"""Fetch and validate Lumen release manifests used by host installers."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_STABLE_TAG_RE = re.compile(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)$")
_ALIAS_RE = re.compile(r"^v([0-9]+)(?:\.([0-9]+))?$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMMUTABLE_REF_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_LEGACY_SERVICES = ("api", "worker", "web", "tgbot")
_COMPONENT_SERVICES = ("agent-runtime",)
_SERVICES = (*_LEGACY_SERVICES, *_COMPONENT_SERVICES)
_DEPENDENCIES = ("python", "postgres", "redis")
_COMPONENT_DEPENDENCIES = ("node",)
_ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024


class ManifestError(ValueError):
    pass


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
            raise ManifestError("release manifest redirect target is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_image(service: str, value: object, tag: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ManifestError(f"missing image metadata for {service}")
    repository = f"ghcr.io/cyeinfpro/lumen-{service}"
    expected_tag = f"{repository}:{tag}"
    digest = value.get("digest")
    immutable_ref = value.get("immutable_ref")
    if value.get("tag") != expected_tag:
        raise ManifestError(f"manifest tag mismatch for {service}")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ManifestError(f"invalid digest for {service}")
    if immutable_ref != f"{repository}@{digest}":
        raise ManifestError(f"immutable ref mismatch for {service}")
    artifacts = value.get("artifacts")
    if artifacts is not None and artifacts != {
        "signature": "sigstore-keyless",
        "sbom": "spdx-json",
        "sbom_attestation_type": "spdxjson",
    }:
        raise ManifestError(f"invalid artifact metadata for {service}")
    output = {
        "tag": expected_tag,
        "digest": digest,
        "immutable_ref": str(immutable_ref),
    }
    if artifacts is not None:
        output["artifacts"] = artifacts
    return output


def validate_manifest(payload: object, *, tag: str) -> dict[str, object]:
    if not _TAG_RE.fullmatch(tag):
        raise ManifestError("release tag is invalid")
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError("unsupported release manifest schema")
    if payload.get("version") != tag:
        raise ManifestError("release manifest version mismatch")
    commit_sha = payload.get("commit_sha")
    if not isinstance(commit_sha, str) or not _COMMIT_RE.fullmatch(commit_sha):
        raise ManifestError("release manifest commit is invalid")
    images = payload.get("images")
    if not isinstance(images, dict) or set(images) != set(_LEGACY_SERVICES):
        raise ManifestError("release manifest image set is invalid")
    validated_images = {
        service: _validate_image(service, images.get(service), tag)
        for service in _LEGACY_SERVICES
    }
    components = payload.get("components")
    if components is None:
        validated_components: dict[str, dict[str, object]] = {}
    elif not isinstance(components, dict) or set(components) != set(
        _COMPONENT_SERVICES
    ):
        raise ManifestError("release manifest component image set is invalid")
    else:
        validated_components = {
            service: _validate_image(service, components.get(service), tag)
            for service in _COMPONENT_SERVICES
        }
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != set(_DEPENDENCIES):
        raise ManifestError("release manifest dependency image set is invalid")
    validated_dependencies: dict[str, dict[str, str]] = {}
    for name in _DEPENDENCIES:
        value = dependencies.get(name)
        reference = value.get("immutable_ref") if isinstance(value, dict) else None
        if not isinstance(reference, str) or not _IMMUTABLE_REF_RE.fullmatch(
            reference
        ):
            raise ManifestError(f"invalid immutable dependency image for {name}")
        validated_dependencies[name] = {"immutable_ref": reference}
    component_dependencies = payload.get("component_dependencies")
    if component_dependencies is None:
        validated_component_dependencies: dict[str, dict[str, str]] = {}
    elif not isinstance(component_dependencies, dict) or set(
        component_dependencies
    ) != set(_COMPONENT_DEPENDENCIES):
        raise ManifestError("release manifest component dependency set is invalid")
    else:
        validated_component_dependencies = {}
        for name in _COMPONENT_DEPENDENCIES:
            value = component_dependencies.get(name)
            reference = value.get("immutable_ref") if isinstance(value, dict) else None
            if not isinstance(reference, str) or not _IMMUTABLE_REF_RE.fullmatch(
                reference
            ):
                raise ManifestError(
                    f"invalid immutable component dependency image for {name}"
                )
            validated_component_dependencies[name] = {"immutable_ref": reference}
    return {
        **payload,
        "dependencies": validated_dependencies,
        "images": validated_images,
        "components": validated_components,
        "component_dependencies": validated_component_dependencies,
    }


def load_manifest(path: Path, *, tag: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError("cannot read release manifest") from exc
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise ManifestError("release manifest size is invalid")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("release manifest is not valid JSON") from exc
    return validate_manifest(payload, tag=tag)


def _download_json(url: str, *, label: str) -> object:
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/octet-stream",
            "User-Agent": "lumen-release-manifest-guard",
        },
    )
    try:
        with opener.open(request, timeout=20) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if (
                final.scheme != "https"
                or final.hostname not in _ALLOWED_DOWNLOAD_HOSTS
            ):
                raise ManifestError(f"{label} final URL is not allowed")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > _MAX_MANIFEST_BYTES:
                raise ManifestError(f"{label} is too large")
            raw = response.read(_MAX_MANIFEST_BYTES + 1)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(f"cannot download {label}") from exc
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise ManifestError(f"{label} size is invalid")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{label} is not valid JSON") from exc


def fetch_manifest(*, tag: str, output: Path) -> None:
    if not _TAG_RE.fullmatch(tag):
        raise ManifestError("release tag is invalid")
    url = (
        "https://github.com/cyeinfpro/Lumen/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/release-manifest.json"
    )
    payload = _download_json(url, label="release manifest")
    validated = validate_manifest(payload, tag=tag)

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(validated, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, output)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def resolve_latest_tag() -> str:
    payload = _download_json(
        "https://api.github.com/repos/cyeinfpro/Lumen/releases/latest",
        label="latest release response",
    )
    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
        raise ManifestError("latest release tag is invalid")
    return tag


def select_matching_release_tag(payload: object, *, alias: str) -> str:
    alias_match = _ALIAS_RE.fullmatch(alias)
    if alias_match is None:
        raise ManifestError("release alias is invalid")
    if not isinstance(payload, list):
        raise ManifestError("release list response is invalid")
    required_major = int(alias_match.group(1))
    required_minor = (
        int(alias_match.group(2)) if alias_match.group(2) is not None else None
    )
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for item in payload:
        if (
            not isinstance(item, dict)
            or item.get("draft")
            or item.get("prerelease")
        ):
            continue
        tag = item.get("tag_name")
        match = _STABLE_TAG_RE.fullmatch(tag) if isinstance(tag, str) else None
        if match is None:
            continue
        version = tuple(int(match.group(index)) for index in range(1, 4))
        if version[0] != required_major:
            continue
        if required_minor is not None and version[1] != required_minor:
            continue
        candidates.append((version, str(tag)))
    if not candidates:
        raise ManifestError(f"no concrete release matches alias {alias}")
    return max(candidates)[1]


def resolve_alias_tag(alias: str) -> str:
    releases: list[object] = []
    for page in range(1, 101):
        payload = _download_json(
            "https://api.github.com/repos/cyeinfpro/Lumen/releases"
            f"?per_page=100&page={page}",
            label=f"release list response page {page}",
        )
        if not isinstance(payload, list):
            raise ManifestError("release list response is invalid")
        releases.extend(payload)
        if len(payload) < 100:
            return select_matching_release_tag(releases, alias=alias)
    raise ManifestError("release list pagination exceeded the safety limit")


def print_entries(*, path: Path, tag: str, services: list[str]) -> None:
    payload = load_manifest(path, tag=tag)
    images = payload["images"]
    components = payload["components"]
    assert isinstance(images, dict)
    assert isinstance(components, dict)
    available = {**images, **components}
    requested = services or list(available)
    for service in requested:
        if service not in _SERVICES:
            raise ManifestError(f"unknown service: {service}")
        if service not in available:
            raise ManifestError(f"service is absent from this release: {service}")
        image = available[service]
        assert isinstance(image, dict)
        print(
            "\t".join(
                (
                    service,
                    str(image["tag"]),
                    str(image["digest"]),
                    str(image["immutable_ref"]),
                )
            )
        )


def print_commit(*, path: Path, tag: str) -> None:
    payload = load_manifest(path, tag=tag)
    print(payload["commit_sha"])


def print_compose_env(*, path: Path, tag: str) -> None:
    payload = load_manifest(path, tag=tag)
    images = payload["images"]
    dependencies = payload["dependencies"]
    assert isinstance(images, dict)
    assert isinstance(dependencies, dict)
    values = {
        "LUMEN_POSTGRES_IMAGE_REF": dependencies["postgres"]["immutable_ref"],
        "LUMEN_REDIS_IMAGE_REF": dependencies["redis"]["immutable_ref"],
        "LUMEN_API_IMAGE_REF": images["api"]["immutable_ref"],
        "LUMEN_WORKER_IMAGE_REF": images["worker"]["immutable_ref"],
        "LUMEN_WEB_IMAGE_REF": images["web"]["immutable_ref"],
        "LUMEN_TGBOT_IMAGE_REF": images["tgbot"]["immutable_ref"],
        "LUMEN_PYTHON_BASE_REF": dependencies["python"]["immutable_ref"],
    }
    components = payload["components"]
    component_dependencies = payload["component_dependencies"]
    assert isinstance(components, dict)
    assert isinstance(component_dependencies, dict)
    agent_runtime = components.get("agent-runtime")
    if isinstance(agent_runtime, dict):
        values["LUMEN_AGENT_RUNTIME_IMAGE_REF"] = agent_runtime["immutable_ref"]
    node = component_dependencies.get("node")
    if isinstance(node, dict):
        values["LUMEN_NODE_BASE_REF"] = node["immutable_ref"]
    for key, value in values.items():
        print(f"{key}\t{value}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("--tag", required=True)
    fetch.add_argument("--output", type=Path, required=True)
    entries = subparsers.add_parser("entries")
    entries.add_argument("--tag", required=True)
    entries.add_argument("--manifest", type=Path, required=True)
    entries.add_argument("--service", action="append", default=[])
    commit = subparsers.add_parser("commit")
    commit.add_argument("--tag", required=True)
    commit.add_argument("--manifest", type=Path, required=True)
    compose_env = subparsers.add_parser("compose-env")
    compose_env.add_argument("--tag", required=True)
    compose_env.add_argument("--manifest", type=Path, required=True)
    subparsers.add_parser("latest-tag")
    alias = subparsers.add_parser("resolve-alias")
    alias.add_argument("--alias", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "fetch":
            fetch_manifest(tag=args.tag, output=args.output)
        elif args.command == "latest-tag":
            print(resolve_latest_tag())
        elif args.command == "resolve-alias":
            print(resolve_alias_tag(args.alias))
        elif args.command == "commit":
            print_commit(path=args.manifest, tag=args.tag)
        elif args.command == "compose-env":
            print_compose_env(path=args.manifest, tag=args.tag)
        else:
            print_entries(
                path=args.manifest,
                tag=args.tag,
                services=args.service,
            )
    except ManifestError as exc:
        print(f"release manifest validation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
