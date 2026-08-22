#!/usr/bin/env python3
"""Verify a completed Lumen release and its stable image aliases."""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Sequence
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_manifest_guard import (  # noqa: E402
    ManifestError,
    fetch_manifest,
    load_manifest,
)


LEGACY_SERVICES = ("api", "worker", "tgbot", "web")
COMPONENT_SERVICES = ("agent-runtime",)
SERVICES = (*LEGACY_SERVICES, *COMPONENT_SERVICES)
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_DIGEST_ANNOTATION = "io.lumen.promotion.source-digest"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_OUTPUT = ROOT / ".audit_state" / "release-proof.json"


class ReleaseProofError(RuntimeError):
    """Raised when a release cannot be proven end to end."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RegistryManifest:
    digest: str
    source_digest: str


CommandRunner = Callable[[Sequence[str], Path], CommandResult]
ManifestFetcher = Callable[[str], dict[str, object]]
RegistryFetcher = Callable[[str, str], RegistryManifest]
ArtifactVerifier = Callable[[str], dict[str, object]]


def _run(command: Sequence[str], cwd: Path) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _command_json(
    command: Sequence[str],
    *,
    root: Path,
    runner: CommandRunner,
    label: str,
) -> object:
    result = runner(command, root)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or result.returncode
        raise ReleaseProofError(f"{label} failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseProofError(f"{label} returned invalid JSON") from exc


def _fetch_release_manifest(tag: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="lumen-release-proof-") as directory:
        path = Path(directory) / "release-manifest.json"
        try:
            fetch_manifest(tag=tag, output=path)
            return load_manifest(path, tag=tag)
        except ManifestError as exc:
            raise ReleaseProofError(str(exc)) from exc


def _github_token(runner: CommandRunner, root: Path) -> str | None:
    result = runner(("gh", "auth", "token"), root)
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def _read_url(request: urllib.request.Request, *, label: str) -> tuple[bytes, Any]:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            headers = response.headers
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseProofError(f"cannot fetch {label}") from exc
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ReleaseProofError(f"{label} size is invalid")
    return raw, headers


def _registry_bearer_token(repository: str, github_token: str | None) -> str:
    query = urllib.parse.urlencode(
        {"scope": f"repository:{repository}:pull", "service": "ghcr.io"}
    )
    request = urllib.request.Request(
        f"https://ghcr.io/token?{query}",
        headers={"User-Agent": "lumen-release-proof"},
    )
    if github_token:
        encoded = base64.b64encode(
            f"x-access-token:{github_token}".encode("utf-8")
        ).decode("ascii")
        request.add_header("Authorization", f"Basic {encoded}")
    raw, _headers = _read_url(request, label="GHCR token")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseProofError("GHCR token response is invalid") from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ReleaseProofError("GHCR token response lacks a token")
    return token


def _registry_fetcher(github_token: str | None) -> RegistryFetcher:
    token_cache: dict[str, str] = {}

    def fetch(repository: str, reference: str) -> RegistryManifest:
        token = token_cache.get(repository)
        if token is None:
            token = _registry_bearer_token(repository, github_token)
            token_cache[repository] = token
        encoded_reference = urllib.parse.quote(reference, safe=":@")
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{repository}/manifests/{encoded_reference}",
            headers={
                "Accept": (
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
                "Authorization": f"Bearer {token}",
                "User-Agent": "lumen-release-proof",
            },
        )
        raw, headers = _read_url(
            request,
            label=f"GHCR manifest {repository}:{reference}",
        )
        digest = headers.get("Docker-Content-Digest")
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseProofError(
                f"GHCR manifest {repository}:{reference} is invalid"
            ) from exc
        annotations = payload.get("annotations") if isinstance(payload, dict) else None
        source_digest = (
            annotations.get(SOURCE_DIGEST_ANNOTATION)
            if isinstance(annotations, dict)
            else None
        )
        if source_digest is None:
            source_digest = digest
        if (
            not isinstance(source_digest, str)
            or DIGEST_RE.fullmatch(source_digest) is None
        ):
            raise ReleaseProofError(
                f"GHCR manifest {repository}:{reference} has invalid source digest"
            )
        return RegistryManifest(digest=digest, source_digest=source_digest)

    return fetch


def _artifact_verifier(
    runner: CommandRunner,
    root: Path,
) -> ArtifactVerifier:
    identity = (
        "https://github.com/cyeinfpro/Lumen/.github/workflows/"
        "docker-release.yml@refs/tags/v.*"
    )

    def verify(reference: str) -> dict[str, object]:
        signature = runner(
            (
                "cosign",
                "verify",
                "--certificate-oidc-issuer",
                "https://token.actions.githubusercontent.com",
                "--certificate-identity-regexp",
                identity,
                reference,
            ),
            root,
        )
        if signature.returncode != 0:
            detail = signature.stderr.strip() or signature.stdout.strip()
            raise ReleaseProofError(
                f"image signature verification failed for {reference}: "
                f"{detail or signature.returncode}"
            )
        sbom = runner(
            (
                "cosign",
                "verify-attestation",
                "--type",
                "spdxjson",
                "--certificate-oidc-issuer",
                "https://token.actions.githubusercontent.com",
                "--certificate-identity-regexp",
                identity,
                reference,
            ),
            root,
        )
        if sbom.returncode != 0:
            detail = sbom.stderr.strip() or sbom.stdout.strip()
            raise ReleaseProofError(
                f"SPDX SBOM attestation verification failed for {reference}: "
                f"{detail or sbom.returncode}"
            )
        return {
            "signature": "verified",
            "sbom_attestation": "verified",
            "sbom_format": "spdx-json",
        }

    return verify


def _stable_aliases(tag: str) -> tuple[str, ...]:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseProofError("release proof requires a stable vX.Y.Z tag")
    major, minor, _patch = match.groups()
    return (tag, f"v{major}.{minor}", f"v{major}", "latest")


def verify_release(
    *,
    tag: str,
    commit: str,
    repository: str = "cyeinfpro/Lumen",
    registry_namespace: str = "cyeinfpro",
    root: Path = ROOT,
    runner: CommandRunner = _run,
    manifest_fetcher: ManifestFetcher = _fetch_release_manifest,
    registry_fetcher: RegistryFetcher | None = None,
    artifact_verifier: ArtifactVerifier | None = None,
) -> dict[str, object]:
    aliases = _stable_aliases(tag)
    if COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseProofError("release commit must be a full Git SHA")

    tag_result = runner(("git", "rev-list", "-n", "1", tag), root)
    tag_commit = tag_result.stdout.strip()
    if tag_result.returncode != 0 or tag_commit != commit:
        raise ReleaseProofError(
            f"tag {tag} resolves to {tag_commit or 'nothing'}, expected {commit}"
        )

    runs = _command_json(
        (
            "gh",
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            "docker-release.yml",
            "--event",
            "push",
            "--branch",
            tag,
            "--limit",
            "20",
            "--json",
            "databaseId,status,conclusion,headSha,headBranch,url",
        ),
        root=root,
        runner=runner,
        label="Docker Release run lookup",
    )
    matching_runs = (
        [
            item
            for item in runs
            if isinstance(item, dict)
            and item.get("headSha") == commit
            and item.get("headBranch") == tag
        ]
        if isinstance(runs, list)
        else []
    )
    successful_runs = [
        item
        for item in matching_runs
        if item.get("status") == "completed" and item.get("conclusion") == "success"
    ]
    if not successful_runs:
        raise ReleaseProofError(
            f"no successful completed Docker Release run for {tag} at {commit}"
        )
    run = max(successful_runs, key=lambda item: int(item.get("databaseId", 0)))

    release = _command_json(
        ("gh", "api", f"repos/{repository}/releases/tags/{tag}"),
        root=root,
        runner=runner,
        label="GitHub Release lookup",
    )
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != tag
        or release.get("draft") is not False
        or release.get("prerelease") is not False
    ):
        raise ReleaseProofError(f"GitHub Release {tag} is not a final stable release")
    latest = _command_json(
        ("gh", "api", f"repos/{repository}/releases/latest"),
        root=root,
        runner=runner,
        label="latest GitHub Release lookup",
    )
    if not isinstance(latest, dict) or latest.get("tag_name") != tag:
        raise ReleaseProofError(f"stable update channel does not resolve to {tag}")

    manifest = manifest_fetcher(tag)
    if manifest.get("commit_sha") != commit:
        raise ReleaseProofError("release manifest commit does not match the tag commit")
    images = manifest.get("images")
    components = manifest.get("components")
    if (
        not isinstance(images, dict)
        or set(images) != set(LEGACY_SERVICES)
        or not isinstance(components, dict)
        or set(components) != set(COMPONENT_SERVICES)
    ):
        raise ReleaseProofError("release manifest has an incomplete image set")
    release_images = {**images, **components}

    fetch_registry = registry_fetcher
    if fetch_registry is None:
        fetch_registry = _registry_fetcher(_github_token(runner, root))
    verify_artifacts = artifact_verifier or _artifact_verifier(runner, root)
    image_proof: dict[str, object] = {}
    for service in SERVICES:
        image = release_images.get(service)
        expected = image.get("digest") if isinstance(image, dict) else None
        if not isinstance(expected, str) or DIGEST_RE.fullmatch(expected) is None:
            raise ReleaseProofError(f"release manifest digest is invalid for {service}")
        expected_artifacts = image.get("artifacts") if isinstance(image, dict) else None
        if expected_artifacts != {
            "signature": "sigstore-keyless",
            "sbom": "spdx-json",
            "sbom_attestation_type": "spdxjson",
        }:
            raise ReleaseProofError(
                f"release manifest artifact metadata is invalid for {service}"
            )
        repository_path = f"{registry_namespace}/lumen-{service}"
        immutable_ref = f"ghcr.io/{repository_path}@{expected}"
        artifact_proof = verify_artifacts(immutable_ref)
        alias_proof: dict[str, dict[str, str]] = {}
        for alias in aliases:
            observed = fetch_registry(repository_path, alias)
            if observed.source_digest != expected:
                raise ReleaseProofError(
                    f"{repository_path}:{alias} resolves to "
                    f"{observed.source_digest}, expected {expected}"
                )
            alias_proof[alias] = asdict(observed)
        image_proof[service] = {
            "expected_digest": expected,
            "aliases": alias_proof,
            "artifacts": artifact_proof,
        }

    return {
        "schema_version": 1,
        "status": "passed",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository,
        "tag": tag,
        "commit": commit,
        "docker_release_run": {
            "database_id": run.get("databaseId"),
            "url": run.get("url"),
        },
        "github_release": {
            "id": release.get("id"),
            "url": release.get("html_url"),
        },
        "stable_aliases": list(aliases),
        "images": image_proof,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", default="cyeinfpro/Lumen")
    parser.add_argument("--registry-namespace", default="cyeinfpro")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        proof = verify_release(
            tag=args.tag,
            commit=args.commit,
            repository=args.repository,
            registry_namespace=args.registry_namespace,
        )
    except ReleaseProofError as exc:
        print(f"release proof failed: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    run = proof["docker_release_run"]
    assert isinstance(run, dict)
    print(
        f"release proof passed: {proof['tag']} {proof['commit']} "
        f"run={run['database_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
