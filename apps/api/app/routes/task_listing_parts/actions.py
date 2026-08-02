"""失败任务推荐动作(重试/充值/检查 Key 等)的构造。

从 routes/task_listing_routes.py 拆出,保持路由文件在 route/controller
行数上限内。
"""

from __future__ import annotations

from lumen_core.schema_models import TaskRecommendedActionOut


def task_recommended_actions(
    *,
    kind: str,
    status: str,
    error_code: str | None,
    retryable: bool,
) -> list[TaskRecommendedActionOut]:
    if status == "canceled":
        return [
            TaskRecommendedActionOut(id="retry", label="重新开始", kind="retry"),
        ]
    if status != "failed":
        return []

    code = (error_code or "").strip()
    actions: list[TaskRecommendedActionOut] = []
    if retryable:
        actions.append(TaskRecommendedActionOut(id="retry", label="重试", kind="retry"))

    if code in {"INSUFFICIENT_BALANCE", "insufficient_credits"}:
        actions.extend(
            [
                TaskRecommendedActionOut(
                    id="open_wallet",
                    label="去充值",
                    kind="link",
                    href="/me/wallet",
                ),
                TaskRecommendedActionOut(
                    id="reduce_cost",
                    label="降低质量/数量",
                    kind="adjust",
                ),
            ]
        )
    elif code in {
        "NO_ACTIVE_API_KEY",
        "no_active_api_key",
        "authentication_error",
        "permission_error",
        "unauthorized",
        "invalid_api_key",
        "upstream_auth_error",
    }:
        actions.append(
            TaskRecommendedActionOut(
                id="open_api_key",
                label="检查 API Key",
                kind="link",
                href="/settings/api-key",
            )
        )
    elif code in {
        "invalid_request_error",
        "invalid_request",
        "invalid_param",
        "invalid_value",
        "validation_error",
        "prompt_too_long",
        "upstream_context_too_long",
    }:
        actions.append(
            TaskRecommendedActionOut(id="edit_input", label="调整输入", kind="adjust")
        )
    elif code in {
        "bad_reference_image",
        "reference_missing",
        "missing_input_images",
        "reference_image_too_large",
        "no_mask_capable_provider",
    }:
        actions.append(
            TaskRecommendedActionOut(
                id="fix_reference",
                label="检查参考图/Mask",
                kind="adjust",
            )
        )
    elif code in {
        "moderation_blocked",
        "content_policy_violation",
        "safety_violation",
    }:
        actions.append(
            TaskRecommendedActionOut(
                id="edit_prompt", label="调整提示词", kind="adjust"
            )
        )
    elif not retryable:
        actions.append(
            TaskRecommendedActionOut(
                id="view_details", label="查看详情", kind="details"
            )
        )
    return actions[:3]
