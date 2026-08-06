#!/usr/bin/env python3
"""Reject empty or mutable image references from production Compose output."""

from __future__ import annotations

import re
import sys


IMMUTABLE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def main() -> int:
    images = [
        line.rstrip("\r\n")
        for line in sys.stdin
        if line.rstrip("\r\n") != ""
    ]
    if not images:
        print("compose returned no images", file=sys.stderr)
        return 2
    invalid = sorted({image for image in images if not IMMUTABLE.fullmatch(image)})
    if invalid:
        for image in invalid:
            print(f"mutable or invalid production image: {image}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
