#!/usr/bin/env python3
"""Create a Tauri v2 updater latest.json from signed updater artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def _parse_artifact(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(
            "artifact must be PLATFORM=PATH, for example darwin-aarch64=dist/Lumen.app.tar.gz"
        )
    platform, path = raw.split("=", 1)
    platform = platform.strip()
    if not platform:
        raise argparse.ArgumentTypeError("artifact platform is empty")
    artifact_path = Path(path).expanduser()
    if not artifact_path.is_file():
        raise argparse.ArgumentTypeError(f"artifact does not exist: {artifact_path}")
    return platform, artifact_path


def _signature_path(artifact_path: Path) -> Path:
    return artifact_path.with_name(f"{artifact_path.name}.sig")


def _asset_url(base_url: str, artifact_path: Path) -> str:
    return f"{base_url.rstrip('/')}/{quote(artifact_path.name)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--pub-date", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--artifact",
        action="append",
        type=_parse_artifact,
        required=True,
        help="Signed updater artifact in PLATFORM=PATH form. The .sig file must be next to it.",
    )
    args = parser.parse_args()

    pub_date = args.pub_date or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if pub_date.endswith("+00:00"):
        pub_date = f"{pub_date[:-6]}Z"

    platforms: dict[str, dict[str, str]] = {}
    for platform, artifact_path in args.artifact:
        sig_path = _signature_path(artifact_path)
        if not sig_path.is_file():
            raise SystemExit(f"missing updater signature for {artifact_path}: {sig_path}")
        signature = sig_path.read_text(encoding="utf-8").strip()
        if not signature:
            raise SystemExit(f"empty updater signature: {sig_path}")
        platforms[platform] = {
            "signature": signature,
            "url": _asset_url(args.base_url, artifact_path),
        }

    if not platforms:
        raise SystemExit("no platforms were provided")

    payload = {
        "version": args.version.removeprefix("v"),
        "notes": args.notes or args.base_url,
        "pub_date": pub_date,
        "platforms": platforms,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
