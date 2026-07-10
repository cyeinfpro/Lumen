#!/usr/bin/env python3
"""Build the machine-readable manifest and notes for a tagged Docker release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


SERVICES = ("api", "worker", "tgbot", "web")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class ReleaseManifestError(ValueError):
    """Raised when release metadata is incomplete or ambiguous."""


def parse_alembic_heads(output: str) -> list[str]:
    if "todo" in output.lower():
        raise ReleaseManifestError("alembic heads output contains TODO")

    heads: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "(head)" not in line:
            raise ReleaseManifestError(f"unexpected alembic heads line: {line}")
        revision = line.split(maxsplit=1)[0]
        if not re.fullmatch(r"[0-9A-Za-z_]+", revision):
            raise ReleaseManifestError(f"invalid alembic revision: {revision}")
        heads.append(revision)

    if len(heads) != 1:
        raise ReleaseManifestError(
            f"release requires exactly one alembic head, found {len(heads)}"
        )
    return heads


def _parse_image_digest(value: str) -> tuple[str, str]:
    service, separator, digest = value.partition("=")
    if not separator or service not in SERVICES:
        raise ReleaseManifestError(
            f"image digest must be SERVICE=sha256:..., got {value!r}"
        )
    if not SHA256_RE.fullmatch(digest):
        raise ReleaseManifestError(f"invalid digest for {service}: {digest!r}")
    return service, digest


def resolve_image_digest(reference: str) -> str:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseManifestError(
            f"failed to inspect image {reference}: {stderr or result.returncode}"
        )
    if not result.stdout:
        raise ReleaseManifestError(f"empty manifest returned for image {reference}")
    return f"sha256:{hashlib.sha256(result.stdout).hexdigest()}"


def build_release_manifest(
    *,
    version: str,
    commit_sha: str,
    short_sha: str,
    registry: str,
    alembic_heads: list[str],
    image_digests: dict[str, str],
    generated_at: str,
) -> dict[str, object]:
    if not VERSION_RE.fullmatch(version):
        raise ReleaseManifestError(f"invalid release version: {version!r}")
    if not COMMIT_RE.fullmatch(commit_sha):
        raise ReleaseManifestError(f"invalid commit SHA: {commit_sha!r}")
    if not short_sha or not commit_sha.startswith(short_sha):
        raise ReleaseManifestError("short SHA must be a prefix of the release commit")
    if len(alembic_heads) != 1:
        raise ReleaseManifestError("release manifest requires exactly one alembic head")
    if set(image_digests) != set(SERVICES):
        missing = sorted(set(SERVICES) - set(image_digests))
        extra = sorted(set(image_digests) - set(SERVICES))
        raise ReleaseManifestError(
            f"image digest set mismatch; missing={missing}, extra={extra}"
        )

    clean_registry = registry.rstrip("/")
    images: dict[str, dict[str, str]] = {}
    for service in SERVICES:
        digest = image_digests[service]
        if not SHA256_RE.fullmatch(digest):
            raise ReleaseManifestError(f"invalid digest for {service}: {digest!r}")
        repository = f"{clean_registry}/lumen-{service}"
        images[service] = {
            "tag": f"{repository}:{version}",
            "sha_tag": f"{repository}:sha-{short_sha}",
            "digest": digest,
            "immutable_ref": f"{repository}@{digest}",
        }

    manifest: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "commit_sha": commit_sha,
        "short_sha": short_sha,
        "generated_at": generated_at,
        "alembic_heads": alembic_heads,
        "images": images,
    }
    if "todo" in json.dumps(manifest, ensure_ascii=True).lower():
        raise ReleaseManifestError("release manifest contains TODO")
    return manifest


def render_release_notes(manifest: dict[str, object]) -> str:
    version = str(manifest["version"])
    commit_sha = str(manifest["commit_sha"])
    short_sha = str(manifest["short_sha"])
    heads = manifest["alembic_heads"]
    images = manifest["images"]
    if not isinstance(heads, list) or not isinstance(images, dict):
        raise ReleaseManifestError("invalid release manifest structure")

    lines = [
        f"## Lumen {version}",
        "",
        f"Commit: `{commit_sha}` (sha-{short_sha})",
        "",
        "### images",
        "",
        "```text",
    ]
    for service in SERVICES:
        image = images.get(service)
        if not isinstance(image, dict):
            raise ReleaseManifestError(f"missing image metadata for {service}")
        lines.append(str(image["immutable_ref"]))
    lines.extend(
        [
            "```",
            "",
            "### alembic_heads",
            "",
            "```text",
            *(str(head) for head in heads),
            "```",
            "",
            "### release_manifest",
            "",
            "`release-manifest.json` contains the commit, schema head, tags, and "
            "immutable image digests for this release.",
            "",
        ]
    )
    notes = "\n".join(lines)
    if "todo" in notes.lower():
        raise ReleaseManifestError("release notes contain TODO")
    return notes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--short-sha", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--alembic-heads-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--notes-output", type=Path, required=True)
    parser.add_argument(
        "--image-digest",
        action="append",
        default=[],
        metavar="SERVICE=SHA256",
        help="Provide a digest directly; intended for tests or offline generation.",
    )
    parser.add_argument(
        "--resolve-images",
        action="store_true",
        help="Resolve release tags with docker buildx imagetools inspect.",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO-8601 timestamp; defaults to the current UTC time.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        heads = parse_alembic_heads(
            args.alembic_heads_file.read_text(encoding="utf-8")
        )
        digests = dict(_parse_image_digest(value) for value in args.image_digest)
        if len(digests) != len(args.image_digest):
            raise ReleaseManifestError("duplicate image digest service")

        if args.resolve_images:
            for service in SERVICES:
                if service in digests:
                    raise ReleaseManifestError(
                        f"digest for {service} was provided and requested for resolution"
                    )
                reference = (
                    f"{args.registry.rstrip('/')}/lumen-{service}:{args.version}"
                )
                digests[service] = resolve_image_digest(reference)

        generated_at = args.generated_at or datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        manifest = build_release_manifest(
            version=args.version,
            commit_sha=args.commit,
            short_sha=args.short_sha,
            registry=args.registry,
            alembic_heads=heads,
            image_digests=digests,
            generated_at=generated_at,
        )
        args.output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        args.notes_output.write_text(
            render_release_notes(manifest),
            encoding="utf-8",
        )
    except (OSError, ReleaseManifestError) as exc:
        print(f"release manifest error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
