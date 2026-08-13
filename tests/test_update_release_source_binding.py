from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE_MODULE_DIR = ROOT / "scripts" / "update"
UPDATE_SOURCE_RELATIVE = (
    "release/manifest.sh",
    "release/source_helpers.sh",
    "release/fetch.sh",
    "release/digest.sh",
    "release/activate.sh",
)


def _update_source() -> str:
    return "\n".join(
        (UPDATE_MODULE_DIR / relative).read_text(encoding="utf-8")
        for relative in UPDATE_SOURCE_RELATIVE
    )


def _bash_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def _binding_harness(tmp_path: Path) -> Path:
    source = _update_source()
    functions = "\n".join(
        _bash_function(source, name)
        for name in (
            "release_commit_is_valid",
            "release_manifest_commit_for_tag",
            "verify_release_source_manifest_binding",
        )
    )
    harness = tmp_path / "binding-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "log_error() { printf '%s\\n' \"$*\" >&2; }\n"
        "log_info() { :; }\n"
        f"{functions}\n"
        'RELEASE_SOURCE_COMMIT="${TEST_SOURCE_COMMIT:-}"\n'
        'RELEASE_SOURCE_COMMIT_PROOF="test"\n'
        'verify_release_source_manifest_binding "$1" "$2"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    return harness


def _local_git_commit_harness(tmp_path: Path) -> Path:
    source = _update_source()
    functions = "\n".join(
        _bash_function(source, name)
        for name in (
            "release_commit_is_valid",
            "resolve_local_release_commit",
        )
    )
    harness = tmp_path / "local-git-commit-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"{functions}\n"
        'resolve_local_release_commit "$1" "$2"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    return harness


def _manifest(path: Path, *, tag: str, commit: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": tag,
                "commit_sha": commit,
                "short_sha": commit[:7],
            }
        ),
        encoding="utf-8",
    )


def test_release_source_binding_accepts_only_matching_40_byte_commit(
    tmp_path: Path,
) -> None:
    tag = "v1.2.62"
    commit = "a" * 40
    manifest = tmp_path / "release-manifest.json"
    invalid_manifest = tmp_path / "invalid-release-manifest.json"
    _manifest(manifest, tag=tag, commit=commit)
    _manifest(invalid_manifest, tag=tag, commit="not-a-full-commit")
    harness = _binding_harness(tmp_path)

    matched = subprocess.run(
        ["bash", str(harness), str(manifest), tag],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "TEST_SOURCE_COMMIT": commit},
        check=False,
    )
    mismatched = subprocess.run(
        ["bash", str(harness), str(manifest), tag],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "TEST_SOURCE_COMMIT": "b" * 40},
        check=False,
    )
    unproved = subprocess.run(
        ["bash", str(harness), str(manifest), tag],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "TEST_SOURCE_COMMIT": ""},
        check=False,
    )
    invalid = subprocess.run(
        ["bash", str(harness), str(invalid_manifest), tag],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "TEST_SOURCE_COMMIT": commit},
        check=False,
    )

    assert matched.returncode == 0, matched.stderr
    assert mismatched.returncode != 0
    assert "源码 commit 与 release manifest 不一致" in mismatched.stderr
    assert unproved.returncode != 0
    assert "无法证明待发布源码" in unproved.stderr
    assert invalid.returncode != 0
    assert "manifest 缺少有效的 40 位 commit_sha" in invalid.stderr


def test_formal_release_collects_source_commit_from_allowed_proofs() -> None:
    source = _update_source()

    assert 'RELEASE_SOURCE_GIT_REF="${RELEASE_SOURCE_REF:-HEAD}"' in source
    assert '"${RELEASE_SOURCE_GIT_REF}^{commit}"' in source
    assert "org.opencontainers.image.revision" in source
    assert "prepare_official_release_source_manifest" in source
    assert 'RELEASE_SOURCE_API_IMAGE="${api_image}"' in source
    assert "metadata:${RELEASE_SOURCE_METADATA_FILE}" not in source
    assert source.count("record_release_source_commit") >= 4


