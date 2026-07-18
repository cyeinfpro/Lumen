from __future__ import annotations

import importlib.util
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "promote_release_images.py"
REGISTRY = "ghcr.io/cyeinfpro"
SERVICES = ("api", "worker", "tgbot", "web")

spec = importlib.util.spec_from_file_location("promote_release_images", SCRIPT)
assert spec is not None and spec.loader is not None
promotion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = promotion
spec.loader.exec_module(promotion)

AliasPlan = promotion.AliasPlan
CommandResult = promotion.CommandResult
DockerRegistry = promotion.DockerRegistry
GitHubReleaseSource = promotion.GitHubReleaseSource
PromotionError = promotion.PromotionError
PromotionInterrupted = promotion.PromotionInterrupted
PromotionPublisher = promotion.PromotionPublisher
build_alias_plan = promotion.build_alias_plan


def _digest(value: int) -> str:
    return f"sha256:{value:064x}"


def _digests(offset: int = 0) -> dict[str, str]:
    return {
        service: _digest(index + offset)
        for index, service in enumerate(SERVICES, start=1)
    }


class FakeDockerCommand:
    def __init__(
        self,
        state: dict[str, str],
        *,
        fail_target: str | None = None,
        interrupt_target: str | None = None,
    ) -> None:
        self.state = state
        self.fail_target = fail_target
        self.interrupt_target = interrupt_target
        self.failure_consumed = False
        self.commands: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str]) -> CommandResult:
        command = tuple(args)
        self.commands.append(command)
        if command[:3] == ("buildx", "imagetools", "inspect"):
            reference = command[3]
            digest = self.state.get(reference)
            if digest is None:
                return CommandResult(1, "", "manifest unknown: not found")
            return CommandResult(0, json.dumps({"digest": digest}), "")

        if command[:3] == ("buildx", "imagetools", "create"):
            assert command[3] == "--tag"
            target = command[4]
            source = command[5]
            digest = source.rsplit("@", maxsplit=1)[1]
            self.state[target] = digest
            if target == self.interrupt_target and not self.failure_consumed:
                self.failure_consumed = True
                raise PromotionInterrupted(signal.SIGTERM)
            if target == self.fail_target and not self.failure_consumed:
                self.failure_consumed = True
                return CommandResult(1, "", "simulated registry write failure")
            return CommandResult(0, "", "")

        raise AssertionError(f"unexpected fake docker command: {command}")


class FakeGitHubCommand:
    def __init__(
        self,
        tags: Sequence[str],
        manifests: dict[str, str] | None = None,
    ) -> None:
        self.tags = tuple(tags)
        self.manifests = manifests or {}
        self.commands: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str]) -> CommandResult:
        command = tuple(args)
        self.commands.append(command)
        if command[:2] == ("api", "--paginate"):
            return CommandResult(0, "\n".join(self.tags) + "\n", "")
        if command[:2] == ("release", "download"):
            tag = command[2]
            manifest = self.manifests.get(tag)
            if manifest is None:
                return CommandResult(1, "", "manifest missing")
            return CommandResult(0, manifest, "")
        raise AssertionError(f"unexpected fake GitHub command: {command}")


def _immutable_state(digests: dict[str, str]) -> dict[str, str]:
    return {
        f"{REGISTRY}/lumen-{service}@{digest}": digest
        for service, digest in digests.items()
    }


def _manifest(tag: str, digests: dict[str, str]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "version": tag,
            "images": {
                service: {
                    "tag": f"{REGISTRY}/lumen-{service}:{tag}",
                    "digest": digest,
                    "immutable_ref": f"{REGISTRY}/lumen-{service}@{digest}",
                }
                for service, digest in digests.items()
            },
        }
    )


def _publisher(
    docker: FakeDockerCommand,
    *,
    release_tags: Sequence[str] = (),
    release_manifests: dict[str, dict[str, str]] | None = None,
    logs: list[str] | None = None,
) -> PromotionPublisher:
    github = FakeGitHubCommand(
        release_tags,
        {
            tag: _manifest(tag, digests)
            for tag, digests in (release_manifests or {}).items()
        },
    )
    source = GitHubReleaseSource(github, "cyeinfpro/Lumen")
    return PromotionPublisher(
        registry=DockerRegistry(docker),
        registry_namespace=REGISTRY,
        release_tags=source.published_tags,
        release_manifest=source.release_manifest_digests,
        log=(logs.append if logs is not None else None),
    )


@pytest.mark.parametrize(
    ("mode", "release_tag", "exact", "mutable", "is_prerelease"),
    [
        ("main", None, (), ("main",), False),
        (
            "release",
            "v1.2.3",
            ("v1.2.3",),
            ("v1.2", "v1", "latest"),
            False,
        ),
        ("release", "v1.3.0-rc.1", ("v1.3.0-rc.1",), (), True),
    ],
)
def test_alias_plan(
    mode: str,
    release_tag: str | None,
    exact: tuple[str, ...],
    mutable: tuple[str, ...],
    is_prerelease: bool,
) -> None:
    plan = build_alias_plan(mode, release_tag)

    assert plan == AliasPlan(
        mode=mode,
        release_tag=release_tag,
        exact_aliases=exact,
        mutable_aliases=mutable,
        is_prerelease=is_prerelease,
        version=plan.version,
    )


