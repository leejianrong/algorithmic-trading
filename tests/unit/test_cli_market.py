"""``--market`` selection: one flag reaching all three EPIC-87 seams (ADR-0057).

Phase 1 landed the calendar (ADR-0054), the completeness policy (ADR-0053) and the
risk posture (ADR-0055) as library seams with ``cli.py`` untouched, so nothing
selected a market and a crypto run through the CLI got every equity default. These
tests pin the selection surface: what it resolves, what each seam actually receives,
how a preset composes with explicit risk flags, and the guard that refuses a
crypto-shaped symbol on a market that closes.

Two deliberate choices about *how* this is tested.

**The seams are observed where they are consumed, not where they are computed.** The
risk posture is captured off the ``Guardrails`` the CLI hands the engine, and the
completeness policy off the ``RecentWindowFeed`` the CLI builds — so a change that
resolves the market correctly and then forgets to pass it on turns these red. A test
that only called ``_resolve_market`` would stay green through exactly that bug.

**Each claim is tested against a source that can actually support it.** The
annualization claim ("nothing but the annualized figures moved") needs bars that are
*identical* on both markets, so it uses a written CSV: since ADR-0056 the synthetic
generator reads the calendar and emits a genuinely different continuous series for a
24/7 market, which is what a crypto fixture needs and what makes it the wrong control
for an only-the-basis-moved comparison. The continuous *shape* claim uses that
generator, end to end through the flag. The completeness rule is checked on a
hand-built bar against an explicit clock state, because it is a statement about a
policy, not about a series (ADR-0040's lesson: a stand-in more forgiving than the
thing it stands for cannot test the thing).
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from trading import cli
from trading.calendar import CALENDARS, CRYPTO_24_7, US_EQUITY, MarketCalendar
from trading.cli import app
from trading.config import CRYPTO_HALT_COOLDOWN_BARS, RiskConfig
from trading.dashboard.payload import load_payload
from trading.data.recent_window import RecentWindowFeed, default_is_complete, interval_is_complete
from trading.engine import BacktestResult, EquityPoint
from trading.frequency import Frequency
from trading.risk import Guardrails
from trading.sweep import run_sweep
from trading.types import Bar, Portfolio

runner = CliRunner()

_RANGE = ["--from", "2021-01-01", "--to", "2021-06-30"]
_EQUITY_SHARPE_RATIO = (CRYPTO_24_7.days_per_year / US_EQUITY.days_per_year) ** 0.5


def _backtest(out: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "sma_crossover",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--symbols",
            "AAA,BBB",
            "--no-plot",
            "--out",
            str(out),
            *_RANGE,
            *extra,
        ],
    )


def _paper(out: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "paper",
            "--strategy",
            "sma_crossover",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--symbols",
            "AAA,BBB",
            "--once",
            "--out",
            str(out),
            *_RANGE,
            *extra,
        ],
    )


def _sweep(out: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--symbols",
            "AAA,BBB",
            "--param",
            "fast=5,10",
            "--out",
            str(out),
            *_RANGE,
            *extra,
        ],
    )


def _one_bar_result() -> BacktestResult:
    """The smallest run ``result_to_dict`` will serialize."""
    return BacktestResult(
        symbols=["AAA"],
        starting_cash=100.0,
        equity_curve=[EquityPoint(datetime(2024, 1, 2, tzinfo=UTC), 100.0, 0.0)],
        final_portfolio=Portfolio(cash=100.0),
        fills=[],
    )


def _document(out: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((out.parent / "result.json").read_text())
    return document


def _sweep_rows(out: Path) -> list[dict[str, str]]:
    """The sweep results CSV, in the rank order it was written in."""
    with out.open(newline="") as fh:
        return list(csv.DictReader(fh))


# --- Resolving the choice -----------------------------------------------------


class TestResolvingTheMarket:
    """What ``--market`` accepts, and what it refuses."""

    def test_the_default_is_us_equity(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path / "e.csv")

        assert result.exit_code == 0, result.output
        assert _document(tmp_path / "e.csv")["market"] == US_EQUITY.name

    @pytest.mark.parametrize(
        ("given", "canonical"),
        [
            ("crypto", CRYPTO_24_7.name),
            ("crypto_24_7", CRYPTO_24_7.name),
            ("CRYPTO", CRYPTO_24_7.name),
            ("  crypto-24-7 ", CRYPTO_24_7.name),
            ("equity", US_EQUITY.name),
            ("us_equity", US_EQUITY.name),
        ],
    )
    def test_aliases_resolve_to_the_canonical_calendar_name(
        self, tmp_path: Path, given: str, canonical: str
    ) -> None:
        """An alias is input normalization only — never a second vocabulary.

        The canonical name is what is printed and written, so the registry stays the
        single spelling of a market (ADR-0054's registry, ADR-0057's flag).
        """
        out = tmp_path / "e.csv"
        result = _backtest(out, "--market", given)

        assert result.exit_code == 0, result.output
        assert _document(out)["market"] == canonical

    def test_an_unknown_market_exits_2_and_names_the_known_ones(self, tmp_path: Path) -> None:
        out = tmp_path / "e.csv"
        result = _backtest(out, "--market", "forex")

        assert result.exit_code == 2
        assert "unknown --market 'forex'" in result.output
        assert "us_equity" in result.output and "crypto_24_7" in result.output
        assert not out.exists(), "a bad market must not produce artifacts"

    def test_a_calendar_without_a_posture_is_refused_not_defaulted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No silent equity fallback, one layer up from ``get_calendar``.

        A future calendar registered without deciding its risk limits must not
        inherit the equity posture by accident — that is the ADR-0054 defect wearing
        different clothes, so the CLI refuses to select it.
        """
        futures = MarketCalendar("cme_futures", 252.0, 1380.0)
        monkeypatch.setitem(CALENDARS, futures.name, futures)

        result = _backtest(tmp_path / "e.csv", "--market", "cme_futures")

        assert result.exit_code == 2
        assert "no risk posture" in result.output
        assert "equity limits" in result.output


