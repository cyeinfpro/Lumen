from __future__ import annotations

from collections.abc import Sequence

from lumen_core.context_window import estimate_text_tokens

from .common import SummarySegment


def split_oversized_lines(lines: Sequence[str], limit: int) -> list[str]:
    split_lines: list[str] = []
    for line in lines:
        if estimate_text_tokens(line) <= limit:
            split_lines.append(line)
            continue

        remaining = line
        max_chars = max(1000, limit * 3)
        while remaining:
            piece = remaining[:max_chars]
            while len(piece) > 1 and estimate_text_tokens(piece) > limit:
                piece = piece[: max(1, int(len(piece) * 0.8))]
            split_lines.append(piece)
            remaining = remaining[len(piece) :]
    return split_lines


def summary_segments_by_budget(
    lines: Sequence[str],
    budget: int,
) -> list[SummarySegment]:
    segments: list[SummarySegment] = []
    current: list[str] = []
    used = 0
    limit = max(1000, budget)
    current_covered = 0

    def flush_current() -> None:
        nonlocal current, used
        if not current:
            return
        segments.append(
            SummarySegment(
                lines=current,
                covered_message_count=current_covered,
                ends_at_message_boundary=True,
            )
        )
        current = []
        used = 0

    for message_index, line in enumerate(lines):
        if estimate_text_tokens(line) > limit:
            flush_current()
            pieces = split_oversized_lines([line], limit)
            for piece_index, piece in enumerate(pieces):
                is_last_piece = piece_index == len(pieces) - 1
                segments.append(
                    SummarySegment(
                        lines=[piece],
                        covered_message_count=(
                            message_index + 1 if is_last_piece else message_index
                        ),
                        ends_at_message_boundary=is_last_piece,
                    )
                )
            current_covered = message_index + 1
            continue

        cost = max(1, estimate_text_tokens(line))
        if current and used + cost > limit:
            flush_current()
        current.append(line)
        used += cost
        current_covered = message_index + 1

    flush_current()
    return segments


def chunk_lines_by_budget(lines: Sequence[str], budget: int) -> list[list[str]]:
    return [segment.lines for segment in summary_segments_by_budget(lines, budget)]


def bounded_summary_segments(
    segments: Sequence[SummarySegment],
    max_segments: int,
) -> tuple[list[SummarySegment], str | None]:
    if len(segments) <= max_segments:
        return list(segments), None

    safe_count = max(
        (
            index
            for index, segment in enumerate(
                segments[:max_segments],
                start=1,
            )
            if segment.ends_at_message_boundary
        ),
        default=0,
    )
    return list(segments[:safe_count]), "segment_limit"
