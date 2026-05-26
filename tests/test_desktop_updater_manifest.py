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
    win_artifact = tmp_path / "Lumen_1.2.3_x64.nsis.zip"
    mac_artifact.write_bytes(b"mac")
    win_artifact.write_bytes(b"win")
    mac_artifact.with_name(f"{mac_artifact.name}.sig").write_text(
        "mac-signature\n",
        encoding="utf-8",
    )
    win_artifact.with_name(f"{win_artifact.name}.sig").write_text(
        "win-signature\n",
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
        "signature": "mac-signature",
        "url": "https://example.test/releases/download/v1.2.3/Lumen_1.2.3_aarch64.app.tar.gz",
    }
    assert manifest["platforms"]["windows-x86_64"] == {
        "signature": "win-signature",
        "url": "https://example.test/releases/download/v1.2.3/Lumen_1.2.3_x64.nsis.zip",
    }
