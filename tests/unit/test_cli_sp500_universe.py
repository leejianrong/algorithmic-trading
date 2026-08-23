"""``@sp500`` sigil resolution: point-in-time S&P 500 membership from the CLI (ADR-0072).

Two layers, offline throughout (no network, no 500-symbol fetch needed to prove the
mechanism). The first calls ``cli._parse_symbols``/``cli._parse_sp500_universe``
directly against a **fake** :class:`~trading.data.sp500_membership.PointInTimeSP500`
built from a couple of hand-written :class:`MembershipChange` rows — enough to prove
the sigil resolves against the *caller's* date, not today's, without touching the
real 694-row fixture or its wall-clock-dependent "now". The second drives the same
fake through the real ``backtest`` command end to end on ``--source synthetic``, to
pin that ``as_of`` actually reaches ``_parse_symbols`` from ``--from`` and that
``--sector-map @sp500`` fails the same way an unknown basket always has (there is no
committed sector map for 500 names, and none should be fabricated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from trading import cli
from trading.cli import app
from trading.data.sp500_membership import MembershipChange, PointInTimeSP500
from trading.universe import BASKETS

runner = CliRunner()


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# A tiny two-change fixture: {AAA, BBB} from 2000, AAA replaced by CCC in 2010.
_FAKE_CHANGES = [
    MembershipChange(date=_d(2000, 1, 1), added=("AAA", "BBB"), removed=()),
    MembershipChange(date=_d(2010, 1, 1), added=("CCC",), removed=("AAA",)),
]


class _FakePointInTimeSource:
    """Stands in for ``PointInTimeSP500`` so tests never touch the real fixture."""

    @staticmethod
    def from_fixture() -> PointInTimeSP500:
        return PointInTimeSP500(_FAKE_CHANGES)


@pytest.fixture
def fake_pit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "PointInTimeSP500", _FakePointInTimeSource)


# --- _parse_symbols / _parse_sp500_universe: direct unit tests -----------------


def test_sp500_resolves_membership_as_of_the_given_date(fake_pit: None) -> None:
    assert cli._parse_symbols("@sp500", as_of=_d(2005, 1, 1)) == ["AAA", "BBB"]


def test_sp500_uses_the_caller_s_date_not_today(fake_pit: None) -> None:
    # A later as_of sees the 2010 change: AAA is gone, CCC has arrived. If this
    # sigil ever started resolving against `datetime.now()` instead of the
    # caller's date, this assertion would stop distinguishing the two dates.
    assert cli._parse_symbols("@sp500", as_of=_d(2015, 6, 1)) == ["BBB", "CCC"]


def test_sp500_is_a_point_in_time_snapshot_not_a_moving_target(fake_pit: None) -> None:
    # Two calls at the same as_of must agree -- the whole survivorship point is a
    # backtest resolves the universe ONCE at its own start, not on every call.
    first = cli._parse_symbols("@sp500", as_of=_d(2005, 1, 1))
    second = cli._parse_symbols("@sp500", as_of=_d(2005, 1, 1))
    assert first == second == ["AAA", "BBB"]


def test_sp500_without_a_date_in_scope_exits_2(fake_pit: None) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        cli._parse_symbols("@sp500")
    assert exc_info.value.exit_code == 2


def test_sp500_before_fixture_coverage_exits_2(fake_pit: None) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        cli._parse_symbols("@sp500", as_of=_d(1999, 1, 1))
    assert exc_info.value.exit_code == 2


def test_sp500_is_not_a_static_basket() -> None:
    # ADR-0072: @sp500 is handled before universe.get_universe ever sees it, and
    # must never be added to BASKETS -- a static basket is exactly the wrong shape
    # for a query whose answer depends on the caller's date.
    assert "sp500" not in BASKETS


def test_plain_comma_list_is_unaffected_by_as_of(fake_pit: None) -> None:
    assert cli._parse_symbols("AAPL,MSFT", as_of=_d(2005, 1, 1)) == ["AAPL", "MSFT"]


def test_unknown_basket_still_errors_the_same_way(fake_pit: None) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        cli._parse_symbols("@not_a_real_basket", as_of=_d(2005, 1, 1))
    assert exc_info.value.exit_code == 2


# --- End to end through `backtest` ----------------------------------------------

_COMMON = ["--from", "2005-06-01", "--to", "2005-09-01", "--source", "synthetic"]


def test_backtest_symbols_sp500_resolves_from_the_start_date(
    fake_pit: None, tmp_path: Path
) -> None:
    out = tmp_path / "equity.csv"
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--symbols",
            "@sp500",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Symbols:       AAA, BBB" in result.output


def test_backtest_symbols_sp500_at_a_later_start_sees_later_membership(
    fake_pit: None, tmp_path: Path
) -> None:
    out = tmp_path / "equity.csv"
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--symbols",
            "@sp500",
            "--out",
            str(out),
            "--from",
            "2015-06-01",
            "--to",
            "2015-09-01",
            "--source",
            "synthetic",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Symbols:       BBB, CCC" in result.output


def test_backtest_sector_map_sp500_fails_the_existing_unknown_basket_way(
    tmp_path: Path,
) -> None:
    # No committed sector data for 500 names (by design, per the ticket) -- this
    # must fail with the SAME "unknown basket" error @blue20 already gives for a
    # typo, not a fabricated approximate sector map.
    out = tmp_path / "equity.csv"
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--symbols",
            "AAA,BBB",
            "--sector-map",
            "@sp500",
            "--max-sector-exposure",
            "0.3",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "unknown basket 'sp500'" in result.output


def test_verify_universe_sp500_has_no_date_in_scope(fake_pit: None) -> None:
    # verify-universe never parses --from/--to, so @sp500 cannot resolve there --
    # a clear, existing-shape CLI error (exit 2), never a silent "today" fallback.
    result = runner.invoke(app, ["verify-universe", "--symbols", "@sp500"])
    assert result.exit_code == 2
    assert "needs a start date" in result.output