def test_shared_alias_failure_restores_every_modified_service_alias() -> None:
    new_digests = _digests()
    old_digests = _digests(100)
    state = _immutable_state(new_digests)
    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        state[f"{image}:v1.2.65"] = new_digests[service]
        for alias in ("v1.2", "v1", "latest"):
            state[f"{image}:{alias}"] = old_digests[service]

    fail_target = f"{REGISTRY}/lumen-worker:v1.2"
    docker = FakeDockerCommand(state, fail_target=fail_target)
    logs: list[str] = []
    publisher = _publisher(
        docker,
        release_tags=("v1.2.64",),
        release_manifests={"v1.2.64": old_digests},
        logs=logs,
    )

    with pytest.raises(PromotionError, match="simulated registry write failure"):
        publisher.publish(
            build_alias_plan("release", "v1.2.65"),
            new_digests,
            phase="mutable",
        )

    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        assert state[f"{image}:v1.2.65"] == new_digests[service]
        for alias in ("v1.2", "v1", "latest"):
            assert state[f"{image}:{alias}"] == old_digests[service]
    assert any("do not provide a multi-tag transaction" in line for line in logs)
    assert any("rollback restored" in line for line in logs)


def test_release_failure_after_exact_phase_leaves_shared_aliases_unchanged() -> None:
    new_digests = _digests()
    old_digests = _digests(300)
    state = _immutable_state(new_digests)
    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        for alias in ("v1.2", "v1", "latest"):
            state[f"{image}:{alias}"] = old_digests[service]

    docker = FakeDockerCommand(state)
    publisher = _publisher(docker)
    publisher.publish(
        build_alias_plan("release", "v1.2.65"),
        new_digests,
        phase="exact",
    )

    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        assert state[f"{image}:v1.2.65"] == new_digests[service]
        for alias in ("v1.2", "v1", "latest"):
            assert state[f"{image}:{alias}"] == old_digests[service]


def test_interrupt_uses_the_same_mutable_alias_rollback_path() -> None:
    new_digests = _digests()
    old_digests = _digests(200)
    state = _immutable_state(new_digests)
    for service in SERVICES:
        state[f"{REGISTRY}/lumen-{service}:main"] = old_digests[service]

    docker = FakeDockerCommand(
        state,
        interrupt_target=f"{REGISTRY}/lumen-worker:main",
    )
    publisher = _publisher(docker)

    with pytest.raises(PromotionInterrupted):
        publisher.publish(build_alias_plan("main"), new_digests)

    for service in SERVICES:
        assert state[f"{REGISTRY}/lumen-{service}:main"] == old_digests[service]


def test_stable_release_refuses_to_downgrade_shared_aliases() -> None:
    digests = _digests()
    old_digests = _digests(100)
    state = _immutable_state(digests)
    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        state[f"{image}:v1.2.65"] = digests[service]
        for alias in ("v1.2", "v1", "latest"):
            state[f"{image}:{alias}"] = old_digests[service]
    docker = FakeDockerCommand(state)
    publisher = _publisher(
        docker,
        release_tags=("v1.2.64", "v1.2.66", "v1.3.0-rc.1"),
        release_manifests={"v1.2.64": old_digests},
    )

    with pytest.raises(PromotionError, match="refusing stable alias downgrade"):
        publisher.publish(
            build_alias_plan("release", "v1.2.65"),
            digests,
            phase="mutable",
        )

    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )


def test_shared_phase_rechecks_downgrade_guard_immediately_before_writes() -> None:
    digests = _digests()
    old_digests = _digests(100)
    state = _immutable_state(digests)
    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        state[f"{image}:v1.2.65"] = digests[service]
        for alias in ("v1.2", "v1", "latest"):
            state[f"{image}:{alias}"] = old_digests[service]
    docker = FakeDockerCommand(state)
    release_tag_snapshots = iter(
        [
            ("v1.2.64", "v1.2.65"),
            ("v1.2.64", "v1.2.65", "v1.2.66"),
        ]
    )
    publisher = PromotionPublisher(
        registry=DockerRegistry(docker),
        registry_namespace=REGISTRY,
        release_tags=lambda: next(release_tag_snapshots),
        release_manifest=lambda tag: (
            old_digests
            if tag == "v1.2.64"
            else pytest.fail(f"unexpected manifest tag: {tag}")
        ),
    )

    with pytest.raises(PromotionError, match="refusing stable alias downgrade"):
        publisher.publish(
            build_alias_plan("release", "v1.2.65"),
            digests,
            phase="mutable",
        )

    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )


