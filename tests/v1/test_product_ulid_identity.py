from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re

import pytest

from ecorex.artifacts.identity import (
    new_artifact_id,
    new_feedback_id,
    new_retouch_job_id,
    new_retouch_workspace_id,
    new_revision_id,
)
from ecorex.ids import is_id, new_id, new_ulid
from ecorex.runtime.ids import new_id as runtime_new_id


_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_product_domains_share_one_ulid_contract() -> None:
    assert _ULID.fullmatch(new_ulid())
    assert runtime_new_id("evt").startswith("evt_")
    for value, prefix in (
        (new_artifact_id(), "art"),
        (new_revision_id(), "rev"),
        (new_feedback_id(), "fdb"),
        (new_retouch_job_id(), "rtj"),
        (new_retouch_workspace_id(), "rtw"),
    ):
        actual_prefix, ulid = value.split("_", 1)
        assert actual_prefix == prefix
        assert _ULID.fullmatch(ulid)
        assert is_id(value, prefix) is True


@pytest.mark.parametrize(
    ("value", "prefix"),
    (
        (None, "art"),
        ("art_", "art"),
        ("art_" + "0" * 25, "art"),
        ("art_" + "0" * 27, "art"),
        ("art_" + "o" * 26, "art"),
        ("art_" + "0" * 26, "rev"),
        ("art_" + "0" * 26, "bad_value"),
    ),
)
def test_shared_identity_predicate_rejects_noncanonical_values(
    value: object,
    prefix: str,
) -> None:
    assert is_id(value, prefix) is False


def test_concurrent_artifact_ulids_do_not_collide() -> None:
    with ThreadPoolExecutor(max_workers=32) as pool:
        values = list(pool.map(lambda _index: new_artifact_id(), range(2_000)))

    assert len(values) == len(set(values))


@pytest.mark.parametrize("prefix", ("", "1bad", "bad_value", "x" * 33, "中文"))
def test_identity_prefix_is_a_bounded_ascii_product_identifier(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        new_id(prefix)
