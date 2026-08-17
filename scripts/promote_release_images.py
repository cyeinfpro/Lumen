#!/usr/bin/env python3
"""Publish Lumen image aliases with downgrade guards and best-effort rollback."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote

try:
    from .release_alias_plan import (
        ISOLATED_ALIAS_NAME_ANNOTATION,
        ISOLATED_ALIAS_SOURCE_ANNOTATION,
        MISSING_MANIFEST_MARKERS,
        SEMVER_RE,
        AliasPlan,
        Command,
        CommandResult,
        PromotionError,
        PromotionInterrupted,
        SemVer,
        SubprocessCommand,
        build_alias_plan,
    )
except ImportError:
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from release_alias_plan import (
        ISOLATED_ALIAS_NAME_ANNOTATION,
        ISOLATED_ALIAS_SOURCE_ANNOTATION,
        MISSING_MANIFEST_MARKERS,
        SEMVER_RE,
        AliasPlan,
        Command,
        CommandResult,
        PromotionError,
        PromotionInterrupted,
        SemVer,
        SubprocessCommand,
        build_alias_plan,
    )


SERVICES = ("api", "worker", "tgbot", "web")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

__all__ = [
    "AliasPlan",
    "Command",
    "CommandResult",
    "PromotionError",
    "PromotionInterrupted",
    "SemVer",
    "SubprocessCommand",
    "build_alias_plan",
]


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
    def __init__(
        self,
        command: Command,
        *,
        delete_tag: Callable[[str, str], None] | None = None,
    ) -> None:
        self._command = command
        self._delete_tag = delete_tag or self._deletion_unavailable

    @staticmethod
    def _deletion_unavailable(reference: str, _digest: str) -> None:
        raise PromotionError(
            f"registry tag deletion is unavailable for rollback of {reference}"
        )

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

    def create_isolated_alias(self, image: str, alias: str, digest: str) -> str:
        target = f"{image}:{alias}"
        source = f"{image}@{digest}"
        result = self._command.run(
            [
                "buildx",
                "imagetools",
                "create",
                "--annotation",
                f"index:{ISOLATED_ALIAS_SOURCE_ANNOTATION}={digest}",
                "--annotation",
                f"index:{ISOLATED_ALIAS_NAME_ANNOTATION}={alias}",
                "--tag",
                target,
                source,
            ]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise PromotionError(f"failed to publish isolated alias {target}: {detail}")
        actual = self.inspect_digest(target)
        if actual == digest:
            raise PromotionError(
                f"isolated alias {target} reused immutable digest {digest}"
            )
        return actual

    def inspect_release_digest(self, reference: str, digest: str) -> str:
        result = self._command.run(
            ["buildx", "imagetools", "inspect", reference, "--raw"]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise PromotionError(
                f"failed to inspect alias metadata for {reference}: {detail}"
            )
        try:
            manifest = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PromotionError(
                f"invalid raw manifest inspection output for {reference}"
            ) from exc
        annotations = (
            manifest.get("annotations") if isinstance(manifest, dict) else None
        )
        source_digest = (
            annotations.get(ISOLATED_ALIAS_SOURCE_ANNOTATION)
            if isinstance(annotations, dict)
            else None
        )
        if source_digest is None:
            return digest
        if (
            not isinstance(source_digest, str)
            or DIGEST_RE.fullmatch(source_digest) is None
        ):
            raise PromotionError(
                f"alias metadata for {reference} has invalid source digest"
            )
        return source_digest

    def delete_alias(self, image: str, alias: str, digest: str) -> None:
        if SEMVER_RE.fullmatch(alias) is not None:
            raise PromotionError(
                f"refusing to delete immutable exact tag {image}:{alias}"
            )
        self._delete_tag(f"{image}:{alias}", digest)


class GitHubPackageTagDeleter:
    def __init__(self, command: Command, owner: str) -> None:
        if not owner:
            raise PromotionError("GitHub package owner cannot be empty")
        self._command = command
        self._owner = owner

    def _package_versions(self, package: str) -> tuple[str, list[dict[str, object]]]:
        encoded_package = quote(package, safe="")
        failures: list[str] = []
        for owner_kind in ("users", "orgs"):
            endpoint = (
                f"{owner_kind}/{self._owner}/packages/container/"
                f"{encoded_package}/versions?per_page=100"
            )
            result = self._command.run(
                ["api", "--paginate", endpoint, "--jq", ".[] | @json"]
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                failures.append(f"{owner_kind}: {detail or result.returncode}")
                continue
            versions: list[dict[str, object]] = []
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    version = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PromotionError(
                        f"invalid GitHub package version response for {package}"
                    ) from exc
                if not isinstance(version, dict):
                    raise PromotionError(
                        f"invalid GitHub package version entry for {package}"
                    )
                versions.append(version)
            return owner_kind, versions
        raise PromotionError(
            f"failed to list GitHub package versions for {package}: "
            + "; ".join(failures)
        )

    def delete(self, reference: str, digest: str) -> None:
        image, separator, alias = reference.rpartition(":")
        if not separator or not image or not alias:
            raise PromotionError(f"invalid package alias reference: {reference!r}")
        registry, path_separator, package_path = image.partition("/")
        if registry != "ghcr.io" or not path_separator:
            raise PromotionError(
                f"GitHub package deletion requires a ghcr.io image: {image}"
            )
        owner, owner_separator, package = package_path.partition("/")
        if not owner_separator or owner != self._owner or not package:
            raise PromotionError(
                f"image {image} is outside GitHub package owner {self._owner}"
            )

        owner_kind, versions = self._package_versions(package)
        matches: list[tuple[object, list[object]]] = []
        for version in versions:
            metadata = version.get("metadata")
            container = (
                metadata.get("container") if isinstance(metadata, dict) else None
            )
            tags = container.get("tags") if isinstance(container, dict) else None
            if (
                version.get("name") == digest
                and isinstance(tags, list)
                and alias in tags
            ):
                matches.append((version.get("id"), tags))
        if len(matches) != 1:
            raise PromotionError(
                f"expected one GitHub package version for {reference}@{digest}, "
                f"found {len(matches)}"
            )
        version_id, tags = matches[0]
        if not isinstance(version_id, int):
            raise PromotionError(
                f"GitHub package version for {reference} has invalid id"
            )
        if tags != [alias]:
            raise PromotionError(
                f"refusing to delete shared package version for {reference}; "
                f"tags={tags}"
            )

        endpoint = (
            f"{owner_kind}/{self._owner}/packages/container/"
            f"{quote(package, safe='')}/versions/{version_id}"
        )
        result = self._command.run(["api", "--method", "DELETE", endpoint])
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise PromotionError(
                f"failed to delete GitHub package version for {reference}: {detail}"
            )


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
        tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if tags:
            return tags

        # GitHub's REST release collection can transiently return an empty
        # 200 response while its pagination metadata still advertises releases.
        # The CLI release list uses GraphQL and avoids that stale REST edge cache.
        fallback = self._command.run(
            [
                "release",
                "list",
                "--repo",
                self._repository,
                "--limit",
                "1000",
                "--json",
                "tagName,isDraft",
                "--jq",
                ".[] | select(.isDraft == false) | .tagName",
            ]
        )
        if fallback.returncode != 0:
            detail = (
                fallback.stderr.strip()
                or fallback.stdout.strip()
                or fallback.returncode
            )
            raise PromotionError(
                f"failed to list published GitHub releases via fallback: {detail}"
            )
        return [line.strip() for line in fallback.stdout.splitlines() if line.strip()]

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
    old_release_digest: str | None
    rollback_digest: str | None
    published_digest: str | None = None


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
                old_release_digest = old_digest
                if old_digest is not None and old_digest != digests[service]:
                    old_release_digest = self._registry.inspect_release_digest(
                        f"{image}:{alias}",
                        old_digest,
                    )
                snapshots.append(
                    AliasSnapshot(
                        service=service,
                        alias=alias,
                        expected_digest=digests[service],
                        old_digest=old_digest,
                        old_release_digest=old_release_digest,
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
        if missing and plan.mode == "main":
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
            if not existing:
                rollback_manifest = {}
                rollback_tag = "absent"
            elif all(
                snapshot.old_release_digest == snapshot.expected_digest
                for snapshot in existing
            ):
                rollback_manifest = {
                    snapshot.service: snapshot.expected_digest for snapshot in existing
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
                    snapshot.old_release_digest == manifest[snapshot.service]
                    for snapshot in existing
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
            expected = snapshot.published_digest or snapshot.expected_digest
            if actual != expected:
                raise PromotionError(
                    f"{reference} resolved to {actual}, expected {expected}"
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
            try:
                last_error: BaseException | None = None
                for _attempt in range(3):
                    try:
                        current = self._registry.inspect_digest(
                            reference,
                            missing_ok=True,
                        )
                        if current == rollback_digest:
                            last_error = None
                            break
                        if rollback_digest is not None and (
                            current != snapshot.expected_digest
                        ):
                            raise PromotionError(
                                f"rollback found unexpected current digest {current}; "
                                f"expected promoted digest {snapshot.expected_digest}"
                            )
                        if rollback_digest is None:
                            self._registry.delete_alias(
                                self._image(snapshot.service),
                                snapshot.alias,
                                current,
                            )
                        else:
                            self._registry.create_alias(
                                self._image(snapshot.service),
                                snapshot.alias,
                                rollback_digest,
                            )
                        actual = self._registry.inspect_digest(
                            reference,
                            missing_ok=rollback_digest is None,
                        )
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
                if rollback_digest is None:
                    self._log(f"rollback deleted newly created alias {reference}")
                else:
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
        published: list[AliasSnapshot] = []
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

        def suppress_interrupts() -> None:
            for signum in old_handlers:
                signal.signal(signum, signal.SIG_IGN)

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)

        try:
            for snapshot in snapshots:
                if snapshot.old_digest == snapshot.expected_digest:
                    published.append(
                        replace(
                            snapshot,
                            published_digest=snapshot.expected_digest,
                        )
                    )
                    continue
                if snapshot.old_digest is None:
                    # GHCR deletes package versions rather than individual tags.
                    # A wrapper keeps this new alias independently removable.
                    published_digest = self._registry.create_isolated_alias(
                        self._image(snapshot.service),
                        snapshot.alias,
                        snapshot.expected_digest,
                    )
                else:
                    self._registry.create_alias(
                        self._image(snapshot.service),
                        snapshot.alias,
                        snapshot.expected_digest,
                    )
                    published_digest = snapshot.expected_digest
                published_snapshot = replace(
                    snapshot,
                    published_digest=published_digest,
                )
                published.append(published_snapshot)
                self._verify((published_snapshot,))
            self._verify(published)
        except BaseException:
            suppress_interrupts()
            self._log(
                "mutable alias publication failed; OCI registries do not provide "
                "a multi-tag transaction, starting best-effort rollback"
            )
            self._rollback(snapshots)
            raise
        finally:
            restore_handlers()
        return published

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
        release_source: GitHubReleaseSource | None = None
        package_deleter: GitHubPackageTagDeleter | None = None
        if (
            plan.version is not None
            and not plan.is_prerelease
            and args.phase in {"all", "mutable"}
        ):
            repository = args.github_repository or ""
            github_command = SubprocessCommand(_command_prefix(args.github_command))
            release_source = GitHubReleaseSource(
                github_command,
                repository,
            )
            owner, _, _ = repository.partition("/")
            package_deleter = GitHubPackageTagDeleter(github_command, owner)
        docker = DockerRegistry(
            SubprocessCommand(_command_prefix(args.docker_command)),
            delete_tag=(
                package_deleter.delete if package_deleter is not None else None
            ),
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
