from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "docs" / "audits" / "archive"
LOCAL_ARCHIVE = ROOT / "docs" / "audits" / "local"
REQUIRED_KEYS = {
    "baseline_commit",
    "status",
    "resolved_by",
    "superseded_by",
}


def _front_matter(path: Path) -> dict[str, str | None]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", path
    end = lines.index("---", 1)
    values: dict[str, str | None] = {}
    for line in lines[1:end]:
        key, separator, raw = line.partition(":")
        assert separator, f"invalid front matter line in {path}: {line}"
        value = raw.strip().strip('"')
        values[key.strip()] = None if value in {"", "null"} else value
    return values


def test_versioned_audit_archive_metadata_is_complete_and_conservative() -> None:
    reports = sorted(ARCHIVE.glob("*.md"))
    assert reports

    for report in reports:
        metadata = _front_matter(report)
        assert REQUIRED_KEYS <= metadata.keys(), report
        assert metadata["baseline_commit"], report
        status = metadata["status"]
        assert status in {"archived", "resolved", "superseded"}, report
        if status == "archived":
            assert metadata["resolved_by"] is None, report
        if status == "resolved":
            assert metadata["resolved_by"], report
        if status == "superseded":
            assert metadata["superseded_by"], report


def test_generated_local_audits_stay_gitignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "docs/audits/local/*" in gitignore
    assert "!docs/audits/local/README.md" in gitignore
    assert (LOCAL_ARCHIVE / "README.md").is_file()
