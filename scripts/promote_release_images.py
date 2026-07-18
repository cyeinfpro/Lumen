#!/usr/bin/env python3
"""Publish Lumen image aliases with downgrade guards and best-effort rollback."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence


SERVICES = ("api", "worker", "tgbot", "web")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SEMVER_RE = re.compile(
    r"^v"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
MISSING_MANIFEST_MARKERS = (
    "404",
    "manifest unknown",
    "manifest_unknown",
    "no such manifest",
    "not found",
)


class PromotionError(RuntimeError):
    """Raised when aliases cannot be published safely."""


class PromotionInterrupted(PromotionError):
    """Raised when publication receives SIGINT or SIGTERM."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"publication interrupted by signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Command(Protocol):
    def run(self, args: Sequence[str]) -> CommandResult:
        """Run command arguments after the configured executable prefix."""


class SubprocessCommand:
    def __init__(self, prefix: Sequence[str]) -> None:
        if not prefix:
            raise PromotionError("command prefix cannot be empty")
        self._prefix = tuple(prefix)

    def run(self, args: Sequence[str]) -> CommandResult:
        result = subprocess.run(
            [*self._prefix, *args],
            check=False,
            capture_output=True,
            text=True,
        )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] | None

    @classmethod
    def parse(cls, tag: str) -> SemVer:
        match = SEMVER_RE.fullmatch(tag)
        if match is None:
            raise PromotionError(f"invalid release tag: {tag!r}")
        prerelease_text = match.group("prerelease")
        prerelease = (
            tuple(prerelease_text.split(".")) if prerelease_text is not None else None
        )
        if prerelease is not None:
            for identifier in prerelease:
                if (
                    identifier.isdigit()
                    and len(identifier) > 1
                    and identifier[0] == "0"
                ):
                    raise PromotionError(
                        f"numeric prerelease identifiers cannot have leading zeroes: {tag}"
                    )
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=prerelease,
        )

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    def compare(self, other: SemVer) -> int:
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return -1 if core < other_core else 1
        if self.prerelease is None:
            return 0 if other.prerelease is None else 1
        if other.prerelease is None:
            return -1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return -1 if int(left) < int(right) else 1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return -1 if left < right else 1
        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1


@dataclass(frozen=True)
class AliasPlan:
    mode: str
    release_tag: str | None
    exact_aliases: tuple[str, ...]
    mutable_aliases: tuple[str, ...]
    is_prerelease: bool
    version: SemVer | None


def build_alias_plan(mode: str, release_tag: str | None = None) -> AliasPlan:
    if mode == "main":
        if release_tag:
            raise PromotionError("main publication must not include a release tag")
        return AliasPlan(
            mode=mode,
            release_tag=None,
            exact_aliases=(),
            mutable_aliases=("main",),
            is_prerelease=False,
            version=None,
        )
    if mode != "release":
        raise PromotionError(f"unsupported publication mode: {mode!r}")
    if not release_tag:
        raise PromotionError("release publication requires --release-tag")

    version = SemVer.parse(release_tag)
    if version.is_prerelease:
        mutable_aliases: tuple[str, ...] = ()
    else:
        mutable_aliases = (
            f"v{version.major}.{version.minor}",
            f"v{version.major}",
            "latest",
        )
    return AliasPlan(
        mode=mode,
        release_tag=release_tag,
        exact_aliases=(release_tag,),
        mutable_aliases=mutable_aliases,
        is_prerelease=version.is_prerelease,
        version=version,
    )


