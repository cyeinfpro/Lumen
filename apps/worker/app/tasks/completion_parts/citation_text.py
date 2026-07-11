"""Pure extraction and rendering helpers for completed response text."""

from __future__ import annotations

from typing import Any


def _markdown_link(label: str, url: str) -> str:
    safe_label = (label or url).replace("\\", "\\\\").replace("]", "\\]")
    safe_url = url.replace(")", "%29").replace(" ", "%20")
    return f"[{safe_label}]({safe_url})"


def _extract_url_citations(response: dict[str, Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    output = response.get("output")
    if not isinstance(output, list):
        return citations
    for item in output:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            for annotation in part.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                raw_citation = annotation.get("url_citation")
                raw = raw_citation if isinstance(raw_citation, dict) else annotation
                url = raw.get("url") if isinstance(raw, dict) else None
                if not isinstance(url, str) or not url.startswith(
                    ("http://", "https://")
                ):
                    continue
                title = raw.get("title") if isinstance(raw, dict) else None
                citations.append(
                    {
                        "url": url,
                        "title": title if isinstance(title, str) and title else url,
                        "text": text if isinstance(text, str) else None,
                        "start_index": annotation.get("start_index"),
                        "end_index": annotation.get("end_index"),
                    }
                )
    return citations


def _apply_url_citations(text: str, citations: list[dict[str, Any]]) -> str:
    if not text or not citations:
        return text
    replacements: list[tuple[int, int, str]] = []
    seen_urls: set[str] = set()
    for citation in citations:
        url = citation["url"]
        start = citation.get("start_index")
        end = citation.get("end_index")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if start < 0 or end <= start or end > len(text):
            continue
        label = text[start:end].strip()
        if not label:
            continue
        replacements.append((start, end, _markdown_link(label, url)))
        seen_urls.add(url)
    if replacements:
        # Apply from the end so earlier indexes remain valid.
        for start, end, link in sorted(
            replacements,
            key=lambda item: item[0],
            reverse=True,
        ):
            text = f"{text[:start]}{link}{text[end:]}"
    if not seen_urls:
        unique: list[dict[str, Any]] = []
        for citation in citations:
            if citation["url"] in seen_urls:
                continue
            seen_urls.add(citation["url"])
            unique.append(citation)
        if unique:
            links: list[str] = []
            for index, citation in enumerate(unique[:8], start=1):
                label = str(citation.get("title") or citation["url"])
                links.append(f"{index}. {_markdown_link(label, citation['url'])}")
            text = f"{text.rstrip()}\n\n来源\n" + "\n".join(links)
    return text


def _extract_completed_output_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    chunks: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                chunks.append(part["text"])
    return "".join(chunks)


def _finalize_completion_text(text: str, response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return text
    completed_text = _extract_completed_output_text(response)
    base = completed_text or text
    return _apply_url_citations(base, _extract_url_citations(response))
