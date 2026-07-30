"""Version and alias planning primitives for image promotion."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence

SEMVER_RE = re.compile(
    r"^v"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
MISSING_MANIFEST_MARKERS = (
    "404",
    "manifest unknown",
    "manifest_unknown",
    "no such manifest",
    "not found",
)
ISOLATED_ALIAS_SOURCE_ANNOTATION = "io.lumen.promotion.source-digest"
ISOLATED_ALIAS_NAME_ANNOTATION = "io.lumen.promotion.mutable-alias"


class PromotionError(RuntimeError):
    """Raised when aliases cannot be published safely."""


class PromotionInterrupted(PromotionError):
    """Raised when publication receives SIGINT or SIGTERM."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"publication interrupted by signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Command(Protocol):
    def run(self, args: Sequence[str]) -> CommandResult:
        """Run command arguments after the configured executable prefix."""


class SubprocessCommand:
    def __init__(self, prefix: Sequence[str]) -> None:
        if not prefix:
            raise PromotionError("command prefix cannot be empty")
        self._prefix = tuple(prefix)

    def run(self, args: Sequence[str]) -> CommandResult:
        result = subprocess.run(
            [*self._prefix, *args],
            check=False,
            capture_output=True,
            text=True,
        )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] | None

    @classmethod
    def parse(cls, tag: str) -> SemVer:
        match = SEMVER_RE.fullmatch(tag)
        if match is None:
            raise PromotionError(f"invalid release tag: {tag!r}")
        prerelease_text = match.group("prerelease")
        prerelease = (
            tuple(prerelease_text.split(".")) if prerelease_text is not None else None
        )
        if prerelease is not None:
            for identifier in prerelease:
                if (
                    identifier.isdigit()
                    and len(identifier) > 1
                    and identifier[0] == "0"
                ):
                    raise PromotionError(
                        f"numeric prerelease identifiers cannot have leading zeroes: {tag}"
                    )
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=prerelease,
        )

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    def compare(self, other: SemVer) -> int:
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return -1 if core < other_core else 1
        if self.prerelease is None:
            return 0 if other.prerelease is None else 1
        if other.prerelease is None:
            return -1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return -1 if int(left) < int(right) else 1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return -1 if left < right else 1
        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1


@dataclass(frozen=True)
class AliasPlan:
    mode: str
    release_tag: str | None
    exact_aliases: tuple[str, ...]
    mutable_aliases: tuple[str, ...]
    is_prerelease: bool
    version: SemVer | None


def build_alias_plan(mode: str, release_tag: str | None = None) -> AliasPlan:
    if mode == "main":
        if release_tag:
            raise PromotionError("main publication must not include a release tag")
        return AliasPlan(
            mode=mode,
            release_tag=None,
            exact_aliases=(),
            mutable_aliases=("main",),
            is_prerelease=False,
            version=None,
        )
    if mode != "release":
        raise PromotionError(f"unsupported publication mode: {mode!r}")
    if not release_tag:
        raise PromotionError("release publication requires --release-tag")

    version = SemVer.parse(release_tag)
    if version.is_prerelease:
        mutable_aliases: tuple[str, ...] = ()
    else:
        mutable_aliases = (
            f"v{version.major}.{version.minor}",
            f"v{version.major}",
            "latest",
        )
    return AliasPlan(
        mode=mode,
        release_tag=release_tag,
        exact_aliases=(release_tag,),
        mutable_aliases=mutable_aliases,
        is_prerelease=version.is_prerelease,
        version=version,
    )
