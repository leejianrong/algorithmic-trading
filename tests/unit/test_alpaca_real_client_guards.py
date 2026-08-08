"""Fast, offline guards on :class:`RealAlpacaClient` -- no SDK, no key, no network.

Everything the live wrapper does over the wire belongs in the integration layer
(``tests/integration/test_alpaca_live.py``, credential-gated). What lives here is
the part that can be checked *without* the SDK: the pure helper functions, and the
safety properties that must hold whether or not anyone ever runs a live session.

The headline one is the **paper endpoint default** (ADR-0018): nothing in this
bench may be able to place a real-money order. That rested entirely on an
unasserted default argument until this module, so it is asserted here two ways --
the default itself, and a scan proving no caller overrides it.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from trading.data.alpaca_client import (
    STATUS_CANCELED,
    STATUS_EXPIRED,
    STATUS_FILLED,
    STATUS_NEW,
    STATUS_REJECTED,
    STATUS_REPLACED,
    TERMINAL_STATUSES,
    TERMINAL_UNFILLED_STATUSES,
    DataSubscriptionError,
    RealAlpacaClient,
    _classify_data_error,
    _require_float,
    _require_model,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "trading"


class TestPaperEndpointDefault:
    """The bench must not be able to reach a live-funded account (ADR-0018)."""

    def test_paper_defaults_to_true(self) -> None:
        # Checked by signature so the assertion needs neither the SDK nor a key:
        # constructing the client would require both.
        param = inspect.signature(RealAlpacaClient.__init__).parameters["paper"]
        assert param.default is True

    def test_paper_is_keyword_only(self) -> None:
        # Keyword-only means no caller can flip it by positional accident.
        param = inspect.signature(RealAlpacaClient.__init__).parameters["paper"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY

    def test_no_module_in_src_requests_the_live_endpoint(self) -> None:
        # A crude but load-bearing guard: the paper default only protects us while
        # nothing overrides it. If a future change adds `paper=False` anywhere in
        # the package -- including a CLI flag that forwards it -- this fails.
        offenders = [
            path.relative_to(_SRC).as_posix()
            for path in _SRC.rglob("*.py")
            if "paper=False" in path.read_text().replace(" ", "")
        ]
        assert offenders == []


class TestStatusConstants:
    """The status literals are alpaca-py ``OrderStatus`` *values* (ADR-0033).

    That they match the real SDK is asserted in the integration layer, which can
    import the SDK; here we only pin the classification the broker relies on.
    """

    def test_terminal_set_is_exactly_the_five_end_states(self) -> None:
        assert {
            STATUS_FILLED,
            STATUS_REJECTED,
            STATUS_CANCELED,
            STATUS_EXPIRED,
            STATUS_REPLACED,
        } == TERMINAL_STATUSES

    def test_unfilled_terminal_set_excludes_filled_and_rejected(self) -> None:
        # filled/rejected are terminal but handled specially by the broker.
        assert {STATUS_CANCELED, STATUS_EXPIRED, STATUS_REPLACED} == TERMINAL_UNFILLED_STATUSES

    @pytest.mark.parametrize(
        "working",
        ["new", "accepted", "pending_new", "partially_filled", "done_for_day", "held"],
    )
    def test_working_statuses_are_not_terminal(self, working: str) -> None:
        # A working order may still fill; treating one as terminal would drop a
        # live order on the floor.
        assert working not in TERMINAL_STATUSES

    def test_new_is_a_working_status(self) -> None:
        assert STATUS_NEW not in TERMINAL_STATUSES


class TestRequireModel:
    """The SDK's ``Model | dict`` return arms: the dict one must fail loudly."""

    def test_passes_a_model_through(self) -> None:
        sentinel = object()
        assert _require_model(sentinel, "thing") is sentinel

    def test_passes_a_list_through(self) -> None:
        rows = [1, 2, 3]
        assert _require_model(rows, "positions") == [1, 2, 3]

    def test_raises_on_the_raw_dict_arm(self) -> None:
        # Reading fields off a dict with getattr would silently yield False/empty --
        # which for ADR-0028 would mislabel every asset as untradable.
        with pytest.raises(TypeError, match="raw dict data for get_account"):
            _require_model({"cash": "1.0"}, "get_account")


class TestRequireFloat:
    """Alpaca sends numbers as strings and types many of them Optional."""

    def test_parses_a_string(self) -> None:
        assert _require_float("310.166", "x") == pytest.approx(310.166)

    def test_passes_a_float(self) -> None:
        assert _require_float(2.5, "x") == pytest.approx(2.5)

    def test_raises_naming_the_field_when_omitted(self) -> None:
        with pytest.raises(ValueError, match=r"TradeAccount\.cash"):
            _require_float(None, "TradeAccount.cash")


class TestClassifyDataError:
    """A data-plan refusal is its own condition, not a transport failure (ADR-0034)."""

    def test_subscription_refusal_becomes_a_data_subscription_error(self) -> None:
        original = RuntimeError(
            '{"message":"subscription does not permit querying recent SIP data"}'
        )
        mapped = _classify_data_error(original, "AAPL", None)
        assert isinstance(mapped, DataSubscriptionError)

    def test_message_names_the_actionable_flag_and_the_feed_used(self) -> None:
        original = RuntimeError(
            '{"message":"subscription does not permit querying recent SIP data"}'
        )
        mapped = _classify_data_error(original, "AAPL", None)
        assert "--data-feed iex" in str(mapped)
        assert "'AAPL'" in str(mapped)
        assert "sip (SDK default)" in str(mapped)

    def test_reports_an_explicit_feed_when_one_was_set(self) -> None:
        original = RuntimeError("subscription does not permit querying OTC data")
        mapped = _classify_data_error(original, "XYZ", "otc")
        assert "feed=otc" in str(mapped)

    def test_other_failures_pass_through_unchanged(self) -> None:
        # Auth, rate limit, transport: not our business to reinterpret, exactly as
        # get_asset keeps "the broker said no" apart from "we could not ask".
        original = RuntimeError("403 forbidden: key revoked")
        assert _classify_data_error(original, "AAPL", "iex") is original
