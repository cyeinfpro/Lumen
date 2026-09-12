from __future__ import annotations

import ast
from pathlib import Path

import pytest


APP_ROOT = Path(__file__).resolve().parents[1] / "app"

TRANSACTIONAL_AUDIT_EVENTS = (
    ("routes/admin.py", "admin.user.password_set"),
    ("routes/admin.py", "admin.user.delete"),
    ("routes/admin_proxies.py", "admin.proxies.update"),
    ("routes/admin_allowed_email_routes.py", "admin.allowed_email.add"),
    ("routes/admin_allowed_email_routes.py", "admin.allowed_email.delete"),
    ("routes/admin_dlq_routes.py", "admin.dlq.retry"),
    ("routes/admin_dlq_routes.py", "admin.dlq.sweep_deleted_users"),
    ("routes/providers.py", "admin.video_providers.update"),
    ("routes/providers.py", "admin.providers.clear"),
    ("routes/providers.py", "admin.providers.update"),
    ("routes/providers.py", "admin.providers.enabled"),
    ("routes/invites.py", "invite.create"),
    ("routes/invites.py", "invite.revoke"),
    ("images/application/http_routes.py", "image.delete"),
    ("routes/me.py", "me.account.delete"),
    ("routes/me.py", "me.session.revoke"),
)


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


@pytest.mark.parametrize(("relative_path", "event_type"), TRANSACTIONAL_AUDIT_EVENTS)
def test_pure_db_mutation_audit_is_explicitly_transactional(
    relative_path: str,
    event_type: str,
) -> None:
    tree = ast.parse((APP_ROOT / relative_path).read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(_keyword(node, "event_type"), ast.Constant)
        and _keyword(node, "event_type").value == event_type
    ]

    assert len(matches) == 1, f"{relative_path}: expected one {event_type} audit call"
    autocommit = _keyword(matches[0], "autocommit")
    assert isinstance(autocommit, ast.Constant) and autocommit.value is False
