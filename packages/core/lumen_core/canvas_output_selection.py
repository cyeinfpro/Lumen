"""Exact planned candidate selection shared by worker and API read repair."""

from typing import Any


def canvas_auto_select_output_index(
    config_snapshot: dict[str, Any] | None,
    outputs: list[dict[str, Any]],
) -> int | None:
    """Return stored output-list index without substituting a planned ordinal.

    Single-node/legacy executions keep their first available output behavior.
    Batch plans capture a candidate ordinal; partial results may compact the
    stored list, so resolve its exact ordinal rather than silently taking zero.
    """
    if not outputs:
        return None
    metadata = (config_snapshot or {}).get("_canvas", {})
    if not isinstance(metadata, dict) or "planned_output_ordinal" not in metadata:
        return 0
    ordinal = metadata["planned_output_ordinal"]
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 0 <= ordinal <= 9
    ):
        return None
    matches = [
        index
        for index, output in enumerate(outputs)
        if isinstance(output, dict) and output.get("ordinal") == ordinal
    ]
    return matches[0] if len(matches) == 1 else None