def test_all_immutable_digests_are_preflighted_before_any_alias_write() -> None:
    digests = _digests()
    state = _immutable_state(digests)
    del state[f"{REGISTRY}/lumen-web@{digests['web']}"]
    docker = FakeDockerCommand(state)
    publisher = _publisher(docker)

    with pytest.raises(PromotionError, match="failed to inspect"):
        publisher.publish(build_alias_plan("main"), digests)

    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )


def test_existing_conflicting_exact_release_tag_is_never_overwritten() -> None:
    digests = _digests()
    state = _immutable_state(digests)
    state[f"{REGISTRY}/lumen-worker:v1.2.65"] = _digest(999)
    docker = FakeDockerCommand(state)
    publisher = _publisher(docker)

    with pytest.raises(PromotionError, match="exact release tags are immutable"):
        publisher.publish(
            build_alias_plan("release", "v1.2.65"),
            digests,
            phase="exact",
        )

    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )
    assert state[f"{REGISTRY}/lumen-worker:v1.2.65"] == _digest(999)


def test_exact_release_tag_still_publishes_when_newer_release_exists() -> None:
    digests = _digests()
    state = _immutable_state(digests)
    docker = FakeDockerCommand(state)
    publisher = _publisher(docker, release_tags=("v1.2.66",))

    publisher.publish(
        build_alias_plan("release", "v1.2.65"),
        digests,
        phase="exact",
    )

    for service in SERVICES:
        assert state[f"{REGISTRY}/lumen-{service}:v1.2.65"] == digests[service]


def test_mutable_release_phase_requires_verified_exact_tags() -> None:
    digests = _digests()
    state = _immutable_state(digests)
    docker = FakeDockerCommand(state)
    publisher = _publisher(docker)

    with pytest.raises(PromotionError, match="failed to inspect"):
        publisher.publish(
            build_alias_plan("release", "v1.2.65"),
            digests,
            phase="mutable",
        )

    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )


def test_prerelease_publishes_only_exact_tags() -> None:
    digests = _digests()
    state = _immutable_state(digests)
    docker = FakeDockerCommand(state)
    publisher = _publisher(docker, release_tags=("v9.0.0",))

    publisher.publish(
        build_alias_plan("release", "v1.3.0-rc.1"),
        digests,
        phase="exact",
    )

    aliases = {
        reference.rsplit(":", maxsplit=1)[1]
        for reference in state
        if "@sha256:" not in reference and ":v1.3.0-rc.1" in reference
    }
    assert aliases == {"v1.3.0-rc.1"}
    assert not any(reference.endswith(":latest") for reference in state)


def test_first_main_alias_is_rejected_before_any_registry_write() -> None:
    digests = _digests()
    state = _immutable_state(digests)
    docker = FakeDockerCommand(state)
    publisher = _publisher(docker)

    with pytest.raises(PromotionError, match="no complete rollback baseline"):
        publisher.publish(build_alias_plan("main"), digests)

    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )


def test_first_new_major_refuses_absent_mutable_aliases_before_writes() -> None:
    new_digests = _digests()
    old_digests = _digests(500)
    state = _immutable_state(new_digests)
    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        state[f"{image}:v2.0.0"] = new_digests[service]
        state[f"{image}:latest"] = old_digests[service]

    docker = FakeDockerCommand(
        state,
        fail_target=f"{REGISTRY}/lumen-worker:v2.0",
    )
    publisher = _publisher(
        docker,
        release_tags=("v1.9.9", "v2.0.0"),
        release_manifests={"v1.9.9": old_digests},
    )

    with pytest.raises(PromotionError, match="registry deletion is unavailable"):
        publisher.publish(
            build_alias_plan("release", "v2.0.0"),
            new_digests,
            phase="mutable",
        )

    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )
    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        assert state[f"{image}:v2.0.0"] == new_digests[service]
        assert f"{image}:v2.0" not in state
        assert f"{image}:v2" not in state
        assert state[f"{image}:latest"] == old_digests[service]


def test_partial_mutable_alias_baseline_is_rejected_before_any_write() -> None:
    new_digests = _digests()
    old_digests = _digests(600)
    state = _immutable_state(new_digests)
    for service in SERVICES:
        image = f"{REGISTRY}/lumen-{service}"
        state[f"{image}:v1.2.65"] = new_digests[service]
        for alias in ("v1.2", "v1", "latest"):
            state[f"{image}:{alias}"] = old_digests[service]
    del state[f"{REGISTRY}/lumen-worker:v1"]
    before = state.copy()

    docker = FakeDockerCommand(
        state,
        fail_target=f"{REGISTRY}/lumen-api:v1.2",
    )
    publisher = _publisher(
        docker,
        release_tags=("v1.2.64", "v1.2.65"),
        release_manifests={"v1.2.64": old_digests},
    )

    with pytest.raises(PromotionError, match="worker:v1"):
        publisher.publish(
            build_alias_plan("release", "v1.2.65"),
            new_digests,
            phase="mutable",
        )

    assert state == before
    assert not any(
        command[:3] == ("buildx", "imagetools", "create") for command in docker.commands
    )
