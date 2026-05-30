from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-release.yml"
BUILD_WIN = ROOT / "apps" / "desktop" / "packaging" / "scripts" / "build-win.ps1"
SIGN_WIN = ROOT / "apps" / "desktop" / "packaging" / "scripts" / "sign-win.ps1"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_tagged_mac_release_requires_and_verifies_notarized_dmgs() -> None:
    workflow = _workflow_text()

    for secret in (
        "APPLE_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_TEAM_ID",
        "APPLE_APP_PASSWORD",
        "TAURI_UPDATER_PUBKEY",
        "TAURI_SIGNING_PRIVATE_KEY",
    ):
        assert secret in workflow

    assert "Missing secrets required for tagged mac desktop releases:" in workflow
    assert "apps/desktop/packaging/scripts/notarize-mac.sh" in workflow
    assert 'xcrun stapler validate "$dmg"' in workflow
    assert "spctl --assess --type open --context context:primary-signature" in workflow
    assert "spctl --assess --type execute --verbose=4" in workflow
    assert "xattr -w com.apple.quarantine" in workflow
    assert "xattr -rw com.apple.quarantine" in workflow


def test_tagged_windows_release_exposes_and_verifies_authenticode_signing() -> None:
    workflow = _workflow_text()

    for name in (
        "WINDOWS_SIGNING_THUMBPRINT",
        "WINDOWS_SIGNING_CERT_PATH",
        "WINDOWS_SIGNING_CERT_PASSWORD",
        "WINDOWS_SIGNING_CERT_PFX_BASE64",
        "WINDOWS_TIMESTAMP_URL",
    ):
        assert name in workflow

    assert "Missing secrets required for tagged Windows desktop releases:" in workflow
    assert "Configure Windows Authenticode signing" in workflow
    assert "Verify Authenticode signed installers" in workflow
    assert "Get-AuthenticodeSignature" in workflow
    assert '$signature.Status -ne "Valid"' in workflow
    assert "apps/desktop/packaging/scripts/build-win.ps1" in workflow


def test_windows_build_signs_within_tauri_bundle_before_updater_zip() -> None:
    build_win = BUILD_WIN.read_text(encoding="utf-8")
    sign_win = SIGN_WIN.read_text(encoding="utf-8")

    assert "function Get-WindowsSignCommandConfig" in build_win
    assert "signCommand = $signCommand" in build_win
    assert '"%1"' in build_win
    assert build_win.index("$tauriConfigArgs = Get-TauriConfigArgs") < build_win.index(
        "cargo @cargoArgs"
    )
    assert build_win.index("cargo @cargoArgs") < build_win.index(
        '& (Join-Path $PSScriptRoot "sign-win.ps1") @signArgs'
    )
    assert "WINDOWS_SIGNING_THUMBPRINT" in sign_win
    assert "WINDOWS_SIGNING_CERT_PATH" in sign_win
    assert "Test-Path -LiteralPath $Path -PathType Leaf" in sign_win
    assert "signtool verify /pa /v $_.FullName" in sign_win


def test_tagged_windows_release_verifies_embedded_updater_installers() -> None:
    workflow = _workflow_text()

    assert "Expand-Archive -Path $updater.FullName" in workflow
    assert "valid_authenticode_updater=" in workflow
    assert "missing Windows updater zip for Authenticode verification" in workflow
    assert "missing Windows ARM64 updater zip for Authenticode verification" in workflow


def test_release_manifest_includes_intel_mac_artifacts() -> None:
    workflow = _workflow_text()

    assert "desktop-smoke-mac-x64:" in workflow
    assert "build-mac-x64:" in workflow
    assert "runs-on: macos-15-intel" in workflow
    assert "name: Lumen-mac-x64" in workflow
    assert "Normalize mac updater artifact names" in workflow
    assert "Normalize mac updater artifact names\n        if: startsWith(github.ref, 'refs/tags/')" in workflow
    assert "MAC_UPDATER_ARCH: aarch64" in workflow
    assert "MAC_UPDATER_ARCH: x64" in workflow
    assert 'target="$archive_dir/Lumen_${version}_${MAC_UPDATER_ARCH}.app.tar.gz"' in workflow
    assert "needs: [build-mac-arm64, build-mac-x64, build-win-x64, build-win-arm64]" in workflow
    assert 'missing+=("darwin-aarch64 .app.tar.gz")' in workflow
    assert 'missing+=("darwin-x86_64 .app.tar.gz")' in workflow
    assert "mac_x64_update=" in workflow
    assert '--artifact "darwin-aarch64=$mac_arm64_update"' in workflow
    assert '--artifact "darwin-x86_64=$mac_x64_update"' in workflow