# --- Seam 1: the annualization calendar (ADR-0054) ----------------------------


class TestTheCalendarSeam:
    """``--market`` changes the annualization basis and nothing about the bars.

    The bars come from ``--source csv`` here, not from the synthetic generator, and
    that choice is load-bearing. Since ADR-0056 the synthetic adapter reads the
    calendar off the ``Frequency`` it is constructed with and generates a *different*
    (continuous) series for a 24/7 market — correct, and exactly what a crypto
    fixture needs, but it means a synthetic crypto run differs from its equity twin in
    two ways at once. A committed-shape CSV is identical on both markets by
    construction, so the annualization basis is the only variable and the claim
    "nothing but the annualized figures moved" is actually being tested.
    """

    @staticmethod
    def _write_csv(directory: Path, symbol: str, *, base: float) -> None:
        """A deterministic 60-bar daily series, calendar-independent by construction."""
        directory.mkdir(parents=True, exist_ok=True)
        rows = ["ts,open,high,low,close,volume"]
        for i in range(60):
            day = datetime(2021, 1, 4, tzinfo=UTC) + timedelta(days=i)
            # A gentle wave with drift, so returns are neither constant nor monotone.
            close = base * (1.0 + 0.004 * i + 0.01 * ((i % 7) - 3) / 3.0)
            rows.append(
                f"{day.date().isoformat()},{close:.4f},{close * 1.01:.4f},"
                f"{close * 0.99:.4f},{close:.4f},1000000"
            )
        (directory / f"{symbol}.csv").write_text("\n".join(rows) + "\n")

    def _csv_backtest(self, data_dir: Path, out: Path, *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "backtest",
                "--strategy",
                "buy_and_hold",
                "--source",
                "csv",
                "--cache-dir",
                str(data_dir),
                "--symbols",
                "AAA",
                "--no-plot",
                "--out",
                str(out),
                "--from",
                "2021-01-04",
                "--to",
                "2021-03-31",
                *extra,
            ],
        )

    def test_crypto_rescales_only_the_annualized_figures(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        self._write_csv(data, "AAA", base=100.0)
        equity_out = tmp_path / "equity" / "e.csv"
        crypto_out = tmp_path / "crypto" / "e.csv"

        assert self._csv_backtest(data, equity_out).exit_code == 0
        assert self._csv_backtest(data, crypto_out, "--market", "crypto").exit_code == 0

        equity, crypto = _document(equity_out), _document(crypto_out)
        # Same bars, same trades: the equity curve is byte-identical.
        assert equity_out.read_bytes() == crypto_out.read_bytes()
        # Total return and drawdown do not scale with periods_per_year at all —
        # which is exactly why a mis-annualized report is incoherent rather than
        # merely biased (ADR-0054).
        assert crypto["metrics"]["total_return"] == equity["metrics"]["total_return"]
        assert crypto["metrics"]["max_drawdown"] == equity["metrics"]["max_drawdown"]
        # Sharpe scales by sqrt(365/252) = 1.2035x on daily bars.
        assert crypto["metrics"]["sharpe"] == pytest.approx(
            equity["metrics"]["sharpe"] * _EQUITY_SHARPE_RATIO
        )
        assert crypto["metrics"]["sharpe"] != equity["metrics"]["sharpe"]

    def _csv_sweep(self, data_dir: Path, out: Path, *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--source",
                "csv",
                "--cache-dir",
                str(data_dir),
                "--symbols",
                "AAA",
                "--param",
                "fast=3,5",
                "--param",
                "slow=10,20",
                "--out",
                str(out),
                "--from",
                "2021-01-04",
                "--to",
                "2021-03-31",
                *extra,
            ],
        )

    def test_a_sweep_is_annualized_on_the_markets_basis(self, tmp_path: Path) -> None:
        """KAN-840: the same bars, the same ranking, a different year.

        ``sweep.py`` used to call ``metrics.compute(result)`` with no basis, so every
        trial took the 252.0 default however the bars were spaced or whatever market
        was chosen. The same CSV on both markets isolates the basis exactly as
        :meth:`test_crypto_rescales_only_the_annualized_figures` does for a backtest.
        """
        data = tmp_path / "data"
        self._write_csv(data, "AAA", base=100.0)
        equity_out = tmp_path / "equity.csv"
        crypto_out = tmp_path / "crypto.csv"

        assert self._csv_sweep(data, equity_out).exit_code == 0
        assert self._csv_sweep(data, crypto_out, "--market", "crypto").exit_code == 0

        equity = _sweep_rows(equity_out)
        crypto = _sweep_rows(crypto_out)
        assert len(equity) == len(crypto) == 4
        for slow, fast in zip(equity, crypto, strict=True):
            # Identical bars and fills: the ranking and the unscaled columns hold.
            assert (fast["fast"], fast["slow"]) == (slow["fast"], slow["slow"])
            assert fast["total_return"] == slow["total_return"]
            assert fast["max_drawdown"] == slow["max_drawdown"]
            # ...and the annualized ones move by sqrt(365/252) = 1.2035x.
            assert float(fast["sharpe"]) == pytest.approx(
                float(slow["sharpe"]) * _EQUITY_SHARPE_RATIO
            )
            assert float(fast["sharpe"]) != float(slow["sharpe"])

    def test_the_intraday_factor_is_the_bigger_one(self) -> None:
        """5m is 5.348x out on the factor, 2.3126x in every Sharpe (ADR-0054).

        Arithmetic on the two calendars the flag selects between, asserted here
        because no CLI source serves *identical* sub-daily bars on both markets: the
        only intraday-capable offline source is the synthetic generator, whose 24/7
        grid is a different series on purpose (ADR-0056).
        """
        equity_factor = Frequency.parse("5m").periods_per_year
        crypto_factor = Frequency.parse("5m", calendar=CRYPTO_24_7).periods_per_year

        assert equity_factor == 19_656
        assert crypto_factor == 105_120
        assert crypto_factor / equity_factor == pytest.approx(5.3480, abs=1e-4)
        assert (crypto_factor / equity_factor) ** 0.5 == pytest.approx(2.3126, abs=1e-4)

    def test_an_intraday_crypto_run_reaches_the_continuous_grid(self, tmp_path: Path) -> None:
        """The end-to-end shape ADR-0056 unblocked, through this flag.

        `--market crypto --interval 1h` is now a genuinely continuous run: the market
        selection reaches ``SyntheticAdapter``'s day shape through the ``Frequency``
        the CLI already passed it, with no CLI change of its own. The equity run over
        the same dates is confined to the cash session and skips the weekend, so this
        also shows the two really are different series — the reason the rescaling
        claim above uses a CSV.
        """
        out = tmp_path / "crypto" / "e.csv"
        equity_out = tmp_path / "equity" / "e.csv"
        intraday = ["--interval", "1h", "--from", "2021-06-04", "--to", "2021-06-07"]
        base = [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--source",
            "synthetic",
            "--symbols",
            "AAA",
            "--no-plot",
        ]

        assert (
            runner.invoke(
                app, [*base, "--out", str(out), "--market", "crypto", *intraday]
            ).exit_code
            == 0
        )
        assert runner.invoke(app, [*base, "--out", str(equity_out), *intraday]).exit_code == 0

        crypto_stamps = [
            datetime.fromisoformat(point["ts"]) for point in _document(out)["equity_curve"]
        ]
        equity_stamps = [
            datetime.fromisoformat(point["ts"]) for point in _document(equity_out)["equity_curve"]
        ]
        assert any(s.hour < 13 or s.hour >= 20 for s in crypto_stamps), (
            "a 24/7 market trades outside the US cash session"
        )
        assert all(13 <= s.hour < 20 for s in equity_stamps)
        # 2021-06-05/06 is a weekend: present on 24/7, absent on the equity calendar.
        assert any(s.weekday() >= 5 for s in crypto_stamps)
        assert not any(s.weekday() >= 5 for s in equity_stamps)
        assert len(crypto_stamps) > len(equity_stamps)

    def test_result_json_says_which_market_it_was(self, tmp_path: Path) -> None:
        """The reader must not have to remember. ADR-0054 recorded this gap itself."""
        out = tmp_path / "e.csv"
        assert _backtest(out, "--market", "crypto").exit_code == 0

        document = _document(out)
        assert document["frequency"] == "1d"
        assert document["market"] == CRYPTO_24_7.name
        assert document["schema_version"] == 1, "the market key is additive"
        # The dashboard's exact-equality schema check still accepts the document, and
        # carries the market through verbatim (it does not yet *render* it — a known
        # gap in ADR-0057, one line in static_export.py's summary rows).
        payload = load_payload(out.parent / "result.json")
        assert payload["document"]["market"] == CRYPTO_24_7.name

    def test_paper_records_the_market_too(self, tmp_path: Path) -> None:
        assert _paper(tmp_path, "--market", "crypto").exit_code == 0

        assert json.loads((tmp_path / "result.json").read_text())["market"] == CRYPTO_24_7.name

    def test_an_equity_run_prints_no_market_line_and_a_crypto_run_does(
        self, tmp_path: Path
    ) -> None:
        """The line appears only when there is something to say (ADR-0032's rule)."""
        equity = _backtest(tmp_path / "equity" / "e.csv")
        crypto = _backtest(tmp_path / "crypto" / "e.csv", "--market", "crypto")

        assert "Market:" not in equity.output
        assert "Market:        crypto_24_7" in crypto.output
        assert "365 bars/year" in crypto.output
        assert f"halt re-arms after {CRYPTO_HALT_COOLDOWN_BARS} bar(s)" in crypto.output

    def test_the_derived_benchmark_block_uses_the_markets_basis(self, tmp_path: Path) -> None:
        """The figure ADR-0054 named specifically, in ``result.json``'s own words.

        ``result_to_dict`` derives ``benchmark_metrics`` (annualized alpha, IR) from
        the two curves it holds, resolving the basis from the interval label — which
        on the equity calendar is exactly the silent wrong answer the recorded gap
        described. It now resolves on the run's market.
        """
        data = tmp_path / "data"
        self._write_csv(data, "AAA", base=100.0)
        self._write_csv(data, "BBB", base=50.0)
        equity_out = tmp_path / "equity" / "e.csv"
        crypto_out = tmp_path / "crypto" / "e.csv"
        bench = ["--benchmark", "BBB"]

        assert self._csv_backtest(data, equity_out, *bench).exit_code == 0
        assert self._csv_backtest(data, crypto_out, *bench, "--market", "crypto").exit_code == 0

        equity, crypto = _document(equity_out), _document(crypto_out)
        assert (
            equity["benchmark_metrics"]["shared_bars"] == crypto["benchmark_metrics"]["shared_bars"]
        )
        # Beta and correlation are basis-free; alpha is annualized, so it scales.
        assert crypto["benchmark_metrics"]["beta"] == equity["benchmark_metrics"]["beta"]
        assert crypto["benchmark_metrics"]["alpha"] == pytest.approx(
            equity["benchmark_metrics"]["alpha"]
            * CRYPTO_24_7.days_per_year
            / US_EQUITY.days_per_year
        )

    def test_an_unknown_market_in_the_document_raises_rather_than_defaulting(self) -> None:
        """``result_to_dict`` inherits ``get_calendar``'s refusal (ADR-0054).

        A caller that hands over a market nobody has registered gets a ValueError,
        not an equity-annualized document that looks fine.
        """
        from trading.report import result_to_dict

        result = _one_bar_result()
        with pytest.raises(ValueError, match="unknown market calendar"):
            result_to_dict(result, mode="backtest", market="atlantis", benchmark_curve=[])