def parse_image_digests(values: Sequence[str]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for value in values:
        service, separator, digest = value.partition("=")
        if not separator or service not in SERVICES:
            raise PromotionError(
                f"image digest must be SERVICE=sha256:..., got {value!r}"
            )
        if service in digests:
            raise PromotionError(f"duplicate image digest for {service}")
        if DIGEST_RE.fullmatch(digest) is None:
            raise PromotionError(f"invalid image digest for {service}: {digest!r}")
        digests[service] = digest
    if set(digests) != set(SERVICES):
        missing = sorted(set(SERVICES) - set(digests))
        extra = sorted(set(digests) - set(SERVICES))
        raise PromotionError(
            f"image digest set mismatch; missing={missing}, extra={extra}"
        )
    return digests


def parse_release_manifest_digests(text: str, *, tag: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PromotionError(f"release manifest for {tag} is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("version") != tag
    ):
        raise PromotionError(f"release manifest metadata mismatch for {tag}")
    images = payload.get("images")
    if not isinstance(images, dict) or set(images) != set(SERVICES):
        raise PromotionError(f"release manifest image set mismatch for {tag}")
    digests: dict[str, str] = {}
    for service in SERVICES:
        image = images.get(service)
        repository = f"ghcr.io/cyeinfpro/lumen-{service}"
        if not isinstance(image, dict):
            raise PromotionError(f"release manifest missing {service} for {tag}")
        digest = image.get("digest")
        if (
            not isinstance(digest, str)
            or DIGEST_RE.fullmatch(digest) is None
            or image.get("tag") != f"{repository}:{tag}"
            or image.get("immutable_ref") != f"{repository}@{digest}"
        ):
            raise PromotionError(
                f"release manifest image metadata mismatch for {service}:{tag}"
            )
        digests[service] = digest
    return digests


class DockerRegistry:
    def __init__(self, command: Command) -> None:
        self._command = command

    def inspect_digest(self, reference: str, *, missing_ok: bool = False) -> str | None:
        result = self._command.run(
            [
                "buildx",
                "imagetools",
                "inspect",
                reference,
                "--format",
                "{{json .Manifest}}",
            ]
        )
        if result.returncode != 0:
            error_text = f"{result.stdout}\n{result.stderr}".lower()
            if missing_ok and any(
                marker in error_text for marker in MISSING_MANIFEST_MARKERS
            ):
                return None
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise PromotionError(f"failed to inspect {reference}: {detail}")

        output = result.stdout.strip()
        try:
            manifest = json.loads(output)
        except json.JSONDecodeError as exc:
            raise PromotionError(
                f"invalid manifest inspection output for {reference}: {output!r}"
            ) from exc
        if isinstance(manifest, str):
            digest = manifest
        elif isinstance(manifest, dict):
            digest = manifest.get("digest") or manifest.get("Digest")
        else:
            digest = None
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            raise PromotionError(
                f"manifest inspection returned no valid digest for {reference}"
            )
        return digest

    def create_alias(self, image: str, alias: str, digest: str) -> None:
        target = f"{image}:{alias}"
        source = f"{image}@{digest}"
        result = self._command.run(
            ["buildx", "imagetools", "create", "--tag", target, source]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise PromotionError(f"failed to publish {target}: {detail}")


class GitHubReleaseSource:
    def __init__(self, command: Command, repository: str) -> None:
        if not repository or "/" not in repository:
            raise PromotionError("GitHub repository must use OWNER/REPO form")
        self._command = command
        self._repository = repository

    def published_tags(self) -> list[str]:
        result = self._command.run(
            [
                "api",
                "--paginate",
                f"repos/{self._repository}/releases?per_page=100",
                "--jq",
                ".[] | select(.draft == false) | .tag_name",
            ]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise PromotionError(f"failed to list published GitHub releases: {detail}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def release_manifest_digests(self, tag: str) -> dict[str, str]:
        result = self._command.run(
            [
                "release",
                "download",
                tag,
                "--repo",
                self._repository,
                "--pattern",
                "release-manifest.json",
                "--output",
                "-",
            ]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise PromotionError(
                f"failed to download release manifest for {tag}: {detail}"
            )
        return parse_release_manifest_digests(result.stdout, tag=tag)


@dataclass(frozen=True)
class AliasSnapshot:
    service: str
    alias: str
    expected_digest: str
    old_digest: str | None
    rollback_digest: str | None


class PromotionPublisher:
    def __init__(
        self,
        *,
        registry: DockerRegistry,
        registry_namespace: str,
        release_tags: Callable[[], Sequence[str]] | None = None,
        release_manifest: Callable[[str], dict[str, str]] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._registry = registry
        self._namespace = registry_namespace.rstrip("/")
        if not self._namespace:
            raise PromotionError("registry namespace cannot be empty")
        self._release_tags = release_tags
        self._release_manifest = release_manifest
        self._log = log or (lambda message: print(message, file=sys.stderr))

    def _image(self, service: str) -> str:
        return f"{self._namespace}/lumen-{service}"

    def _ensure_stable_not_downgrade(self, plan: AliasPlan) -> None:
        if plan.version is None or plan.version.is_prerelease:
            return
        if self._release_tags is None:
            raise PromotionError("stable publication requires a GitHub release source")

        newer: list[tuple[SemVer, str]] = []
        for tag in self._release_tags():
            try:
                existing = SemVer.parse(tag)
            except PromotionError:
                continue
            if existing.is_prerelease:
                continue
            if plan.version.compare(existing) < 0:
                newer.append((existing, tag))
        if newer:
            newest = newer[0]
            for candidate in newer[1:]:
                if newest[0].compare(candidate[0]) < 0:
                    newest = candidate
            raise PromotionError(
                f"refusing stable alias downgrade from {newest[1]} "
                f"to {plan.release_tag}"
            )

    def _preflight_immutable_digests(self, digests: dict[str, str]) -> None:
        for service in SERVICES:
            digest = digests[service]
            reference = f"{self._image(service)}@{digest}"
            actual = self._registry.inspect_digest(reference)
            if actual != digest:
                raise PromotionError(
                    f"immutable digest preflight mismatch for {service}: "
                    f"{actual} != {digest}"
                )

    def _snapshots(
        self, aliases: Sequence[str], digests: dict[str, str]
    ) -> list[AliasSnapshot]:
        snapshots: list[AliasSnapshot] = []
        for service in SERVICES:
            image = self._image(service)
            for alias in aliases:
                old_digest = self._registry.inspect_digest(
                    f"{image}:{alias}", missing_ok=True
                )
                snapshots.append(
                    AliasSnapshot(
                        service=service,
                        alias=alias,
                        expected_digest=digests[service],
                        old_digest=old_digest,
                        rollback_digest=old_digest,
                    )
                )
        return snapshots

    def _prepare_mutable_rollback(
        self, plan: AliasPlan, snapshots: Sequence[AliasSnapshot]
    ) -> list[AliasSnapshot]:
        missing = [
            f"{snapshot.service}:{snapshot.alias}"
            for snapshot in snapshots
            if snapshot.old_digest is None
        ]
        if missing:
            raise PromotionError(
                "mutable aliases have no complete rollback baseline and registry "
                "deletion is unavailable; refusing first/partial publication "
                "before writes: " + ", ".join(missing)
            )

        if plan.mode == "main":
            return list(snapshots)

        if plan.version is None or plan.version.is_prerelease:
            return list(snapshots)
        if self._release_tags is None or self._release_manifest is None:
            raise PromotionError(
                "stable mutable publication requires release manifests"
            )

        candidates: list[tuple[SemVer, str]] = []
        for tag in self._release_tags():
            try:
                version = SemVer.parse(tag)
            except PromotionError:
                continue
            if version.is_prerelease or version.compare(plan.version) >= 0:
                continue
            candidates.append((version, tag))
        candidates.sort(
            key=lambda item: (
                item[0].major,
                item[0].minor,
                item[0].patch,
            ),
            reverse=True,
        )
        manifest_cache: dict[str, dict[str, str]] = {}
        prepared: list[AliasSnapshot] = []
        for alias in plan.mutable_aliases:
            alias_snapshots = [
                snapshot for snapshot in snapshots if snapshot.alias == alias
            ]
            rollback_manifest: dict[str, str] | None = None
            rollback_tag = ""
            existing = [
                snapshot
                for snapshot in alias_snapshots
                if snapshot.old_digest is not None
            ]
            if existing and all(
                snapshot.old_digest == snapshot.expected_digest for snapshot in existing
            ):
                rollback_manifest = {
                    snapshot.service: snapshot.expected_digest
                    for snapshot in alias_snapshots
                }
                rollback_tag = plan.release_tag or "target"
            for _, candidate_tag in candidates:
                if rollback_manifest is not None:
                    break
                manifest = manifest_cache.get(candidate_tag)
                if manifest is None:
                    manifest = self._release_manifest(candidate_tag)
                    manifest_cache[candidate_tag] = manifest
                if all(
                    snapshot.old_digest == manifest[snapshot.service]
                    for snapshot in alias_snapshots
                ):
                    rollback_manifest = manifest
                    rollback_tag = candidate_tag
                    break
            if rollback_manifest is None:
                raise PromotionError(
                    f"alias {alias} has no complete prior release manifest "
                    "matching its current state; refusing writes"
                )
            self._log(f"rollback baseline for {alias}: {rollback_tag}")
            prepared.extend(alias_snapshots)
        return prepared

    def _verify(self, snapshots: Sequence[AliasSnapshot]) -> None:
        for snapshot in snapshots:
            reference = f"{self._image(snapshot.service)}:{snapshot.alias}"
            actual = self._registry.inspect_digest(reference)
            if actual != snapshot.expected_digest:
                raise PromotionError(
                    f"{reference} resolved to {actual}, "
                    f"expected {snapshot.expected_digest}"
                )

    def _publish_exact(
        self, aliases: Sequence[str], digests: dict[str, str]
    ) -> list[AliasSnapshot]:
        snapshots = self._snapshots(aliases, digests)
        conflicts = [
            snapshot
            for snapshot in snapshots
            if snapshot.old_digest is not None
            and snapshot.old_digest != snapshot.expected_digest
        ]
        if conflicts:
            details = ", ".join(
                f"{snapshot.service}:{snapshot.alias}={snapshot.old_digest}"
                for snapshot in conflicts
            )
            raise PromotionError(
                f"exact release tags are immutable and already conflict: {details}"
            )

        created: list[str] = []
        try:
            for snapshot in snapshots:
                if snapshot.old_digest == snapshot.expected_digest:
                    continue
                image = self._image(snapshot.service)
                self._registry.create_alias(
                    image, snapshot.alias, snapshot.expected_digest
                )
                created.append(f"{image}:{snapshot.alias}")
            self._verify(snapshots)
        except BaseException:
            if created:
                self._log(
                    "exact-tag publication failed after creating "
                    f"{', '.join(created)}; exact tags are retained for safe retry "
                    "and are never deleted as rollback"
                )
            raise
        return snapshots

    def _rollback(self, snapshots: Sequence[AliasSnapshot]) -> None:
        failures: list[str] = []
        restored: set[tuple[str, str]] = set()
        for snapshot in reversed(snapshots):
            key = (snapshot.service, snapshot.alias)
            if key in restored:
                continue
            restored.add(key)
            reference = f"{self._image(snapshot.service)}:{snapshot.alias}"
            rollback_digest = snapshot.rollback_digest
            if rollback_digest is None:
                failures.append(f"{reference}: no rollback digest")
                continue
            try:
                last_error: BaseException | None = None
                for _attempt in range(3):
                    try:
                        self._registry.create_alias(
                            self._image(snapshot.service),
                            snapshot.alias,
                            rollback_digest,
                        )
                        actual = self._registry.inspect_digest(reference)
                        if actual != rollback_digest:
                            raise PromotionError(
                                f"rollback verification returned {actual}, "
                                f"expected {rollback_digest}"
                            )
                        last_error = None
                        break
                    except BaseException as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error
                self._log(f"rollback restored {reference} to {rollback_digest}")
            except BaseException as exc:
                failures.append(f"{reference}: {exc}")

        if failures:
            self._log(
                "rollback was incomplete; manual reconciliation required: "
                + "; ".join(failures)
            )

    def _publish_mutable(
        self, plan: AliasPlan, aliases: Sequence[str], digests: dict[str, str]
    ) -> list[AliasSnapshot]:
        snapshots = self._prepare_mutable_rollback(
            plan, self._snapshots(aliases, digests)
        )
        self._ensure_stable_not_downgrade(plan)
        old_handlers: dict[int, signal.Handlers] = {}
        handlers_restored = False

        def restore_handlers() -> None:
            nonlocal handlers_restored
            if handlers_restored:
                return
            handlers_restored = True
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)

        def interrupt(signum: int, _frame: object) -> None:
            raise PromotionInterrupted(signum)

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)

        try:
            for snapshot in snapshots:
                if snapshot.old_digest == snapshot.expected_digest:
                    continue
                self._registry.create_alias(
                    self._image(snapshot.service),
                    snapshot.alias,
                    snapshot.expected_digest,
                )
                self._verify((snapshot,))
            self._verify(snapshots)
        except BaseException:
            restore_handlers()
            self._log(
                "mutable alias publication failed; OCI registries do not provide "
                "a multi-tag transaction, starting best-effort rollback"
            )
            self._rollback(snapshots)
            raise
        finally:
            restore_handlers()
        return snapshots

    def publish(
        self, plan: AliasPlan, digests: dict[str, str], *, phase: str = "all"
    ) -> None:
        if phase not in {"all", "exact", "mutable"}:
            raise PromotionError(f"unsupported publication phase: {phase!r}")
        self._preflight_immutable_digests(digests)

        exact_snapshots: list[AliasSnapshot] = []
        mutable_snapshots: list[AliasSnapshot] = []
        if phase == "exact" and not plan.exact_aliases:
            raise PromotionError(
                f"exact publication phase resolved no aliases for mode {plan.mode}"
            )
        if phase == "mutable" and not plan.mutable_aliases:
            raise PromotionError(
                f"mutable publication phase resolved no aliases for mode {plan.mode}"
            )
        if phase in {"all", "exact"} and plan.exact_aliases:
            exact_snapshots = self._publish_exact(plan.exact_aliases, digests)
        elif phase == "mutable" and plan.exact_aliases:
            exact_snapshots = self._snapshots(plan.exact_aliases, digests)
            self._verify(exact_snapshots)
        if phase in {"all", "mutable"} and plan.mutable_aliases:
            mutable_snapshots = self._publish_mutable(
                plan, plan.mutable_aliases, digests
            )

        self._verify([*exact_snapshots, *mutable_snapshots])


def _command_prefix(value: str) -> tuple[str, ...]:
    prefix = tuple(shlex.split(value))
    if not prefix:
        raise PromotionError("command cannot be empty")
    return prefix


def _write_github_output(path: Path, plan: AliasPlan, phase: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"is_prerelease={'true' if plan.is_prerelease else 'false'}\n")
        handle.write(f"publication_mode={plan.mode}\n")
        handle.write(f"publication_phase={phase}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("main", "release"), required=True)
    parser.add_argument(
        "--phase",
        choices=("all", "exact", "mutable"),
        default="all",
        help="Publish every planned alias or only one release phase.",
    )
    parser.add_argument("--release-tag")
    parser.add_argument("--registry", required=True, help="Registry namespace")
    parser.add_argument("--github-repository")
    parser.add_argument(
        "--image-digest",
        action="append",
        default=[],
        metavar="SERVICE=SHA256",
        help="Immutable digest for one service; required for all four services.",
    )
    parser.add_argument(
        "--docker-command",
        default=os.environ.get("LUMEN_PROMOTION_DOCKER_COMMAND", "docker"),
        help="Docker command prefix; replace with a fake command in behavior tests.",
    )
    parser.add_argument(
        "--github-command",
        default=os.environ.get("LUMEN_PROMOTION_GITHUB_COMMAND", "gh"),
        help="GitHub command prefix; replace with a fake command in behavior tests.",
    )
    parser.add_argument("--github-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        plan = build_alias_plan(args.mode, args.release_tag)
        digests = parse_image_digests(args.image_digest)
        docker = DockerRegistry(SubprocessCommand(_command_prefix(args.docker_command)))
        release_source: GitHubReleaseSource | None = None
        if (
            plan.version is not None
            and not plan.is_prerelease
            and args.phase in {"all", "mutable"}
        ):
            release_source = GitHubReleaseSource(
                SubprocessCommand(_command_prefix(args.github_command)),
                args.github_repository or "",
            )
        publisher = PromotionPublisher(
            registry=docker,
            registry_namespace=args.registry,
            release_tags=(
                release_source.published_tags if release_source is not None else None
            ),
            release_manifest=(
                release_source.release_manifest_digests
                if release_source is not None
                else None
            ),
        )
        publisher.publish(plan, digests, phase=args.phase)
        if args.github_output is not None:
            _write_github_output(args.github_output, plan, args.phase)
    except PromotionInterrupted as exc:
        print(f"image promotion interrupted: {exc}", file=sys.stderr)
        return 128 + exc.signum
    except (OSError, PromotionError) as exc:
        print(f"image promotion failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("image promotion interrupted", file=sys.stderr)
        return 130

    aliases: list[str] = []
    if args.phase in {"all", "exact"}:
        aliases.extend(plan.exact_aliases)
    if args.phase in {"all", "mutable"}:
        aliases.extend(plan.mutable_aliases)
    print(
        "image promotion complete: "
        f"mode={plan.mode} phase={args.phase} aliases={','.join(aliases)} "
        f"services={','.join(SERVICES)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
