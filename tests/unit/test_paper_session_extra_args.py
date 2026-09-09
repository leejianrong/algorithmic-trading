"""``PAPER_EXTRA_ARGS`` quote-removal in ``scripts/paper_session.sh``.

``build_cmd`` splits the operator-supplied ``PAPER_EXTRA_ARGS`` string into an
array so flags like ``--max-empty-polls 24`` reach the underlying ``trading
paper`` invocation as separate argv entries. The original implementation used
``read -r -a extra <<<"$PAPER_EXTRA_ARGS"``, which splits on ``IFS`` whitespace
but performs no shell quote-removal -- a quote character embedded in the string
(exactly what ``--hypothesis "two words"`` needs, per
``docs/paper-incubation-2026-09-02.md``) is treated as an ordinary literal
character rather than as quoting. The value tears into stray array entries with
literal backslash-escaped quotes glued onto the first and last words instead of
reassembling into one token, which a Click-based CLI either rejects as unexpected
extra arguments or silently truncates.

The fix is ``eval "extra=($PAPER_EXTRA_ARGS)"``, which performs real shell
parsing (quote-removal included) so a quoted multi-word value collapses back
into one array element. ``eval`` here is safe: ``PAPER_EXTRA_ARGS`` is always
operator-supplied (command line or Makefile invocation), never
externally-controlled input -- the same trust level every other launch
parameter in this script already assumes.

These tests source the script (rather than exec it) to reach ``build_cmd``
directly, which is why ``scripts/paper_session.sh`` gained a
``sourced vs executed`` guard around its final dispatch ``case`` -- without it,
sourcing the script with no positional argument would hit
``die "usage: ... "`` and exit non-zero before ``build_cmd`` was ever defined.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "paper_session.sh"

# A sentinel the probe script prints when `extra` was never assigned (the empty
# PAPER_EXTRA_ARGS case), so "zero extra args" and "one empty-string extra arg"
# are distinguishable in the captured output.
_UNSET_SENTINEL = "__EXTRA_UNSET__"


def _extra_args(paper_extra_args: str) -> list[str]:
    """Return the ``extra`` array ``build_cmd`` produces for one env value.

    Sources the real script (so this exercises the actual fix, not a
    reimplementation of it), sets ``PAPER_EXTRA_ARGS``, calls ``build_cmd`` with
    a throwaway ``--out``, and prints the resulting ``extra`` array one element
    per line -- NUL-separated so an element containing a literal newline (none
    of these test cases do, but the probe should not lie if one did) cannot be
    confused with a line boundary.
    """
    probe = f"""
set -euo pipefail
source {_SCRIPT!s}
unset extra || true
PAPER_EXTRA_ARGS={paper_extra_args!r}
build_cmd /tmp/paper_session_extra_args_test_out
if [ -v extra ]; then
    printf '%s\\0' "${{extra[@]}}"
else
    printf '%s\\0' "{_UNSET_SENTINEL}"
fi
"""
    result = subprocess.run(
        ["bash", "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"probe script failed (rc={result.returncode}):\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    parts = result.stdout.split("\0")
    # A trailing empty string follows the final \0 terminator.
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


class TestQuotedMultiWordValue:
    """The bug case: a flag value containing spaces, wrapped in double quotes."""

    def test_hypothesis_with_embedded_spaces_is_one_token(self) -> None:
        extra = _extra_args('--bootstrap --ledger foo.jsonl --hypothesis "hello world test"')
        assert extra == ["--bootstrap", "--ledger", "foo.jsonl", "--hypothesis", "hello world test"]

    def test_hypothesis_value_is_not_torn_apart(self) -> None:
        # Regression guard for the exact symptom described in the ticket: under
        # the old `read -a` code this value split into three stray entries
        # (`"hello`, `world`, `test"`) with literal quote characters attached.
        extra = _extra_args('--hypothesis "hello world test"')
        assert extra == ["--hypothesis", "hello world test"]
        assert '"hello' not in extra
        assert 'test"' not in extra


class TestPlainUnquotedTokens:
    """The pre-existing, already-working case must keep working."""

    def test_max_empty_polls_still_splits_on_whitespace(self) -> None:
        extra = _extra_args("--max-empty-polls 24")
        assert extra == ["--max-empty-polls", "24"]

    def test_multiple_plain_flags(self) -> None:
        extra = _extra_args("--bootstrap --regimes --monte-carlo")
        assert extra == ["--bootstrap", "--regimes", "--monte-carlo"]


class TestEmptyExtraArgs:
    """An unset/empty PAPER_EXTRA_ARGS must append zero extra CLI args."""

    def test_empty_string_appends_nothing(self) -> None:
        extra = _extra_args("")
        assert extra == [_UNSET_SENTINEL]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["launch"],
        ["dryrun"],
        ["stop"],
        ["status"],
        ["bogus"],
    ],
)
def test_sourcing_the_script_never_dispatches(argv: list[str]) -> None:
    """Sourcing must not run the launch/dryrun/stop/status dispatch.

    Only ``build_cmd`` (and the other function definitions) should take effect;
    none of the real commands -- which would try to create ``results/paper/...``
    directories, launch ``uv run``, etc. -- may execute as a side effect of
    sourcing the script to test it in isolation.
    """
    args_repr = " ".join(f"{a!r}" for a in argv)
    probe = f"source {_SCRIPT!s} {args_repr}; echo SOURCED_OK"
    result = subprocess.run(
        ["bash", "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "SOURCED_OK"


def test_executed_with_no_args_prints_usage_and_exits_nonzero() -> None:
    """Executing (not sourcing) the script with no argument is unchanged."""
    result = subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "usage:" in result.stderr
