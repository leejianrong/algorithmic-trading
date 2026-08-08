# ADR-0040: A required check may not depend on a third-party service

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)

## Context

On 2026-08-08, landing PR #40, CI's `integration` job failed with
`YFRateLimitError('Too Many Requests. Rate limited. Try after a while.')`. A bare
re-run cleared it. No code was involved.

`integration` is one of six **required** status checks (ADR-0010, branch protection
since 2026-08-04), and it made live yfinance calls. So an upstream rate limit could
block **every merge in the repo** — and it is most likely to fire exactly when the
repo is busiest, because every PR run spends more of the provider's request budget.
That is a merge queue whose availability is owned by a free third-party API.

There were exactly two yfinance-touching tests, and their requirements are opposite:

| Test | What it proves | Needs the network? |
|---|---|---|
| `test_backtest_real_data.py` — the ADR-0008 phantom-split guard | buy-and-hold across AAPL's 4-for-1 split shows no ~75% one-day crash | **No.** It reads 2020-06-01..2020-12-01, five years past and immutable. |
| `test_yfinance_reachable.py` — the provider contract | yfinance still returns the OHLCV columns `YFinanceAdapter` parses | **Yes, by definition.** Its `columns.get_level_values(0)` is a MultiIndex accommodation, i.e. evidence this shape has already changed once. |

Fetching immutable 2020 bars on every CI run buys nothing and costs merge
availability. Faking the contract test would delete the only thing it checks.

Two things surfaced while implementing this, both worse than the rate limit itself.

**A refusal was indistinguishable from missing history.** ADR-0032 split absence
(`REASON_NO_BARS`) from failure (`REASON_FETCH_FAILED`) and justified the split with
"`yf.download` signals genuine failure *by raising*". It does not. `multi.py`'s
`_download_one` catches **every** per-ticker exception and substitutes an empty
frame. So a 429 arrived at `_default_fetch` looking exactly like a delisting and left
the engine as `EmptyUniverseError: no bars for AAPL … not listed in this window, or
the source has no history`. Both readings of that sentence are wrong, and the
dangerous direction is the second: a genuine provider break reads as a flake and gets
re-run away.

**The split guard did not discriminate.** It asserted no daily return below −35%,
commenting that a raw series "would show ~ −0.75 on the split day". Measured on the
committed fixture, with the default guardrails (`max_position_pct = 0.25`) an
*unadjusted* series bottoms out at **−25.3%** — comfortably inside the −35% floor.
The risk caps diluted the artifact under test, so the test passed on raw prices and
had never been a guard at all. Fully invested it is stark: **−8.0% adjusted vs
−73.9% unadjusted.**

## Decision

**The merge path does not leave the machine.** `integration` stays a required check
and is now entirely offline; a second job, `integration-network`, holds everything
that talks to a service we do not control, and is deliberately **not** required.

- **`integration`** — `pytest -m "integration and not network"`. Runs on PR + push,
  as before. Required.
- **`integration-network`** — `pytest -m network`. Nightly (`schedule`) plus
  `workflow_dispatch`, and **not on `pull_request`**: a PR trigger would keep
  spending the request budget that causes the problem. It must never be added to
  branch protection — it does not run on PRs, so requiring it would deadlock merging.

**The boundary is a marker, not a path.** A second pytest marker, `network`, layers
the tests by *what they can block* rather than only by cost: `integration` means
"needs infra we own or can skip past (broker creds, optional extras)", `network`
means "needs a third party". Paths would have re-broken the moment an offline test
landed in `tests/integration/`, which is exactly what happened here. The fast gate
deselects both (`-m 'not integration and not e2e and not network'`), so `make check`
is untouched.

**Immutable historical data is a fixture.** The split guard reads a committed
`YFinanceAdapter` cache CSV, `tests/fixtures/yfinance_cache/AAPL_20200601_20201201_adj.csv`
— 128 daily adjusted bars, 12 KB, price data only. The adapter's read-through cache
already made this a one-line change: the file exists, so no fetch happens. The
injected fetcher is a stub that **raises if called**, so a missing or misnamed
fixture fails loudly instead of quietly reaching for the network — and cannot write
into the fixture directory either, since the write follows the fetch.

**A refusal is now classified as a failure.** `_default_fetch` probes an empty
response through `Ticker.history`, which (unlike `download`) re-raises
`YFRateLimitError` unconditionally while a delisted or not-yet-listed symbol still
comes back empty. A refusal becomes `ProviderRefusedError` → `REASON_FETCH_FAILED`;
everything else keeps the empty-means-absent reading, so ADR-0032's multi-decade
walk-forward tolerance is unchanged. Classification is by **exception type**, not by
matching log text, so a provider rewording does not silently flip it. Cost: one extra
request, only on an already-empty response, and only on a cache miss.

