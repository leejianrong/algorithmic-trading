"""Fast, no-infra unit tests for the V5 clock seam (dev-playbook layer 1).

:class:`WallClock` is only ever read once here — never slept on — so the fast
layer stays instant; determinism comes from :class:`FakeClock`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading.clock import Clock, FakeClock, ImmediateClock, WallClock


def _ts(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=UTC)


class TestFakeClock:
    def test_now_is_controllable(self) -> None:
        clock = FakeClock(_ts(1))
        assert clock.now() == _ts(1)
        clock.advance(_ts(3))
        assert clock.now() == _ts(3)

    def test_sleep_until_advances_now_and_records_target(self) -> None:
        clock = FakeClock(_ts(1))
        clock.sleep_until(_ts(2))
        assert clock.now() == _ts(2)
        assert clock.sleep_calls == [_ts(2)]

    def test_sleep_until_never_really_waits(self) -> None:
        # A far-future target must return immediately (no real delay); if this
        # ever slept for real, the fast layer would hang.
        before = datetime.now(UTC)
        clock = FakeClock(_ts(1))
        clock.sleep_until(_ts(1) + timedelta(days=3650))
        assert (datetime.now(UTC) - before) < timedelta(seconds=1)

    def test_queue_steps_now_through_scripted_times(self) -> None:
        clock = FakeClock(_ts(1), queue=[_ts(2), _ts(3)])
        assert clock.now() == _ts(2)
        assert clock.now() == _ts(3)
        # Drained queue falls back to the last value.
        assert clock.now() == _ts(3)


class TestImmediateClock:
    def test_sleep_until_is_a_no_op(self) -> None:
        clock = ImmediateClock(_ts(1))
        clock.sleep_until(_ts(5))
        assert clock.now() == _ts(1)

    def test_now_reflects_last_advance(self) -> None:
        clock = ImmediateClock(_ts(1))
        clock.advance(_ts(4))
        assert clock.now() == _ts(4)


class TestWallClock:
    def test_now_is_tz_aware_utc(self) -> None:
        # Read once, never sleep — must not touch real wall-clock waiting.
        now = WallClock().now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)


class TestClockProtocol:
    def test_implementations_satisfy_protocol(self) -> None:
        assert isinstance(WallClock(), Clock)
        assert isinstance(ImmediateClock(_ts(1)), Clock)
        assert isinstance(FakeClock(_ts(1)), Clock)