# --- Seam 2: the completeness policy (ADR-0053) -------------------------------


def _bar(ts: datetime) -> Bar:
    return Bar(symbol="AAA", ts=ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=1)


class TestTheCompletenessSeam:
    """Which rule the paper feed is handed, per market and per interval.

    The discriminating instant comes from ADR-0053's sweep: a daily bar stamped
    13:00 UTC, judged at 00:30 the next day. The session rule says complete (the UTC
    date turned over); the rolling-24-hour rule says not yet (``ts + 1d`` is 13:00).
    Every disagreement between the two runs that way round, so on a market that
    never closes the session rule hands the strategy a *forming* bar.
    """

    OFF_MIDNIGHT = _bar(datetime(2021, 3, 1, 13, 0, tzinfo=UTC))
    NEXT_DAY_EARLY = datetime(2021, 3, 2, 0, 30, tzinfo=UTC)

    def _captured_policy(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str) -> Any:
        captured: list[Any] = []
        real = RecentWindowFeed

        def spy(adapter: Any, clock: Any, is_complete: Any = default_is_complete, **kw: Any) -> Any:
            captured.append(is_complete)
            return real(adapter, clock, is_complete, **kw)

        monkeypatch.setattr("trading.cli.RecentWindowFeed", spy)
        result = _paper(tmp_path, *extra)
        assert result.exit_code == 0, result.output
        assert captured, "the CLI must build a feed"
        return captured[0]

    def test_the_two_rules_really_disagree_on_this_bar(self) -> None:
        """Guard the fixture: a test whose two branches agreed would prove nothing."""
        assert default_is_complete(self.OFF_MIDNIGHT, self.NEXT_DAY_EARLY) is True
        assert (
            interval_is_complete(timedelta(days=1))(self.OFF_MIDNIGHT, self.NEXT_DAY_EARLY) is False
        )

    def test_a_daily_equity_session_keeps_the_session_rule(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        policy = self._captured_policy(monkeypatch, tmp_path)

        assert policy is default_is_complete
        assert policy(self.OFF_MIDNIGHT, self.NEXT_DAY_EARLY) is True

    def test_a_daily_continuous_market_drops_the_session_special_case(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        policy = self._captured_policy(monkeypatch, tmp_path, "--market", "crypto")

        assert policy is not default_is_complete
        assert policy(self.OFF_MIDNIGHT, self.NEXT_DAY_EARLY) is False, (
            "a 24/7 daily bar is complete at ts + 24h, not when the UTC date turns"
        )
        # And it is complete once the window really has elapsed.
        assert policy(self.OFF_MIDNIGHT, datetime(2021, 3, 2, 13, 0, tzinfo=UTC)) is True

    @pytest.mark.parametrize("market", ["equity", "crypto"])
    def test_sub_daily_uses_the_interval_rule_on_either_market(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, market: str
    ) -> None:
        """ADR-0022's rule needs no calendar, so this axis was already market-free."""
        policy = self._captured_policy(
            monkeypatch,
            tmp_path,
            "--interval",
            "5m",
            "--market",
            market,
        )

        assert policy is not default_is_complete
        five_m = _bar(datetime(2021, 3, 1, 14, 0, tzinfo=UTC))
        assert policy(five_m, datetime(2021, 3, 1, 14, 4, tzinfo=UTC)) is False
        assert policy(five_m, datetime(2021, 3, 1, 14, 5, tzinfo=UTC)) is True


# --- Seam 3: the risk posture (ADR-0055) --------------------------------------


class TestTheRiskPostureSeam:
    """What the guardrails on the engine's order path were actually configured with."""

    def _captured_config(
        self, monkeypatch: pytest.MonkeyPatch, run: Any, *extra: str
    ) -> RiskConfig:
        captured: list[RiskConfig] = []

        def spy(config: RiskConfig) -> Guardrails:
            captured.append(config)
            return Guardrails(config)

        monkeypatch.setattr("trading.cli.Guardrails", spy)
        result = run(*extra)
        assert result.exit_code == 0, result.output
        assert captured, "the CLI must build guardrails"
        return captured[0]

    def test_the_default_market_is_exactly_the_historical_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = self._captured_config(monkeypatch, lambda *a: _backtest(tmp_path / "e.csv", *a))

        assert config == RiskConfig()
        assert config == RiskConfig.equity()

    def test_crypto_selects_the_bounded_halt_posture(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = self._captured_config(
            monkeypatch, lambda *a: _backtest(tmp_path / "e.csv", *a), "--market", "crypto"
        )

        assert config == RiskConfig.crypto()
        # Nothing widened: the posture differs from equity in one field (ADR-0055).
        assert config.max_position_pct == RiskConfig().max_position_pct
        assert config.max_gross_exposure == RiskConfig().max_gross_exposure
        assert config.max_drawdown_pct == RiskConfig().max_drawdown_pct
        assert config.halt_cooldown_bars == CRYPTO_HALT_COOLDOWN_BARS

    def test_the_posture_reaches_a_paper_session_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = self._captured_config(
            monkeypatch, lambda *a: _paper(tmp_path, *a), "--market", "crypto"
        )

        assert config == RiskConfig.crypto()

    def test_the_posture_reaches_a_sweep_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A sweep runs the engine N times; an equity latch would hit every trial."""
        captured: list[RiskConfig | None] = []
        real = run_sweep

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.append(kwargs.get("risk"))
            return real(*args, **kwargs)

        monkeypatch.setattr("trading.cli.run_sweep", spy)
        result = _sweep(tmp_path / "sweep.csv", "--market", "crypto")

        assert result.exit_code == 0, result.output
        assert captured == [RiskConfig.crypto()]


class TestPrecedenceOfFlagsOverThePreset:
    """A preset and explicit flags compose one way: the flag wins, always."""

    def _config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str) -> RiskConfig:
        captured: list[RiskConfig] = []

        def spy(config: RiskConfig) -> Guardrails:
            captured.append(config)
            return Guardrails(config)

        monkeypatch.setattr("trading.cli.Guardrails", spy)
        result = _backtest(tmp_path / "e.csv", *extra)
        assert result.exit_code == 0, result.output
        return captured[0]

    def test_an_explicit_cap_overrides_the_posture_and_keeps_the_rest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = self._config(monkeypatch, tmp_path, "--market", "crypto", "--max-drawdown", "0.35")

        assert config.max_drawdown_pct == 0.35
        assert config.halt_cooldown_bars == CRYPTO_HALT_COOLDOWN_BARS, (
            "overriding one limit must not silently drop the rest of the posture"
        )

    def test_an_explicit_cooldown_overrides_the_postures_cooldown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = self._config(
            monkeypatch, tmp_path, "--market", "crypto", "--halt-cooldown-bars", "7"
        )

        assert config.halt_cooldown_bars == 7

    def test_an_explicit_cooldown_still_works_on_the_equity_market(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ADR-0031's opt-in recovery is unchanged: unset means latch on equity."""
        latching = self._config(monkeypatch, tmp_path)
        assert latching.halt_cooldown_bars is None
        assert latching.halt_recovery_enabled is False

        recovering = self._config(monkeypatch, tmp_path, "--halt-cooldown-bars", "40")
        assert recovering.halt_cooldown_bars == 40

    def test_no_guardrails_beats_the_posture(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An opt-out from enforcement is an opt-out from the market's posture too."""
        config = self._config(monkeypatch, tmp_path, "--market", "crypto", "--no-guardrails")

        assert config == RiskConfig.unlimited()

    def test_a_crypto_run_cannot_be_talked_into_a_latching_halt(self, tmp_path: Path) -> None:
        """There is no CLI spelling of "cooldown None" — the closest is refused.

        ``RiskConfig.crypto(halt_cooldown_bars=None)`` is a ValueError for the same
        reason (ADR-0055): a 24/7 posture whose kill switch is permanent is the thing
        the preset exists to prevent. From the CLI, unset means "the posture's 30"
        and 0 is an invalid limit, so the latch is unreachable rather than one flag
        away.
        """
        result = _backtest(tmp_path / "e.csv", "--market", "crypto", "--halt-cooldown-bars", "0")

        assert result.exit_code == 2
        assert "halt_cooldown_bars" in result.output


# --- The guard: crypto-shaped symbols on a market that closes -----------------


class TestCryptoShapedSymbolGuard:
    """Explicit is honest and forgettable, so forgetting is caught (ADR-0057)."""

    @pytest.mark.parametrize("symbol", ["BTC/USD", "BTC-USD", "ETH/USDT", "eth/btc", "SOL_USDC"])
    def test_a_pair_shape_is_recognized(self, symbol: str) -> None:
        assert cli._crypto_shaped(symbol) is not None

    @pytest.mark.parametrize(
        "symbol",
        [
            "AAPL",
            "BRK-B",  # yfinance share class
            "BRK/B",  # Bloomberg-style share class
            "BF-B",
            "SPY",
            "USD",  # no separator, so no pair
            "GOOGL",
        ],
    )
    def test_an_equity_ticker_is_never_flagged(self, symbol: str) -> None:
        """The rule is narrow on purpose: a false positive would block a real run."""
        assert cli._crypto_shaped(symbol) is None

    def test_every_curated_basket_is_flagged_exactly_as_its_market_requires(self) -> None:
        """The shipped universes must stay runnable — checked, not assumed.

        Strengthened when ``crypto10`` landed (ADR-0058). Until then every basket
        was an equity one and "nothing is flagged" said all there was to say; a
        crypto basket makes the guard's *two* claims separable, and both matter:

        * a session-market basket must never be flagged, or the guard blocks a
          legitimate run (the ``BRK-B`` false-positive worry, ADR-0057);
        * a **pair** basket must be flagged on every symbol, or ``--market
          crypto`` is silently forgettable for the one universe that most needs
          it — the shape guard's whole reason to exist.

        Asserting "no basket is flagged" would now have to be *weakened* to keep
        passing, which is the shape of a test quietly going quiet.
        """
        from trading.universe import BASKETS

        pair_baskets = {"crypto10"}
        assert pair_baskets <= set(BASKETS), "a pair basket must exist for this to mean anything"
        for name, basket in BASKETS.items():
            expect_flagged = name in pair_baskets
            for symbol in basket.symbols:
                flagged = cli._crypto_shaped(symbol) is not None
                assert flagged is expect_flagged, f"{name}: {symbol}"

    def test_a_backtest_refuses_pair_symbols_under_the_equity_market(self, tmp_path: Path) -> None:
        out = tmp_path / "e.csv"
        result = _backtest(out, "--symbols", "BTC/USD,ETH-USD,AAA")

        assert result.exit_code == 2
        assert "look like crypto pairs" in result.output
        assert "BTC/USD" in result.output and "ETH-USD" in result.output
        assert "AAA" not in result.output.split("look like crypto pairs")[1].split("On this")[0]
        assert "--market crypto" in result.output
        assert not out.exists(), "the refusal happens before anything is written"

    def test_the_same_symbols_run_under_the_crypto_market(self, tmp_path: Path) -> None:
        out = tmp_path / "e.csv"
        result = _backtest(out, "--symbols", "BTC/USD,ETH-USD", "--market", "crypto")

        assert result.exit_code == 0, result.output
        assert _document(out)["symbols"] == ["BTC/USD", "ETH-USD"]

    def test_paper_refuses_them_too(self, tmp_path: Path) -> None:
        result = _paper(tmp_path, "--symbols", "BTC/USD")

        assert result.exit_code == 2
        assert "look like crypto pairs" in result.output

    def test_sweep_refuses_them_too(self, tmp_path: Path) -> None:
        result = _sweep(tmp_path / "sweep.csv", "--symbols", "BTC/USD")

        assert result.exit_code == 2
        assert "look like crypto pairs" in result.output

    def test_the_guard_is_one_directional(self, tmp_path: Path) -> None:
        """An equity-looking ticker under ``--market crypto`` is allowed.

        Deliberate asymmetry: the operator typed the market, and a legitimate
        continuous symbol can be a bare ``BTC`` with no separator, so a check the
        other way would fire on correct usage. Recorded in ADR-0057 as the accepted
        hole rather than papered over.
        """
        out = tmp_path / "e.csv"
        result = _backtest(out, "--symbols", "AAA,BBB", "--market", "crypto")

        assert result.exit_code == 0, result.output


# --- Which commands the market reaches ----------------------------------------


class TestUnsupportedSurfacesError:
    """An unsupported combination must error, never be accepted and dropped."""

    def test_gen_data_has_no_market_flag(self, tmp_path: Path) -> None:
        """Generation is the sibling card's axis (KAN-830), so the flag is absent."""
        result = runner.invoke(
            app,
            [
                "gen-data",
                "--symbols",
                "AAA",
                "--market",
                "crypto",
                "--out-dir",
                str(tmp_path),
                *_RANGE,
            ],
        )

        assert result.exit_code == 2
        assert "No such option" in result.output

    def test_dashboard_has_no_market_flag(self, tmp_path: Path) -> None:
        """The market is a property of the ``result.json`` it reads, not an input."""
        result = runner.invoke(
            app, ["dashboard", "--market", "crypto", "--static", str(tmp_path / "d.html")]
        )

        assert result.exit_code == 2
        assert "No such option" in result.output

    def test_a_sweep_no_longer_carries_a_basis_caveat(self, tmp_path: Path) -> None:
        """The market now *does* reach a sweep's per-run metrics (KAN-840).

        This surface used to print a caveat naming the one figure ``--market`` could
        not reach, because ``sweep.py`` annualized every trial at 252. The basis is
        threaded now, so the caveat would be a false statement rather than an honest
        one — the claim it made is asserted positively in
        :meth:`TestTheCalendarSeam.test_a_sweep_is_annualized_on_the_markets_basis`.
        """
        result = _sweep(tmp_path / "sweep.csv", "--market", "crypto")

        assert result.exit_code == 0, result.output
        assert "Market:" in result.output
        assert "annualized on the us_equity basis" not in result.output

    def test_an_equity_sweep_prints_neither_line(self, tmp_path: Path) -> None:
        result = _sweep(tmp_path / "sweep.csv")

        assert result.exit_code == 0, result.output
        assert "Market:" not in result.output
        assert "annualized on the us_equity basis" not in result.output
