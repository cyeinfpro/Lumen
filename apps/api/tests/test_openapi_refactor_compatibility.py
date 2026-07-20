from app.main import app
from app.routes import billing as billing_module
from app.routes import conversations as conversations_module
from app.routes import poster_styles as poster_styles_module


def test_architecture_refactor_preserves_openapi_contract_names() -> None:
    schema = app.openapi()
    components = schema["components"]["schemas"]

    assert (
        schema["paths"]["/tasks"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/lumen_core__schemas__TaskListOut"
    )
    assert (
        schema["paths"]["/admin/request_events"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/_RequestEventsOut"
    )
    assert {
        "_RequestEventImageOut",
        "_RequestEventLiveLane",
        "_RequestEventModelStatOut",
        "_RequestEventOut",
        "_RequestEventsOut",
    }.issubset(components)


def test_architecture_refactor_preserves_openapi_metadata() -> None:
    schema = app.openapi()

    assert schema["paths"]["/me/wallet"]["get"]["tags"] == ["billing"]
    assert schema["paths"]["/admin/pricing"]["put"]["tags"] == ["billing"]
    assert schema["paths"]["/conversations/{conv_id}/compact"]["post"][
        "description"
    ].startswith("Manually compact a conversation's history.")
    assert schema["paths"]["/poster-styles/generate"]["post"]["description"] == (
        "用户提交 prompt + 元数据，后端创建隐藏 workflow 并入队 N 个生成任务。"
    )
    assert schema["components"]["schemas"]["StepRecord"]["description"].startswith(
        "One phase entry parsed from .update.log step lines."
    )


def test_route_facades_preserve_legacy_star_imports() -> None:
    assert "__all__" not in vars(billing_module)
    assert "__all__" not in vars(conversations_module)
    assert "__all__" not in vars(poster_styles_module)

    billing: dict[str, object] = {}
    conversations: dict[str, object] = {}
    poster_styles: dict[str, object] = {}

    exec("from app.routes.billing import *", billing)
    exec("from app.routes.conversations import *", conversations)
    exec("from app.routes.poster_styles import *", poster_styles)

    assert billing["get_my_wallet"]
    assert billing["admin_bulk_pricing"]
    assert conversations["compact_conversation"]
    assert conversations["get_conversation_context"]
    assert poster_styles["generate_poster_style_samples"]
    assert poster_styles["list_poster_styles"]
