# ADR-0080: Share-class tickers are translated at the yfinance boundary; a plain 404 stays "absent"

- **Status:** accepted
- **Date:** 2026-11-12
- **Context ADRs:** 0032, 0040, 0064, 0072

## Context

Two free-data gaps recorded after ADR-0072.

1. The point-in-time S&P 500 fixture (ADR-0064) spells share classes with a dot
   (`BRK.B`, `BF.B`); yfinance wants `BRK-B`, `BF-B` and 404s on the dot form. An
   empty response then reads as historical absence (ADR-0032).
2. A plain 404 for a still-listed name (e.g. `BK`) is also read as absence.

## Decision

**(1) Translate, narrowly, in `YFinanceAdapter.get_bars`**, via `to_yahoo_symbol`: only
`^[A-Z]{1,5}\.[ABC]$` becomes a dash. Exchange suffixes (`VOD.L`, `0700.HK`), crypto
pairs, lowercase and already-dashed names pass through. Translation happens only on
the call to the fetcher, so `Bar.symbol`, absence reports, the cache file name
(`BRK.B_..._adj.csv`) and `result.json` keep the name the user typed; existing cache
files and fixtures are untouched. The committed fixture is not edited. Every dotted
name in the fixture is a single-letter A/B class, so the rule covers it exactly.
Residual risk: a non-share-class `XXX.A` ticker would be rewritten; this bench is
US-equity and has none.

**(2) Not changed.** `probe_refusal` names only `YFRateLimitError`; everything else is
"absent" on purpose (ADR-0040: misclassifying late listings as failures re-breaks the
multi-decade walk-forward). yfinance surfaces a 404 for a live ticker and a genuinely
delisted or not-yet-listed one the same way (empty frame / `YFPricesMissingError`), so
no exception-type rule can separate them offline, and guessing would trade a known
false "absent" for false failures on every late listing. Separating them needs live
calls (an independent listing lookup) and belongs in the nightly `network` layer.
A test pins the current reading so any change is deliberate.
