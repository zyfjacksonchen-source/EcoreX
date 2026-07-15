from __future__ import annotations

from ecorex import ids


def test_ulids_remain_unique_and_sort_in_creation_order_with_a_coarse_clock(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ids, "_last_timestamp_ms", -1)
    monkeypatch.setattr(ids, "_last_randomness", 0)
    now = 1_800_000_000_000_000_000
    monkeypatch.setattr(ids.time, "time_ns", lambda: now)

    generated = [ids.new_ulid() for _ in range(100)]

    assert len(generated) == len(set(generated))
    assert generated == sorted(generated)


def test_ulids_do_not_move_backwards_when_the_wall_clock_regresses(monkeypatch) -> None:
    monkeypatch.setattr(ids, "_last_timestamp_ms", -1)
    monkeypatch.setattr(ids, "_last_randomness", 0)
    values = iter(
        [
            1_800_000_000_100_000_000,
            1_800_000_000_000_000_000,
            1_799_999_999_900_000_000,
        ]
    )
    monkeypatch.setattr(ids.time, "time_ns", lambda: next(values))

    generated = [ids.new_ulid() for _ in range(3)]

    assert generated == sorted(generated)
    assert len(generated) == len(set(generated))
