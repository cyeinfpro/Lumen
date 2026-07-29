from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_release_proof",
    ROOT / "scripts" / "release_proof.py",
)
assert SPEC is not None and SPEC.loader is not None
release_proof = module_from_spec(SPEC)
sys.modules[SPEC.name] = release_proof
SPEC.loader.exec_module(release_proof)


TAG = "v1.2.83"
COMMIT = "a" * 40
SERVICES = ("api", "worker", "tgbot", "web")


def _digest(index: int) -> str:
    return f"sha256:{index:064x}"


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": TAG,
        "commit_sha": COMMIT,
        "images": {
            service: {"digest": _digest(index)}
            for index, service in enumerate(SERVICES, start=1)
        },
    }


class FakeRunner:
    def __init__(self, *, successful: bool = True) -> None:
        self.successful = successful

    def __call__(
        self,
        command: tuple[str, ...] | list[str],
        _root: Path,
    ) -> object:
        args = tuple(command)
        if args[:4] == ("git", "rev-list", "-n", "1"):
            return release_proof.CommandResult(0, COMMIT + "\n", "")
        if args[:3] == ("gh", "run", "list"):
            conclusion = "success" if self.successful else "failure"
            return release_proof.CommandResult(
                0,
                (
                    '[{"databaseId":42,"status":"completed",'
                    f'"conclusion":"{conclusion}","headSha":"{COMMIT}",'
                    f'"headBranch":"{TAG}","url":"https://example/run"}}]'
                ),
                "",
            )
        if args[:2] == ("gh", "api"):
            return release_proof.CommandResult(
                0,
                (
                    f'{{"id":7,"tag_name":"{TAG}","draft":false,'
                    '"prerelease":false,"html_url":"https://example/release"}'
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {args}")


def _registry(repository: str, alias: str) -> object:
    service = repository.rsplit("-", maxsplit=1)[1]
    expected = _digest(SERVICES.index(service) + 1)
    observed = expected if alias == TAG else _digest(100 + len(alias))
    return release_proof.RegistryManifest(
        digest=observed,
        source_digest=expected,
    )


def test_release_proof_verifies_run_manifest_and_all_stable_aliases() -> None:
    proof = release_proof.verify_release(
        tag=TAG,
        commit=COMMIT,
        root=ROOT,
        runner=FakeRunner(),
        manifest_fetcher=lambda _tag: _manifest(),
        registry_fetcher=_registry,
    )

    assert proof["status"] == "passed"
    assert proof["stable_aliases"] == [TAG, "v1.2", "v1", "latest"]
    assert set(proof["images"]) == set(SERVICES)


def test_release_proof_fails_closed_when_workflow_did_not_succeed() -> None:
    with pytest.raises(
        release_proof.ReleaseProofError,
        match="no successful completed Docker Release run",
    ):
        release_proof.verify_release(
            tag=TAG,
            commit=COMMIT,
            root=ROOT,
            runner=FakeRunner(successful=False),
            manifest_fetcher=lambda _tag: _manifest(),
            registry_fetcher=_registry,
        )


def test_release_proof_rejects_alias_source_digest_drift() -> None:
    def drifted_registry(repository: str, alias: str) -> object:
        result = _registry(repository, alias)
        if alias == "latest":
            return release_proof.RegistryManifest(
                digest=result.digest,
                source_digest=_digest(999),
            )
        return result

    with pytest.raises(release_proof.ReleaseProofError, match="latest resolves"):
        release_proof.verify_release(
            tag=TAG,
            commit=COMMIT,
            root=ROOT,
            runner=FakeRunner(),
            manifest_fetcher=lambda _tag: _manifest(),
            registry_fetcher=drifted_registry,
        )
