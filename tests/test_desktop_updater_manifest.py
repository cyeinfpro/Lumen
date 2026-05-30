from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCRIPT = (
    ROOT / "apps" / "desktop" / "packaging" / "scripts" / "create-updater-manifest.py"
)


def test_create_updater_manifest_finds_compound_suffix_signatures(
    tmp_path: Path,
) -> None:
    mac_artifact = tmp_path / "Lumen_1.2.3_aarch64.app.tar.gz"
    mac_x64_artifact = tmp_path / "Lumen_1.2.3_x64.app.tar.gz"
    win_artifact = tmp_path / "Lumen_1.2.3_x64.nsis.zip"
    mac_artifact.write_bytes(b"mac")
    mac_x64_artifact.write_bytes(b"mac-x64")
    win_artifact.write_bytes(b"win")
    mac_artifact.with_name(f"{mac_artifact.name}.sig").write_text(
        "bWFjLXNpZ25hdHVyZQ==\n",
        encoding="utf-8",
    )
    mac_x64_artifact.with_name(f"{mac_x64_artifact.name}.sig").write_text(
        "bWFjLXg2NC1zaWduYXR1cmU=\n",
        encoding="utf-8",
    )
    win_artifact.with_name(f"{win_artifact.name}.sig").write_text(
        "d2luLXNpZ25hdHVyZQ==\n",
        encoding="utf-8",
    )

    output = tmp_path / "latest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(MANIFEST_SCRIPT),
            "--version",
            "v1.2.3",
            "--base-url",
            "https://example.test/releases/download/v1.2.3",
            "--output",
            str(output),
            "--artifact",
            f"darwin-aarch64={mac_artifact}",
            "--artifact",
            f"darwin-x86_64={mac_x64_artifact}",
            "--artifact",
            f"windows-x86_64={win_artifact}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.3"
    assert manifest["platforms"]["darwin-aarch64"] == {
        "signature": "bWFjLXNpZ25hdHVyZQ==",
        "url": "https://example.test/releases/download/v1.2.3/Lumen_1.2.3_aarch64.app.tar.gz",
    }
    assert manifest["platforms"]["darwin-x86_64"] == {
        "signature": "bWFjLXg2NC1zaWduYXR1cmU=",
        "url": "https://example.test/releases/download/v1.2.3/Lumen_1.2.3_x64.app.tar.gz",
    }
    assert manifest["platforms"]["windows-x86_64"] == {
        "signature": "d2luLXNpZ25hdHVyZQ==",
        "url": "https://example.test/releases/download/v1.2.3/Lumen_1.2.3_x64.nsis.zip",
    }


def test_create_updater_manifest_rejects_invalid_base64_signature(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "Lumen_1.2.3_aarch64.app.tar.gz"
    artifact.write_bytes(b"mac")
    artifact.with_name(f"{artifact.name}.sig").write_text(
        "not valid base64!\n",
        encoding="utf-8",
    )

    output = tmp_path / "latest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(MANIFEST_SCRIPT),
            "--version",
            "v1.2.3",
            "--base-url",
            "https://example.test/releases/download/v1.2.3",
            "--output",
            str(output),
            "--artifact",
            f"darwin-aarch64={artifact}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid base64 updater signature" in result.stderr
    assert not output.exists()
