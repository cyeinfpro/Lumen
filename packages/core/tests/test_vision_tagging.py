from __future__ import annotations

from lumen_core.vision_tagging import parse_model_library_tagging_payload


def test_parse_model_library_payload_extracts_json_fence_with_language() -> None:
    result = parse_model_library_tagging_payload(
        "img-1",
        """可以，结果如下：

```text
not-json
```

```json
{
  "appearance_direction": "east_asian",
  "style_tags": ["清冷", "高级感"],
  "age_segment": "adult",
  "gender": "female",
  "notes": "五官清秀，适合高级感定位"
}
```
""",
    )

    assert result.appearance_direction == "east_asian"
    assert result.style_tags == ["清冷", "高级感"]
    assert result.age_segment == "adult"
    assert result.gender == "female"
