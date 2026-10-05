from app.tasks.completion_parts.citation_text import finalize_completion_text


def part(text, annotations=()):
    return {"type": "output_text", "text": text, "annotations": list(annotations)}


def citation(start=0, end=4, url="https://example.com/source"):
    return {"start_index": start, "end_index": end, "url": url}


def response(*parts):
    return {"output": [{"type": "message", "content": [p]} for p in parts]}


def test_multipart_and_multimessage_offsets():
    assert finalize_completion_text("", response(part("Alpha "), part("Beta", [citation()]))) == (
        "Alpha [Beta](https://example.com/source)"
    )


def test_unicode_part_offsets():
    assert finalize_completion_text("", response(part("中文😀 "), part("乙😀", [citation(0, 2)]))) == (
        "中文😀 [乙😀](https://example.com/source)"
    )


def test_overlapping_sources_do_not_corrupt_text():
    text = finalize_completion_text("", response(part("Beta", [
        citation(), citation(url="https://example.com/second"),
    ])))
    assert text.startswith("[Beta](https://example.com/source)")
    assert "[https://example.com/second](https://example.com/second)" in text
    assert text.count("Beta") == 1


def test_missing_or_out_of_part_range_spans_append_sources():
    text = finalize_completion_text("", response(part("Alpha "), part("B", [citation()])))
    assert text.startswith("Alpha B\n\n来源")
    assert "https://example.com/source" in text


def test_different_aggregate_text_never_reuses_part_coordinates():
    data = response(part("Alpha"), part("Beta", [citation()]))
    data["output_text"] = "Alpha\nBeta"
    text = finalize_completion_text("", data)
    assert text.startswith("Alpha\nBeta\n\n来源")
