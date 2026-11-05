from app.routes.provider_parts.presentation import provider_out

def test_provider_presentation_exposes_only_scoped_recommendation():
    source = {"name": "tested", "base_url": "https://flux.infpro.me/v1"}
    result = provider_out(source, 0)
    assert result.svg_author_recommendation["model"] == "gpt-6.1-sol"
    assert "svg_author_recommendation" not in source
    assert provider_out({"base_url": "https://unknown.invalid/v1"}, 0).svg_author_recommendation is None
