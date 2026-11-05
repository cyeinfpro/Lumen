"""XML-looking image payloads are validated even with SVG comments or namespaces."""
import pytest
from lumen_core.svg_output import looks_like_svg, validate_static_svg


@pytest.mark.parametrize("source", [
    b'<!-- generated vector --><svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"/>',
    b'<s:svg xmlns:s="http://www.w3.org/2000/svg" width="32" height="32"/>',
])
def test_comment_and_prefixed_svg_are_recognized_and_validated(source):
    assert looks_like_svg(source)
    assert validate_static_svg(source) == (32, 32)


def test_xml_recognition_never_bypasses_root_validation():
    source = b'<!-- not an SVG --><html><script>bad()</script></html>'
    assert looks_like_svg(source)
    with pytest.raises(ValueError, match="SVG root"):
        validate_static_svg(source)


def test_raster_magic_is_not_svg():
    assert not looks_like_svg(b"\x89PNG\r\n")
