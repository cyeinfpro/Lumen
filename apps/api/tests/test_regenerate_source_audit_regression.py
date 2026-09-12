"""B06/B08 regressions against the actual API selectors and request contract."""

from types import SimpleNamespace

import pytest

from app.routes.messages_parts.silent import (
    SilentGenerationIn,
    silent_generation_request_hash,
)
from app.routes.regenerate_source import primary_generations, requested_generation_count


def row(**request):
    return SimpleNamespace(upstream_request=request)


def test_bonus_rows_do_not_increase_requested_batch_count():
    rows = [row(), row(), row(is_dual_race_bonus=True), row(bonus_billing_obligation=True)]
    assert len(primary_generations(rows)) == 2
    assert requested_generation_count(rows) == 2


def test_ten_primary_images_and_extra_remain_a_ten_image_request():
    rows = [row(batch_task_count=10) for _ in range(10)]
    rows.append(row(billing_policy="batch_extra_settled_separately"))
    assert requested_generation_count(rows) == 10


def test_partial_history_keeps_original_requested_count():
    assert requested_generation_count([row(requested_image_count=4)]) == 4


def test_legitimate_parent_reference_is_not_mistaken_for_a_bonus():
    primary = row(parent_generation_id="previous-generation")
    assert primary_generations([primary]) == [primary]
    assert requested_generation_count([primary]) == 1


def test_conflicting_history_counts_fail_instead_of_resetting_all_parameters():
    with pytest.raises(ValueError, match="conflicting"):
        requested_generation_count([row(batch_task_count=2), row(batch_task_count=4)])


def test_invalid_history_counts_fail_instead_of_silently_clamping():
    for invalid in (True, 0, 11, "4"):
        with pytest.raises(ValueError, match="invalid"):
            requested_generation_count([row(requested_image_count=invalid)])


def test_mask_is_part_of_silent_request_idempotency():
    common = dict(
        idempotency_key="test-idempotency",
        parent_message_id="parent-user-message",
        intent="image_to_image",
        prompt="edit only the marked area",
        attachment_image_ids=["reference"],
    )
    unmasked = SilentGenerationIn(**common)
    first_mask = SilentGenerationIn(**common, mask_image_id="mask-one")
    second_mask = SilentGenerationIn(**common, mask_image_id="mask-two")
    assert len({
        silent_generation_request_hash(unmasked),
        silent_generation_request_hash(first_mask),
        silent_generation_request_hash(second_mask),
    }) == 3


def test_omitted_and_explicit_null_masks_keep_legacy_hash_compatibility():
    common = dict(idempotency_key="test-idempotency", parent_message_id="parent")
    assert silent_generation_request_hash(SilentGenerationIn(**common)) == (
        silent_generation_request_hash(SilentGenerationIn(**common, mask_image_id=None))
    )