**The guard is now watched failing, in both directions.** The split test runs fully
invested (`RiskConfig.unlimited()`) so the phantom crash is undiluted, and a sibling
test de-adjusts the same fixture by the exact known 4-for-1 ratio and asserts the
−35% floor **does** trip (−73.9%). A third test asserts the fixture still spans
2020-08-31, so a trimmed fixture cannot make the guard vacuous. A fourth asserts that
a *missing* fixture reports `REASON_FETCH_FAILED` and never the words "not listed in
this window" — the reason-code discipline applied to this test's own failure mode.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Retry/backoff around the yfinance call in CI | Buys availability with wall-clock and hides the signal. A rate limit is a *fact about the provider*; retrying until it passes means a real outage also eventually "passes" or times out with the same red X. |
| Drop `integration` from the required checks | Throws away the guard to fix the plumbing. The offline half of that job is fast, deterministic, and exactly what should gate a merge. |
| Mark the network test `xfail`/skip on rate limit | Makes the contract test unable to report the thing it exists to report. A skipped provider check is a green tick that means nothing. |
| Separate `nightly.yml` workflow | Cleaner triggers, but duplicates the setup steps and splits "what CI does" across two files. One workflow with a job-level `if:` keeps it in one place; the added `schedule` also gives `security`/`pip-audit` a nightly run against newly published advisories, which is a bonus, not a cost. |
| File-path selection (`pytest tests/integration/offline/`) | Directory layout is not the property we care about; a marker states the constraint on the test itself and `--strict-markers` enforces the spelling. |
| Keep fetching live and accept occasional blocked merges | It is a solo repo with `enforce_admins: true`. "Occasionally nothing can merge, for reasons entirely outside the repo" is not an acceptable steady state. |
| Fix the 429 misclassification by scraping yfinance's log output | Works today (`download` logs `repr(exc)`), breaks on any rewording, and an absent symbol logs errors too — so it needs message matching to tell them apart. Exception types are the stable contract. |
| Switch `_default_fetch` to `Ticker.history` outright | Would classify everything correctly in one request, but changes the primary fetch path: `history` returns exchange-tz-aware timestamps, which would shift every cached bar off midnight UTC and desynchronise it from every other adapter. Not worth it for the empty-response case. |

## Consequences

- **A third-party rate limit can no longer block a merge.** The required
  `integration` job passes with outbound connectivity removed entirely (verified in a
  network namespace: 4 passed; the `network` layer fails there, as it should).
- The required job also got *faster* — the split guard went from a live multi-request
  download to ~0.1 s off a 12 KB CSV.
- **A committed fixture stops testing that *yfinance today* returns adjusted prices.**
  This is the honest cost. The offline test now proves that **the adapter** handles
  adjusted prices correctly — the engine, the broker, the equity curve, the split
  arithmetic — and nothing about the provider's current behaviour. The other half
  moved to the nightly job, which now also asserts that a live fetch across AAPL's
  split still comes back adjusted (~$121 pre-split, not ~$484). Both halves exist;
  only one of them can block a merge, and it is the one we control.
- **The fixture can go stale.** If yfinance revises those bars (a late adjustment, a
  data-quality fix), the fixture keeps testing the old numbers and nothing notices.
  Judged acceptable: the window is five years old and its corporate action is
  historical fact. To refresh, delete the file and run the fetch once:
  ```bash
  uv run python -c "
  from datetime import UTC, datetime
  from pathlib import Path
  from trading.data.yfinance_adapter import YFinanceAdapter
  YFinanceAdapter(Path('tests/fixtures/yfinance_cache')).get_bars(
      'AAPL', datetime(2020, 6, 1, tzinfo=UTC), datetime(2020, 12, 1, tzinfo=UTC))
  "
  ```
  The cache filename is `cache_filename()`'s output, so the name must not be typed by
  hand. Then re-run `make test-integration` — the tests pin the row count, the split
  date's presence, and the pre-split price band, so a materially different refetch
  fails loudly rather than sliding in.
- A red nightly `integration-network` needs a human to read the message, because it
  gates nothing. The failure text now distinguishes the two cases in words: a refusal
  raises `ProviderRefusedError` naming the rate limit, and an unexplained empty
  response fails with "investigate the provider, do not just re-run".
- `make test-integration` is now the offline layer and `make test-network` the live
  one; `make ci-local` runs the merge path only and prints a pointer to the nightly.
  `make test-all` still runs everything.
- `ProviderRefusedError` is new public surface on `trading.data.yfinance_adapter`.
  Anything catching broad `Exception` around a fetch is unaffected; the engine's
  per-symbol guard (ADR-0032) already routes it to `REASON_FETCH_FAILED`.
- The `concurrency` group is keyed on `github.event_name` now, so the nightly cron on
  `main` cannot cancel an in-flight push run on `main`.
- Still open: other adapters are not covered by a contract test at all. `AlpacaAdapter`
  has creds-gated live tests (ADR-0018) that skip in CI, so nothing nightly notices an
  Alpaca response-shape change either — the same treatment would apply, and the
  `integration-network` job is now the place to put it.