def test_no_git_official_release_defaults_to_immutable_image_source() -> None:
    source = _update_source()
    mandatory = source.index(
        'if [ -n "${RELEASE_EXPECTED_COMMIT}" ] \\\n'
        '        && [ "${OFFICIAL_LOCAL_GIT_COMMIT}" != "${RELEASE_EXPECTED_COMMIT}" ]; then'
    )
    wrapper_opt_in = source.index(
        'if [ "${LUMEN_UPDATE_GIT_PULL:-0}" = "1" ]; then',
        mandatory,
    )

    assert mandatory < wrapper_opt_in
    assert '"${RELEASE_SOURCE_API_IMAGE}"' in source[mandatory:wrapper_opt_in]
    assert "不能禁用 immutable image source" in source[mandatory:wrapper_opt_in]
    assert "与 manifest 匹配的本地 git source" in source[mandatory:wrapper_opt_in]
    assert (
        'RELEASE_SOURCE_REF="${RELEASE_SOURCE_COMMIT}"'
        in source[mandatory:wrapper_opt_in]
    )


def test_stale_git_official_release_falls_back_to_immutable_image_source() -> None:
    source = _update_source()
    resolve = source.index(
        'OFFICIAL_LOCAL_GIT_COMMIT="$(\n'
        "        resolve_local_release_commit"
    )
    fallback = source.index(
        '&& [ "${OFFICIAL_LOCAL_GIT_COMMIT}" != "${RELEASE_EXPECTED_COMMIT}" ]',
        resolve,
    )
    image_extract = source.index("try_image_extract_release", fallback)
    git_pull = source.index(
        'if [ "${LUMEN_UPDATE_GIT_PULL:-0}" = "1" ]; then',
        image_extract,
    )

    assert resolve < fallback < image_extract < git_pull
    assert "本地 git 无法解析" in source[resolve:image_extract]
    assert "commit 与 manifest 不一致" in source[resolve:image_extract]
    assert 'RELEASE_SOURCE_IMAGE_EXTRACT=1' in source[fallback:git_pull]


def test_local_release_commit_resolver_requires_an_existing_immutable_ref(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Lumen Test"],
        cwd=repository,
        check=True,
    )
    (repository / "VERSION").write_text("1.2.118\n", encoding="utf-8")
    subprocess.run(["git", "add", "VERSION"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "release"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "tag", "v1.2.118"], cwd=repository, check=True)
    harness = _local_git_commit_harness(tmp_path)

    matched = subprocess.run(
        ["bash", str(harness), str(repository), "v1.2.118"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    missing = subprocess.run(
        ["bash", str(harness), str(repository), "v1.2.119"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert matched.returncode == 0, matched.stderr
    assert matched.stdout.strip() == commit
    assert missing.returncode != 0
    assert missing.stdout == ""


def test_official_manifest_is_bound_after_fetch_without_changing_compat_paths() -> None:
    source = _update_source()
    fetch = source.index("prepare_official_release_source_manifest")
    verify = source.index(
        "verify_release_source_manifest_binding \\\n"
        '                "${RELEASE_MANIFEST_FILE}" "${RELEASE_MANIFEST_TAG}"'
    )
    verified = source.index(
        'emit_info set_image_tag release_manifest "verified"',
        verify,
    )

    assert fetch < verify < verified
    assert source.count("verify_release_source_manifest_binding \\") == 1
    assert 'if [ "${LUMEN_IMAGE_REGISTRY%/}" != "ghcr.io/cyeinfpro" ]; then' in source
    assert "LUMEN_ALLOW_UNVERIFIED_CUSTOM_REGISTRY" in source
    assert 'TARGET_TAG="main"' in source
    assert 'RELEASE_MANIFEST_FILE=""' in source


def test_pull_binds_single_inspect_image_ids_before_completion() -> None:
    source = _update_source()
    pull = _bash_function(source, "update_phase_pull_images")
    references = _bash_function(source, "lumen_update_required_image_references")
    capture = _bash_function(source, "lumen_update_capture_image_record")
    binder = _bash_function(source, "lumen_update_bind_immutable_images")

    assert "config --images" in references
    assert "--profile tgbot config --images" in references
    assert capture.count("lumen_docker image inspect") == 1
    assert '"Id"' in capture
    assert "RepoDigests" in capture
    assert "org.opencontainers.image.revision" in capture
    assert "compose_services" in source
    assert "pull_policy: never" in source
    assert "TARGET_IMAGE_SET_DIGEST" in binder
    assert pull.count("lumen_update_bind_immutable_images") >= 3
    assert pull.index("lumen_update_bind_immutable_images") < pull.index(
        "emit_done pull_images 0"
    )
