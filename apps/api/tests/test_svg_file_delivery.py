import io
from pathlib import Path

from app.images.application._file_delivery import storage_streaming_response


def test_svg_original_is_sandboxed_download():
    result = storage_streaming_response(
        Path("generated.svg"), media_type="image/svg+xml", etag='"abc"',
        cache_control="no-store", storage_key=None, request=None,
        inline_filename="design.svg", etag_matches=lambda *_: False,
        validate_storage_key=lambda _: Path("generated.svg"),
        open_file=lambda _: (io.BytesIO(b"<svg/>"), 6),
        iter_file=lambda stream: iter([stream.read()]),
        redirect_enabled=lambda: False,
    )
    assert result.headers["content-type"].startswith("image/svg+xml")
    assert result.headers["content-disposition"] == 'attachment; filename="design.svg"'
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in result.headers["content-security-policy"]
    assert "default-src 'none'" in result.headers["content-security-policy"]
