"""Fast CLI tests for ``trading paper --divergence`` (ADR-0038).

Offline throughout: ``--source synthetic`` plus the default ``--once`` replay, so
there is no network, no credentials, and no wall-clock wait. The point of the file
is the *default*: enabling divergence tracking must add artifacts and change
nothing else, and leaving it off must produce exactly the run that existed before
the flag did.

No assertion here reads ``--help`` output: rendered help wraps at the terminal
width and broke CI once.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner, Result

from trading.cli import app

runner = CliRunner()

_ARTIFACTS = ("equity_curve.csv", "result.json")


def _invoke(out: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "paper",
            "--strategy",
            "buy_and_hold",
            "--symbols",
            "AAA,BBB",
            "--from",
            "2024-01-02",
            "--to",
            "2024-03-01",
            "--source",
            "synthetic",
            "--seed",
            "7",
            "--cash",
            "10000",
            "--out",
            str(out),
            *extra,
        ],
    )


class TestDivergenceIsOffByDefault:
    def test_no_divergence_artifacts_without_the_flag(self, tmp_path: Path) -> None:
        out = tmp_path / "plain"
        result = _invoke(out)
        assert result.exit_code == 0, result.output
        assert not (out / "fill_divergence.csv").exists()
        assert "Fill divergence" not in result.output

    def test_enabling_it_changes_nothing_about_the_run(self, tmp_path: Path) -> None:
        """The byte-identity guarantee: the wrapper is transparent end to end.

        ``equity_curve.csv`` and ``result.json`` are the canonical artifacts of a
        run (ADR-0023) and neither carries a wall-clock stamp, so identical bytes
        here mean the shadow changed no fill, no rejection, and no equity mark.
        """
        plain, tracked = tmp_path / "plain", tmp_path / "tracked"
        assert _invoke(plain).exit_code == 0
        assert _invoke(tracked, "--divergence").exit_code == 0

        for name in _ARTIFACTS:
            assert (tracked / name).read_bytes() == (plain / name).read_bytes(), name


class TestDivergenceOutput:
    def test_writes_the_csv_and_prints_the_block(self, tmp_path: Path) -> None:
        out = tmp_path / "tracked"
        result = _invoke(out, "--divergence")
        assert result.exit_code == 0, result.output

        csv_path = out / "fill_divergence.csv"
        assert csv_path.exists()
        lines = csv_path.read_text().splitlines()
        assert lines[0].startswith("submitted_ts,")
        assert len(lines) > 1, "the replay should have produced at least one order"

        assert "Fill divergence" in result.output
        assert "VERDICT" in result.output
        assert str(csv_path) in result.output

    def test_the_offline_replay_reports_no_divergence(self, tmp_path: Path) -> None:
        """``--broker simulated`` compared against a simulated shadow is the null test.

        A non-zero divergence here would mean the counterfactual is priced against
        the wrong bar, and every number this report prints about a real venue would
        carry the same error.
        """
        result = _invoke(tmp_path / "tracked", "--divergence")
        assert result.exit_code == 0, result.output
        assert "Outcome mismatch:  0 " in result.output
        assert "error (realized - modelled) +0.00 bps mean" in result.output

    def test_the_replay_labels_its_price_notion_as_adjusted(self, tmp_path: Path) -> None:
        """ADR-0021: the ``--once`` replay materializes the range adjusted, not raw.

        Mislabelling this is how a divergence number becomes meaningless, so the
        label follows the feed rather than the mode's name.
        """
        result = _invoke(tmp_path / "tracked", "--divergence")
        assert "Price notion:      adjusted" in result.output
