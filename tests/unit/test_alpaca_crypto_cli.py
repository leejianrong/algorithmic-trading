"""CLI wiring for the crypto venue: what `--market crypto --source alpaca` builds.

Fast and offline. Nothing here reaches Alpaca — the point is to pin the *choices*
`cli.py` makes before any request goes out, because two of them were wrong and
both were caught here rather than live:

* ``--data-feed`` defaulted to ``iex`` for every live Alpaca run (ADR-0034), which
  is a field ``CryptoBarsRequest`` does not have. It made
  ``paper --market crypto --broker alpaca --live`` fail at client construction.
* The adapter and broker had no way to learn which venue they were talking to, so
  a crypto run would have gone to the stock tape and collected ``invalid symbol``
  on every bar.

The claim these tests exist to defend is that **the venue is derived from the
market's calendar and from nothing else** (ADR-0058, reusing ADR-0056's argument):
no ``--asset-class`` flag, no symbol sniffing, so "crypto bars annualized on a
252-day year" is not representable.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading import cli
from trading.calendar import CRYPTO_24_7, US_EQUITY, MarketCalendar
from trading.cli import app
from trading.data.alpaca_adapter import AlpacaAdapter
from trading.data.alpaca_client import ASSET_CLASS_CRYPTO, ASSET_CLASS_US_EQUITY
from trading.frequency import Frequency

runner = CliRunner()


class TestNoAssetClassFlagExists:
    """The combination ADR-0058 removes rather than documents."""

    def test_the_cli_has_no_asset_class_option(self) -> None:
        """A second flag would put ADR-0054's defect one keyword away.

        `--market` already fixes the calendar (ADR-0054), the completeness rule
        (ADR-0053) and the risk posture (ADR-0055). Letting the *venue* be chosen
        separately would make "Alpaca crypto bars, equity year" expressible again.
        """
        for command in ("backtest", "paper", "sweep"):
            result = runner.invoke(app, [command, "--help"])
            assert "--asset-class" not in result.output, command
            assert "--venue" not in result.output, command

    def test_the_adapter_derives_the_venue_from_the_calendar(self) -> None:
        params = inspect.signature(AlpacaAdapter.__init__).parameters
        assert "calendar" in params
        assert "asset_class" not in params, "the venue must not be separately selectable"

    def test_the_broker_derives_the_venue_from_the_calendar(self) -> None:
        from trading.brokers.alpaca import AlpacaBroker

        params = inspect.signature(AlpacaBroker.__init__).parameters
        assert "calendar" in params
        assert "asset_class" not in params


class TestAdapterConstructionPicksTheVenue:
    """`_make_adapter` already had the frequency, which already had the calendar."""

    @pytest.mark.parametrize(
        ("calendar", "expected"),
        [(US_EQUITY, ASSET_CLASS_US_EQUITY), (CRYPTO_24_7, ASSET_CLASS_CRYPTO)],
    )
    def test_the_calendar_selects_the_asset_class(
        self, calendar: MarketCalendar, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted through the real `_make_adapter`, on a recorded construction.

        The client itself is never built (it would need the SDK and a key), so the
        adapter's own record of which venue it chose is what gets checked.
        """
        recorded: dict[str, object] = {}

        class _Recorder(AlpacaAdapter):
            def __init__(self, **kwargs: object) -> None:
                recorded.update(kwargs)
                self._asset_class = (
                    ASSET_CLASS_CRYPTO
                    if getattr(kwargs["calendar"], "is_continuous", False)
                    else ASSET_CLASS_US_EQUITY
                )

        monkeypatch.setattr(cli, "AlpacaAdapter", _Recorder)
        freq = Frequency.parse("1d", calendar=calendar)
        adapter = cli._make_adapter("alpaca", Path("."), 1, freq)

        assert recorded["calendar"] is calendar
        assert getattr(adapter, "_asset_class") == expected  # noqa: B009

    def test_the_interval_still_reaches_the_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADR-0022's property must not have been displaced by the calendar."""
        recorded: dict[str, object] = {}
        monkeypatch.setattr(
            cli, "AlpacaAdapter", lambda **kwargs: recorded.update(kwargs) or object()
        )
        cli._make_adapter("alpaca", Path("."), 1, Frequency.parse("5m", calendar=CRYPTO_24_7))
        assert recorded["interval"] == timedelta(minutes=5)


