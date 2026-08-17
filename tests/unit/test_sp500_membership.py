"""Fast, offline unit tests for point-in-time S&P 500 membership (ADR-0064).

Two layers. The first constructs tiny synthetic fixtures under ``tmp_path`` to
pin the parsing/replay mechanism precisely. The second reads the real committed
fixture (``tests/fixtures/sp500_membership/sp500_changes.csv``) -- entirely
offline, no network -- and characterizes it: coverage span, row count, and three
known corporate-history spot checks. Per ADR-0040, nothing here fetches; a
missing or stale fixture fails the "does the module even work" tests, and a
changed real-world value only shows up if someone re-runs
``scripts/refresh_sp500_membership.py`` and updates these pins deliberately (the
same discipline the yfinance cache fixture uses).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading.data.sp500_membership import (
    DEFAULT_FIXTURE_PATH,
    MembershipChange,
    PointInTimeSP500,
    load_changes,
    members_as_of,
)

_HEADER = "date,added,removed\n"


def _write_fixture(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER + "".join(row + "\n" for row in rows))


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# --- load_changes: parsing and validation --------------------------------------


def test_load_changes_parses_added_and_removed(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    _write_fixture(
        fixture,
        [
            "2000-01-01,AAA;BBB;CCC,",
            "2000-06-01,DDD,BBB",
        ],
    )
    changes = load_changes(fixture)
    assert changes == [
        MembershipChange(date=_d(2000, 1, 1), added=("AAA", "BBB", "CCC"), removed=()),
        MembershipChange(date=_d(2000, 6, 1), added=("DDD",), removed=("BBB",)),
    ]


def test_load_changes_missing_file_names_the_refresh_script(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"refresh_sp500_membership\.py"):
        load_changes(tmp_path / "does_not_exist.csv")


def test_load_changes_rejects_bad_header(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    fixture.write_text("wrong,header\n2000-01-01,x\n")
    with pytest.raises(ValueError, match="unexpected header"):
        load_changes(fixture)


def test_load_changes_rejects_malformed_date(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    _write_fixture(fixture, ["not-a-date,AAA,"])
    with pytest.raises(ValueError, match="malformed date"):
        load_changes(fixture)


def test_load_changes_rejects_wrong_column_count(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    fixture.write_text(_HEADER + "2000-01-01,AAA\n")
    with pytest.raises(ValueError, match="expected 3 columns"):
        load_changes(fixture)


def test_load_changes_rejects_non_ascending_dates(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    _write_fixture(
        fixture,
        [
            "2000-06-01,AAA,",
            "2000-01-01,BBB,",
        ],
    )
    with pytest.raises(ValueError, match="ascending"):
        load_changes(fixture)


def test_load_changes_rejects_duplicate_dates(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    _write_fixture(
        fixture,
        [
            "2000-01-01,AAA,",
            "2000-01-01,BBB,",
        ],
    )
    with pytest.raises(ValueError, match="ascending"):
        load_changes(fixture)


def test_load_changes_tolerates_blank_trailing_lines(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    fixture.write_text(_HEADER + "2000-01-01,AAA,\n\n")
    expected = [MembershipChange(date=_d(2000, 1, 1), added=("AAA",), removed=())]
    assert load_changes(fixture) == expected


# --- PointInTimeSP500: replay and query -----------------------------------------


def _three_change_universe() -> PointInTimeSP500:
    return PointInTimeSP500(
        [
            MembershipChange(date=_d(2000, 1, 1), added=("AAA", "BBB", "CCC"), removed=()),
            MembershipChange(date=_d(2000, 6, 1), added=("DDD",), removed=("BBB",)),
            MembershipChange(date=_d(2001, 1, 1), added=(), removed=("CCC",)),
        ]
    )


def test_members_as_of_at_first_change() -> None:
    pit = _three_change_universe()
    assert pit.members_as_of(_d(2000, 1, 1)) == ["AAA", "BBB", "CCC"]


def test_members_as_of_between_changes_holds_the_prior_snapshot() -> None:
    pit = _three_change_universe()
    assert pit.members_as_of(_d(2000, 3, 15)) == ["AAA", "BBB", "CCC"]


def test_members_as_of_reflects_an_add_and_a_remove() -> None:
    pit = _three_change_universe()
    assert pit.members_as_of(_d(2000, 6, 1)) == ["AAA", "CCC", "DDD"]
    assert pit.members_as_of(_d(2000, 12, 31)) == ["AAA", "CCC", "DDD"]


def test_members_as_of_after_last_change_returns_final_snapshot() -> None:
    pit = _three_change_universe()
    assert pit.members_as_of(_d(2001, 1, 1)) == ["AAA", "DDD"]
    # past coverage_end: still returns the last known state, not an error.
    assert pit.members_as_of(_d(2099, 1, 1)) == ["AAA", "DDD"]


def test_members_as_of_before_coverage_raises() -> None:
    pit = _three_change_universe()
    with pytest.raises(ValueError, match="before this fixture's earliest coverage"):
        pit.members_as_of(_d(1999, 12, 31))


def test_coverage_bounds() -> None:
    pit = _three_change_universe()
    assert pit.coverage_start == _d(2000, 1, 1)
    assert pit.coverage_end == _d(2001, 1, 1)


def test_empty_changes_list_rejected() -> None:
    with pytest.raises(ValueError, match="no membership changes"):
        PointInTimeSP500([])


def test_from_fixture_round_trip(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    _write_fixture(fixture, ["2000-01-01,AAA;BBB,"])
    pit = PointInTimeSP500.from_fixture(fixture)
    assert pit.members_as_of(_d(2000, 1, 1)) == ["AAA", "BBB"]


def test_module_level_members_as_of_matches_the_class(tmp_path: Path) -> None:
    fixture = tmp_path / "changes.csv"
    _write_fixture(fixture, ["2000-01-01,AAA;BBB,"])
    assert members_as_of(_d(2000, 1, 1), path=fixture) == ["AAA", "BBB"]


# --- The real committed fixture: characterization, offline ----------------------
# These read tests/fixtures/sp500_membership/sp500_changes.csv directly. No
# network call; the fixture is either present (committed) or these fail loudly,
# same discipline as the yfinance cache tests (ADR-0040).


@pytest.fixture(scope="module")
def real_pit() -> PointInTimeSP500:
    return PointInTimeSP500.from_fixture(DEFAULT_FIXTURE_PATH)


def test_default_fixture_path_exists() -> None:
    assert DEFAULT_FIXTURE_PATH.exists(), (
        f"committed fixture missing at {DEFAULT_FIXTURE_PATH}; see "
        "scripts/refresh_sp500_membership.py"
    )


def test_real_fixture_coverage_span(real_pit: PointInTimeSP500) -> None:
    # Pinned as of the 2026-08-17 refresh (docs/adr/0064). A future refresh will
    # move these forward; that is expected and should be a deliberate pin update,
    # not a silent pass.
    assert real_pit.coverage_start == _d(1996, 1, 2)
    assert real_pit.coverage_end == _d(2026, 6, 30)


def test_real_fixture_aapl_present_throughout(real_pit: PointInTimeSP500) -> None:
    assert "AAPL" in real_pit.members_as_of(real_pit.coverage_start)
    assert "AAPL" in real_pit.members_as_of(_d(2015, 1, 1))
    assert "AAPL" in real_pit.members_as_of(real_pit.coverage_end)


def test_real_fixture_tesla_added_2020_12_21(real_pit: PointInTimeSP500) -> None:
    assert "TSLA" not in real_pit.members_as_of(_d(2020, 12, 20))
    assert "TSLA" in real_pit.members_as_of(_d(2020, 12, 21))


def test_real_fixture_facebook_to_meta_ticker_change_2022_06_09(real_pit: PointInTimeSP500) -> None:
    assert "FB" in real_pit.members_as_of(_d(2022, 6, 8))
    assert "META" not in real_pit.members_as_of(_d(2022, 6, 8))
    assert "META" in real_pit.members_as_of(_d(2022, 6, 9))
    assert "FB" not in real_pit.members_as_of(_d(2022, 6, 9))


def test_real_fixture_gm_readded_2013_06_07(real_pit: PointInTimeSP500) -> None:
    # The post-bankruptcy re-IPO GM, correctly distinct from the pre-2009 GM.
    assert "GM" not in real_pit.members_as_of(_d(2013, 6, 6))
    assert "GM" in real_pit.members_as_of(_d(2013, 6, 7))


def test_real_fixture_membership_size_stays_near_500(real_pit: PointInTimeSP500) -> None:
    for probe in (_d(2001, 1, 16), _d(2010, 1, 1), _d(2020, 1, 1), real_pit.coverage_end):
        n = len(real_pit.members_as_of(probe))
        assert 480 <= n <= 520, f"{probe.date()}: {n} members, expected close to 500"


def test_real_fixture_pre_coverage_date_raises(real_pit: PointInTimeSP500) -> None:
    with pytest.raises(ValueError, match="before this fixture's earliest coverage"):
        real_pit.members_as_of(_d(1990, 1, 1))
