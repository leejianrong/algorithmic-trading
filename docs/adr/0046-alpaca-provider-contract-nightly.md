# ADR-0046: A nightly contract test for Alpaca, and CI's first live credentials

- Status: Accepted (needs one manual step — see **Ops**)
- Date: 2026-08-09
- Deciders: strategy developer (project owner)
- Extends (does not edit): ADR-0040 (CI network boundary), ADR-0018 (Alpaca creds)

## Context

ADR-0040 built the right mechanism and pointed it at one provider. Its own closing
line said so:

> Still open: other adapters are not covered by a contract test at all.
> `AlpacaAdapter` has creds-gated live tests (ADR-0018) that skip in CI, so nothing
> nightly notices an Alpaca response-shape change either — the same treatment would
> apply, and the `integration-network` job is now the place to put it.

ADR-0045 proved that gap is not theoretical. Alpaca stopped applying AAPL's
2020-08-31 split some time between 2026-08-04 and 2026-08-08, and **nothing in
this repo noticed**. It surfaced only because an agent happened to be executing
live Alpaca paths for an unrelated ticket. Every Alpaca test sat behind a
credentials gate CI could never satisfy, so "skipped" and "passing" were
indistinguishable from outside — exactly the failure mode ADR-0040 named ("a
skipped provider check is a green tick that means nothing"), arriving through the
one provider it did not cover.

The blocker was never the test. It was that this repo has never given CI live
keys.

## Decision

**Alpaca gets a nightly provider-contract test, and CI gets paper credentials.**

`tests/integration/test_alpaca_contract.py`, marked `integration` **and**
`network`, runs in the existing non-required `integration-network` job. It asserts
four things:

1. **Adjusted really means adjusted.** A known split (NVDA 10:1, 2024-06-10) must
   still be backed out of the adjusted series, measured by the same exact
   factor-ratio arithmetic ADR-0045 uses but restated in the test's own terms, so
   a bug in the adapter cannot make the test agree with it. Plus a direct
   assertion that the adjusted series carries no phantom cliff.
2. **The known defect, honestly recorded.** AAPL's 2020-08-31 split is a
   `@pytest.mark.xfail(strict=True)`. It xfails today. The day Alpaca fixes it,
   the test XPASSes and the nightly turns **RED** — which is the signal to delete
   the xfail and re-evaluate whether ADR-0045's guard is still needed. A
   non-strict xfail would let the good news pass unnoticed, which is the same
   silence that let the bad news through in the first place.
3. **The guard's own dependency.** ADR-0045 degrades to warn-only if the
   corporate-actions endpoint stops answering, and that degradation is invisible
   in a batch run. The nightly asserts the endpoint still reports AAPL's split
   with `ratio == 4.0`, and that a clean window comes back empty rather than
   erroring.
4. **Shape and metadata.** Daily bars parse into our `Bar` (OHLC ordering,
   tz-aware timestamps, non-zero volume — read by the ADV screen, ADR-0029);
   intraday bars are still START-stamped on the interval boundary (ADR-0022, what
   `interval_is_complete` gates on); `get_asset` still reports
   tradable/fractionable with the `AssetExchange.` prefix stripped (ADR-0028);
   an unknown ticker is still a `LookupError`.

**Credentials, and the shape of "skipped".** The job now installs the optional
SDK (`uv sync --frozen --extra alpaca` — without it every Alpaca test would skip
on SDK grounds and the keys would never be consulted) and passes
`ALPACA_API_KEY` / `ALPACA_SECRET_KEY` from repository secrets. Absent secrets
expand to `""`, which is exactly what the tests' credential gate checks, so a
fork or an unconfigured repo **skips cleanly and never fails**.

Silence is not allowed to look like success. A step reads the two secrets and
emits either a GitHub `::notice` ("the Alpaca provider-contract tests will RUN")
or a `::warning` ("...did not run. A green job here does NOT mean Alpaca's
contract holds"), and pytest runs with `-rs` so every skip is listed with its
reason in the log.

**Nothing here trades.** The whole file is a read of market data and asset
metadata: no `submit_order`, no `cancel_order`, no position or balance change.
PAPER keys only.

**`integration-network` stays non-required.** It does not run on `pull_request`,
so adding it to branch protection would deadlock every merge. This is restated in
the workflow comment beside the job, in the test module docstring, and here.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Put the Alpaca contract test in the required `integration` job | Directly violates ADR-0040. It talks to a service we do not control, so it would hand Alpaca a veto over every merge — the exact failure that ADR existed to fix, re-introduced through a different provider. |
| A separate `nightly-alpaca.yml` workflow | Duplicates setup and splits "what CI does" across files. ADR-0040 already rejected this for the same reason; one job with a job-level `if:` keeps it in one place. |
| Keep the Alpaca tests creds-gated and rely on an operator running them | That is the status quo, and it is precisely what failed: the regression sat unnoticed for four days and was found by accident. |
| Commit a fixture of the 2020 bars, as ADR-0040 did for yfinance | A fixture proves our *adapter* handles adjusted prices; it cannot notice a provider regression, which is the entire point here. ADR-0040 already drew this line — the fixture and the contract test are the two halves, and this is the contract half. |
| Non-strict `xfail` for the AAPL defect | An XPASS would be a quiet dot. The provider getting fixed is a state change we must act on (delete the guard's justification), so it must be loud. |
| Assert the AAPL defect *positively* ("still broken") so the nightly stays green | Reads as endorsing the broken state, and would go red for the right reason but with a message saying the opposite. The strict xfail carries the same information with honest polarity. |
| Skip silently when secrets are missing | A green job that ran nothing is the failure this ADR exists to prevent. Hence the explicit annotation step. |
| Give CI a *live-funded* key, or a trading-scoped key | Never. Paper only, and the tests are read-only regardless. |

## Ops — the manual step this ADR cannot perform

The repository secrets must be added by hand (a slice cannot create them):

| Secret name | Value |
|---|---|
| `ALPACA_API_KEY` | Paper API key ID |
| `ALPACA_SECRET_KEY` | Paper API secret key |

Steps:

1. Generate **paper** keys at
   <https://app.alpaca.markets/paper/dashboard/overview> -> "API Keys". These must
   be paper keys; `RealAlpacaClient` defaults to `paper=True` and nothing in the
   `network` layer places an order, but a live-funded key does not belong in this
   bench (ADR-0018).
2. Repository -> Settings -> Secrets and variables -> Actions -> *New repository
   secret*, twice, with the names above spelled exactly.
3. Trigger the job once by hand to confirm: Actions -> CI -> *Run workflow*
   (`workflow_dispatch`), then check the `integration-network` job for the
   `::notice` saying credentials are present. Until the secrets exist the job
   emits the `::warning` and the Alpaca tests skip — the yfinance contract test
   still runs, so the job is not empty.
4. **Do not** add `integration-network` to branch protection.

Scoping the keys to data-only access was investigated: Alpaca's paper key model
issues one key pair per paper account with no per-scope restriction, so
"data-only" is not available. The mitigation is that the `network` layer performs
no write operation at all, which is asserted by inspection of the module (no
`submit_order` / `cancel_order` import) rather than merely intended.

## Consequences

- **An Alpaca provider regression now has one night's latency instead of
  indefinite.** The KAN-694 defect would have been caught the night of
  2026-08-05 rather than by accident on 2026-08-08.
- **The nightly is currently expected to show one xfail**, and that is the
  designed steady state until Alpaca fixes AAPL. A reader who sees only "all
  green" should check the xfail count.
- **A red nightly still needs a human**, as ADR-0040 established — it gates
  nothing. The failure messages name what broke and which ADR it violates.
- **Request budget**: roughly a dozen Alpaca requests per nightly run. Negligible,
  and it is on a schedule rather than per-PR, which is the ADR-0040 principle.
- **CI now holds a credential.** The blast radius is a paper account with fake
  money; the `security` job's gitleaks scan is unchanged and the keys are never
  echoed (the annotation step tests emptiness, never prints a value). A leaked
  paper key can move paper positions, which is real but bounded.
- **`make test-network` now runs both providers'** contract tests locally; the
  Alpaca half needs `uv sync --extra alpaca` and creds in the environment (via
  `--env-file .env`), and skips otherwise.
- **Trap worth recording (KAN-696):** `pytest tests/integration/<file>` silently
  deselects everything, because `addopts` carries
  `-m 'not integration and not e2e and not network'`. Running these by hand needs
  an explicit `-m`, e.g.
  `uv run --env-file .env pytest tests/integration/test_alpaca_contract.py -m "integration and network"`.
  Confirmed still true on this branch.
- Still open: `test_alpaca_live.py` and `test_fill_divergence_live.py` remain
  `integration`-only and creds-gated, so they still never run in CI. Promoting
  them to `network` would put order submission on a schedule, which is a bigger
  decision than this one; only the read-only contract surface is promoted here.
