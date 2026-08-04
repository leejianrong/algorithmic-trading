"""Unit tests for the curated stock-basket registry (universe.py).

The broker-verification half (ADR-0028) runs entirely against
:class:`~trading.data.alpaca_client.FakeAlpacaClient`: no network, no credentials.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from trading.data.alpaca_client import AssetInfo, FakeAlpacaClient
from trading.universe import (
    BASKETS,
    REASON_NOT_FRACTIONABLE,
    REASON_NOT_TRADABLE,
    REASON_UNVERIFIED,
    AssetSource,
    DroppedSymbol,
    get_sector_map,
    get_universe,
    validate_universe,
)


def test_blue20_has_exactly_twenty_symbols() -> None:
    symbols = get_universe("blue20")
    assert len(symbols) == 20
    # No accidental duplicates.
    assert len(set(symbols)) == 20


def test_blue20_sector_map_covers_all_and_only_the_symbols() -> None:
    symbols = get_universe("blue20")
    sectors = get_sector_map("blue20")
    assert set(sectors) == set(symbols)
    # The curated spread is 8 named sectors.
    assert set(sectors.values()) == {
        "tech",
        "comms",
        "discretionary",
        "financials",
        "health",
        "staples",
        "energy",
        "industrials",
    }


def test_get_universe_round_trips_registry() -> None:
    assert get_universe("blue20") == list(BASKETS["blue20"].symbols)


def test_get_sector_map_round_trips_registry() -> None:
    assert get_sector_map("blue20") == dict(BASKETS["blue20"].sectors)


def test_getters_return_fresh_copies() -> None:
    # Mutating a returned collection must not corrupt the registry.
    get_universe("blue20").append("BOGUS")
    get_sector_map("blue20")["BOGUS"] = "junk"
    assert "BOGUS" not in get_universe("blue20")
    assert "BOGUS" not in get_sector_map("blue20")


def test_unknown_universe_raises_keyerror_naming_known_baskets() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_universe("nope")
    assert "blue20" in str(excinfo.value)


def test_unknown_sector_map_raises_keyerror_naming_known_baskets() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_sector_map("nope")
    assert "blue20" in str(excinfo.value)


# --- Broker verification (ADR-0028) -------------------------------------------


def test_universe_module_has_no_runtime_dependency_on_the_alpaca_lane() -> None:
    # The AssetInfo annotation is TYPE_CHECKING-only, so importing the basket
    # registry must not drag the broker module in (ADR-0028).
    code = (
        "import sys; import trading.universe; "
        "assert 'trading.data.alpaca_client' not in sys.modules, sorted(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_fake_client_satisfies_asset_source_structurally() -> None:
    # The seam is structural: mypy checks the assignment, so universe.py needs no
    # runtime import of the Alpaca module.
    source: AssetSource = FakeAlpacaClient()
    assert source.get_asset("AAPL").tradable is True


class TestValidateUniverse:
    def test_all_good_universe_passes_intact(self) -> None:
        symbols = ["AAPL", "MSFT", "NVDA"]
        result = validate_universe(symbols, FakeAlpacaClient())

        assert result.usable == ("AAPL", "MSFT", "NVDA")  # order preserved
        assert result.requested == ("AAPL", "MSFT", "NVDA")
        assert result.unusable == ()
        assert result.unverified == ()
        assert result.dropped == ()
        assert result.is_clean is True

    def test_non_fractionable_symbol_dropped_with_reason(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("BRK.A", fractionable=False)

        result = validate_universe(["AAPL", "BRK.A"], client)

        assert result.usable == ("AAPL",)
        assert [d.symbol for d in result.unusable] == ["BRK.A"]
        drop = result.unusable[0]
        assert drop.reason == REASON_NOT_FRACTIONABLE
        assert "fractionable" in drop.detail
        assert result.unverified == ()
        assert result.is_clean is False

    def test_untradable_symbol_dropped_with_reason(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("HALT", tradable=False)

        result = validate_universe(["HALT", "AAPL"], client)

        assert result.usable == ("AAPL",)
        assert result.unusable[0].reason == REASON_NOT_TRADABLE
        assert result.unusable[0].symbol == "HALT"

    def test_untradable_wins_over_fractionable_reason(self) -> None:
        # A name that is neither tradable nor fractionable reports the harder stop.
        client = FakeAlpacaClient()
        client.set_asset("DEAD", tradable=False, fractionable=False)
        result = validate_universe(["DEAD"], client)
        assert result.unusable[0].reason == REASON_NOT_TRADABLE

    def test_lookup_failure_is_unverified_not_unusable(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset_failure("FLAKY", "connection reset")

        result = validate_universe(["AAPL", "FLAKY"], client)

        # Excluded from the usable set (absent permission is not permission)...
        assert result.usable == ("AAPL",)
        # ...but reported distinctly, so a network blip never reads as a delisting.
        assert result.unusable == ()
        assert [d.symbol for d in result.unverified] == ["FLAKY"]
        assert result.unverified[0].reason == REASON_UNVERIFIED
        assert "connection reset" in result.unverified[0].detail
        assert result.is_clean is False

    def test_unusable_and_unverified_are_separate_buckets(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("BRK.A", fractionable=False)
        client.set_asset_failure("FLAKY")

        result = validate_universe(["AAPL", "BRK.A", "FLAKY"], client)

        assert {d.symbol for d in result.unusable} == {"BRK.A"}
        assert {d.symbol for d in result.unverified} == {"FLAKY"}
        # `dropped` is the union, verified-unusable first.
        assert [d.symbol for d in result.dropped] == ["BRK.A", "FLAKY"]

    def test_every_requested_symbol_is_accounted_for(self) -> None:
        # The core honesty guarantee: nothing is silently filtered.
        client = FakeAlpacaClient()
        client.set_asset("BRK.A", fractionable=False)
        client.set_asset("HALT", tradable=False)
        client.set_asset_failure("FLAKY")
        symbols = ["AAPL", "BRK.A", "HALT", "FLAKY", "MSFT"]

        result = validate_universe(symbols, client)

        accounted = set(result.usable) | {d.symbol for d in result.dropped}
        assert accounted == set(symbols)
        assert len(result.usable) + len(result.dropped) == len(symbols)

    def test_duplicates_collapse_to_one_lookup(self) -> None:
        calls: list[str] = []

        class CountingSource:
            def get_asset(self, symbol: str) -> AssetInfo:
                calls.append(symbol)
                return AssetInfo(symbol=symbol, tradable=True, fractionable=True)

        result = validate_universe(["AAPL", "AAPL", "MSFT"], CountingSource())
        assert result.requested == ("AAPL", "MSFT")
        assert result.usable == ("AAPL", "MSFT")
        assert calls == ["AAPL", "MSFT"]

    def test_empty_universe_is_clean_and_empty(self) -> None:
        result = validate_universe([], FakeAlpacaClient())
        assert result.usable == ()
        assert result.is_clean is True

    def test_report_lines_name_every_dropped_symbol_and_reason(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("BRK.A", fractionable=False)
        client.set_asset_failure("FLAKY")

        text = "\n".join(validate_universe(["AAPL", "BRK.A", "FLAKY"], client).report_lines())

        assert "1/3 symbols usable" in text
        assert "BRK.A" in text and REASON_NOT_FRACTIONABLE in text
        assert "FLAKY" in text and REASON_UNVERIFIED in text
        assert "unverified symbols are unknown, not rejected" in text


class TestBlue20AgainstBroker:
    def test_blue20_validates_clean_against_an_all_good_broker(self) -> None:
        symbols = get_universe("blue20")
        result = validate_universe(symbols, FakeAlpacaClient())
        assert result.is_clean is True
        assert list(result.usable) == symbols

    def test_one_bad_blue20_name_shrinks_the_usable_universe(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("LLY", fractionable=False)
        result = validate_universe(get_universe("blue20"), client)
        assert "LLY" not in result.usable
        assert len(result.usable) == 19
        assert result.unusable[0] == DroppedSymbol(
            symbol="LLY",
            reason=REASON_NOT_FRACTIONABLE,
            detail=result.unusable[0].detail,
        )


class TestDroppedSymbol:
    def test_rejects_unknown_reason(self) -> None:
        with pytest.raises(ValueError, match="unknown drop reason"):
            DroppedSymbol(symbol="AAPL", reason="vibes", detail="because")

    def test_rejects_empty_symbol(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            DroppedSymbol(symbol="", reason=REASON_UNVERIFIED, detail="because")

    def test_rejects_missing_detail(self) -> None:
        with pytest.raises(ValueError, match="detail"):
            DroppedSymbol(symbol="AAPL", reason=REASON_UNVERIFIED, detail="")
