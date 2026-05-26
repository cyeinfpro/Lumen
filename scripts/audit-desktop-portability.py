#!/usr/bin/env python3
"""Scan for Postgres/Redis assumptions that need an explicit desktop decision."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "postgres_json_path": re.compile(r"\.astext|->>|@>"),
    "postgres_array": re.compile(r"\bARRAY\b|func\.cardinality|cardinality\("),
    "postgres_lock": re.compile(r"pg_advisory|pg_try_advisory|hashtext"),
    "postgres_dialect": re.compile(r"postgresql\.|dialects\.postgresql"),
    "redis_runtime": re.compile(r"Redis|redis|XADD|XREAD|EVAL|Pub/Sub", re.IGNORECASE),
}
SEARCH_ROOTS = [
    ROOT / "apps/api/app",
    ROOT / "apps/worker/app",
    ROOT / "packages/core/lumen_core",
]


def classify(path: Path, pattern: str) -> str:
    text = str(path)
    if "routes/workflows.py" in text or "poster" in text or "apparel" in text:
        return "desktop_not_registered"
    if pattern == "redis_runtime":
        return "keep_garnet_gate"
    if pattern in {"postgres_json_path", "postgres_array"}:
        return "replace_or_branch"
    if pattern == "postgres_lock":
        return "keep_best_effort_fallback"
    return "review"


def main() -> None:
    findings = []
    for root in SEARCH_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name, regex in PATTERNS.items():
                for match in regex.finditer(text):
                    line_no = text.count("\n", 0, match.start()) + 1
                    findings.append(
                        {
                            "file": str(path.relative_to(ROOT)),
                            "line": line_no,
                            "pattern": name,
                            "decision": classify(path, name),
                        }
                    )
    print(json.dumps({"findings": findings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