class TestDataFeedIsEquityOnly:
    """ADR-0034 predates any second venue; its default had to be narrowed."""

    def test_an_explicit_data_feed_on_the_crypto_venue_is_a_clean_cli_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 2 with an explanation, never a traceback and never silently ignored.

        Ignoring it would let an operator believe they had chosen a tape. There is
        no tape to choose: `CryptoBarsRequest` has no `feed` field at all.

        Credentials are faked so the refusal is reached; it is raised *before* the
        SDK import, so this stays offline whether or not `alpaca-py` is installed.
        """
        monkeypatch.setenv("ALPACA_API_KEY", "not-a-real-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "not-a-real-secret")
        result = runner.invoke(
            app,
            [
                "paper",
                "--market",
                "crypto",
                "--source",
                "alpaca",
                "--data-feed",
                "iex",
                "--symbols",
                "BTC/USD",
                "--from",
                "2026-01-01",
                "--to",
                "2026-02-01",
                "--strategy",
                "sma_crossover",
                "--once",
                "--out",
                str(tmp_path / "paper"),
            ],
        )
        assert result.exit_code == 2, result.output
        assert "no feed field" in result.output

    def test_data_feed_still_rejects_a_non_alpaca_source(self, tmp_path: Path) -> None:
        """The pre-existing guard is untouched."""
        result = runner.invoke(
            app,
            [
                "paper",
                "--source",
                "synthetic",
                "--data-feed",
                "iex",
                "--symbols",
                "AAPL",
                "--from",
                "2021-01-01",
                "--to",
                "2021-02-01",
                "--strategy",
                "sma_crossover",
                "--once",
                "--out",
                str(tmp_path / "paper"),
            ],
        )
        assert result.exit_code == 2
        assert "--data-feed applies only to --source alpaca" in result.output

    def test_the_live_iex_default_is_conditioned_on_a_session_market(self) -> None:
        """Read off the source, because the live path cannot run in a fast test.

        The default itself is ADR-0034's and must stay for equities; what changed
        is that it no longer fires on a continuous market, or
        `paper --market crypto --broker alpaca --live` cannot start at all.

        Located by **AST**, not by string slicing. The first version of this test
        split the file around a literal `if data_feed is None`, and when ruff
        reformatted the widened condition across several lines that substring
        stopped existing — so the search fell back to the whole file, found
        `is_continuous` somewhere unrelated, and passed no matter what. It was
        caught by mutation testing: deleting the guard left it green. A test whose
        failure mode is "silently matches everything" is worse than no test.
        """
        import ast

        tree = ast.parse(Path(cli.__file__).read_text())
        guards = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and any(
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value == "iex"
                for stmt in node.body
            )
        ]
        assert len(guards) == 1, f"expected exactly one `data_feed = 'iex'` guard, got {guards}"
        condition = ast.unparse(guards[0].test)
        assert "is_continuous" in condition, (
            "the live IEX default is no longer conditioned on the market closing; "
            f"paper --market crypto --broker alpaca --live cannot start. Got: {condition}"
        )
        assert "live" in condition and "alpaca" in condition, (
            f"ADR-0034's original conditions were lost: {condition}"
        )


class TestCryptoBasketReachesTheCli:
    """`@crypto10` expands, and only under a continuous market."""

    def test_the_basket_expands(self) -> None:
        from trading.universe import get_universe

        symbols = get_universe("crypto10")
        assert len(symbols) == 10
        assert all("/" in s for s in symbols), "the data API only accepts the slash form"
        assert symbols[0] == "BTC/USD"

    def test_every_symbol_has_a_sector(self) -> None:
        from trading.universe import get_sector_map, get_universe

        sectors = get_sector_map("crypto10")
        assert set(sectors) == set(get_universe("crypto10"))
        assert len(set(sectors.values())) > 1, "a single bucket makes the sector cap a no-op"

    def test_the_basket_is_refused_under_a_session_market(self, tmp_path: Path) -> None:
        """ADR-0057's shape guard, now with a basket that is genuinely crypto-shaped.

        Exit 2 before any fetch, naming the fix. This is the case the guard was
        written for and could not previously be exercised with a curated basket.
        """
        result = runner.invoke(
            app,
            [
                "backtest",
                "--source",
                "synthetic",
                "--symbols",
                "@crypto10",
                "--from",
                "2021-01-01",
                "--to",
                "2021-03-01",
                "--strategy",
                "sma_crossover",
                "--out",
                str(tmp_path / "e.csv"),
            ],
        )
        assert result.exit_code == 2, result.output
        assert "look like crypto pairs" in result.output
        assert "--market crypto" in result.output
        assert not (tmp_path / "e.csv").exists()

    def test_it_runs_under_the_crypto_market(self, tmp_path: Path) -> None:
        """Offline, on the synthetic continuous generator (ADR-0056)."""
        out = tmp_path / "e.csv"
        result = runner.invoke(
            app,
            [
                "backtest",
                "--market",
                "crypto",
                "--source",
                "synthetic",
                "--symbols",
                "@crypto10",
                "--from",
                "2021-01-01",
                "--to",
                "2021-04-01",
                "--strategy",
                "cross_sectional",
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert "crypto_24_7 (365 days x 1440 min/day)" in result.output
        assert "halt re-arms after 30 bar(s)" in result.output, "ADR-0055's posture"
