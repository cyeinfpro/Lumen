"""Durable release and immutable image proof helpers for the updater journal."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat


SOURCE_PROOF_NAME = ".release-source-proof"
IMAGE_PROOF_NAME = ".update-image-proof.json"
IMAGE_OVERRIDE_NAME = ".update-images.override.yml"
SOURCE_TREE_EXCLUDES = frozenset(
    {
        ".env",
        ".image-tag",
        IMAGE_OVERRIDE_NAME,
        IMAGE_PROOF_NAME,
        SOURCE_PROOF_NAME,
        "VERSION",
        "release-manifest.json",
    }
)
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_tree_sha256(root: Path) -> str:
    if not root.is_dir():
        raise SystemExit(f"target release source tree is missing: {root}")
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*"),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        if relative in SOURCE_TREE_EXCLUDES:
            continue
        relative_bytes = relative.encode("utf-8")
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            kind = b"L"
            content = os.readlink(path).encode("utf-8")
        elif path.is_dir():
            kind = b"D"
            content = b""
        elif path.is_file():
            digest.update(b"F")
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(mode.to_bytes(4, "big"))
            digest.update(info.st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            continue
        else:
            raise SystemExit(
                f"target release contains unsupported source entry: {path}"
            )
        digest.update(kind)
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SystemExit(f"update journal {label} is invalid")
    return value


def _phase_completed(payload: dict[str, object], phase: str) -> bool:
    completed = payload.get("completed_phases", [])
    return isinstance(completed, list) and phase in completed


def _require_file_digest(path: Path, expected: str, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SystemExit(f"resume {label} is missing")
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"resume {label} is not a regular file")
    if sha256_file(path) != expected:
        raise SystemExit(f"resume {label} hash mismatch")


def _expected_image_override(compose_services: dict[str, object]) -> bytes:
    lines = ["services:"]
    for service, image_id in sorted(compose_services.items()):
        lines.extend(
            (
                f"  {service}:",
                f'    image: "{image_id}"',
                "    pull_policy: never",
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _validate_image_binding_artifacts(
    payload: dict[str, object],
    target: dict[str, object],
    release_path: Path,
) -> None:
    proof_raw = target.get("image_proof_path")
    if proof_raw is None:
        return
    proof_path = Path(str(proof_raw))
    override_path = Path(str(target["image_override_path"]))
    if proof_path != release_path / IMAGE_PROOF_NAME:
        raise SystemExit("resume immutable image proof path mismatch")
    if override_path != release_path / IMAGE_OVERRIDE_NAME:
        raise SystemExit("resume immutable image override path mismatch")
    _require_file_digest(
        proof_path,
        str(target["image_proof_sha256"]),
        "immutable image proof",
    )
    _require_file_digest(
        override_path,
        str(target["image_override_sha256"]),
        "immutable image override",
    )
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"resume immutable image proof is invalid: {exc}") from exc
    if not isinstance(proof, dict) or set(proof) != {
        "build",
        "compose_services",
        "schema",
        "services",
        "source_commit",
        "target_tag",
    }:
        raise SystemExit("resume immutable image proof fields are invalid")
    if (
        proof.get("schema") != 1
        or proof.get("target_tag") != target["effective_tag"]
        or proof.get("source_commit") != target["source_commit"]
        or not isinstance(proof.get("build"), bool)
    ):
        raise SystemExit("resume immutable image proof contract mismatch")
    context = _require_mapping(payload.get("context", {}), "context")
    if proof["build"] != (context.get("LUMEN_UPDATE_BUILD") == "1"):
        raise SystemExit("resume immutable image proof build mode mismatch")

    services = _require_mapping(proof.get("services"), "image proof services")
    service_names = set(services)
    if not {"api", "worker", "web"}.issubset(service_names) or service_names - {
        "api",
        "worker",
        "web",
        "tgbot",
    }:
        raise SystemExit("resume immutable image proof service set is invalid")
    image_ids: dict[str, str] = {}
    for service, record_raw in services.items():
        record = _require_mapping(record_raw, f"image proof service {service}")
        if (
            set(record)
            != {
                "image_id",
                "repo_digests",
                "revision",
                "service",
                "source_ref",
            }
            or record.get("service") != service
        ):
            raise SystemExit(
                f"resume immutable image proof record is invalid: {service}"
            )
        image_id = record.get("image_id")
        if not isinstance(image_id, str) or not IMAGE_DIGEST_RE.fullmatch(image_id):
            raise SystemExit(f"resume immutable Image ID is invalid: {service}")
        source_ref = record.get("source_ref")
        expected_name = f"lumen-{service}:{target['effective_tag']}"
        if not isinstance(source_ref, str):
            raise SystemExit(f"resume immutable source ref is invalid: {service}")
        source_leaf = source_ref.rsplit("/", 1)[-1]
        if "@" in source_leaf:
            source_repository, source_digest = source_ref.rsplit("@", 1)
            if (
                source_repository.rsplit("/", 1)[-1] != f"lumen-{service}"
                or not IMAGE_DIGEST_RE.fullmatch(source_digest)
            ):
                raise SystemExit(f"resume immutable source ref is invalid: {service}")
        else:
            if source_leaf != expected_name:
                raise SystemExit(f"resume immutable source ref is invalid: {service}")
            source_repository = source_ref.rsplit(":", 1)[0]
        repo_digests = record.get("repo_digests")
        if not isinstance(repo_digests, list) or any(
            not isinstance(value, str)
            or "@" not in value
            or not value.startswith(f"{source_repository}@")
            or not IMAGE_DIGEST_RE.fullmatch(value.rsplit("@", 1)[-1])
            for value in repo_digests
        ):
            raise SystemExit(f"resume immutable RepoDigest set is invalid: {service}")
        revision = record.get("revision")
        if proof["build"]:
            if revision is not None and not isinstance(revision, str):
                raise SystemExit(f"resume build revision is invalid: {service}")
        elif revision != target["source_commit"] or not repo_digests:
            raise SystemExit(f"resume image revision/digest proof mismatch: {service}")
        image_ids[service] = image_id

    expected_compose = {
        "api": image_ids["api"],
        "api-green": image_ids["api"],
        "bootstrap": image_ids["api"],
        "migrate": image_ids["api"],
        "web": image_ids["web"],
        "worker": image_ids["worker"],
    }
    if "tgbot" in image_ids:
        expected_compose["tgbot"] = image_ids["tgbot"]
    compose_services = _require_mapping(
        proof.get("compose_services"),
        "image proof compose services",
    )
    if compose_services != expected_compose:
        raise SystemExit("resume immutable compose image mapping mismatch")
    if override_path.read_bytes() != _expected_image_override(compose_services):
        raise SystemExit("resume immutable image override content mismatch")


def validate_target_artifacts(
    payload: dict[str, object],
    target: dict[str, object],
) -> None:
    release_path = Path(str(target["release_path"]))
    release_id = str(target["release_id"])
    root_raw = os.environ.get("ROOT")
    if root_raw and release_path != Path(root_raw) / "releases" / release_id:
        raise SystemExit("resume target release path/id invariant mismatch")
    if release_path.is_symlink() or not release_path.is_dir():
        raise SystemExit("resume target release invariant mismatch")

    source_proof = Path(str(target["source_proof_path"]))
    if source_proof != release_path / SOURCE_PROOF_NAME:
        raise SystemExit("resume source proof path invariant mismatch")
    _require_file_digest(
        source_proof,
        str(target["source_proof_sha256"]),
        "source proof",
    )
    expected_source_proof = (
        f"{target['source_commit']}\n{target['source_commit_proof']}\n"
    ).encode("utf-8")
    if source_proof.read_bytes() != expected_source_proof:
        raise SystemExit("resume source proof content mismatch")
    if source_tree_sha256(release_path) != target["source_tree_sha256"]:
        raise SystemExit("resume source tree hash mismatch")

    image_tag_path = release_path / ".image-tag"
    image_tag_sha = target.get("image_tag_sha256")
    if os.path.lexists(image_tag_path):
        expected_image_tag = f"{target['effective_tag']}\n".encode("utf-8")
        if image_tag_sha is not None:
            _require_file_digest(
                image_tag_path,
                str(image_tag_sha),
                ".image-tag proof",
            )
        elif not image_tag_path.is_file() or image_tag_path.is_symlink():
            raise SystemExit("resume .image-tag proof is not a regular file")
        if image_tag_path.read_bytes() != expected_image_tag:
            raise SystemExit("resume .image-tag proof content mismatch")
    if target.get("image_tag_path") is not None:
        if target.get("image_tag_path") != str(image_tag_path):
            raise SystemExit("resume .image-tag proof path mismatch")
        assert image_tag_sha is not None
        _require_file_digest(
            image_tag_path,
            str(image_tag_sha),
            ".image-tag proof",
        )
    if _phase_completed(payload, "set_image_tag"):
        declared_image_tag = target.get("image_tag_path")
        if declared_image_tag != str(image_tag_path) or image_tag_sha is None:
            raise SystemExit("resume .image-tag target proof is missing")
        _require_file_digest(
            image_tag_path,
            str(image_tag_sha),
            ".image-tag proof",
        )

    manifest_sha = target.get("manifest_sha256")
    canonical_manifest = release_path / "release-manifest.json"
    manifest_paths = [
        Path(str(target[field]))
        for field in ("manifest_cache_path", "manifest_path")
        if target.get(field)
    ]
    manifest_paths.append(canonical_manifest)
    unique_manifest_paths = list(dict.fromkeys(manifest_paths))
    existing_manifests = [
        path for path in unique_manifest_paths if os.path.lexists(path)
    ]
    if manifest_sha is None:
        if existing_manifests:
            raise SystemExit("resume found an unbound release manifest")
    else:
        if not existing_manifests:
            raise SystemExit("resume release manifest proof is missing")
        for manifest_path in existing_manifests:
            _require_file_digest(
                manifest_path,
                str(manifest_sha),
                "release manifest proof",
            )
        if _phase_completed(payload, "set_image_tag"):
            if target.get("manifest_path") != str(canonical_manifest):
                raise SystemExit("resume final release manifest path is missing")
            _require_file_digest(
                canonical_manifest,
                str(manifest_sha),
                "final release manifest proof",
            )
    _validate_image_binding_artifacts(payload, target, release_path)
