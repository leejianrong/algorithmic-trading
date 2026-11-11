"""Post-IPO fade comp table (ADR-0077). Network: pulls daily bars from yfinance.

Measures returns from the DAY-2 CLOSE, the first point options can trade on a large
IPO (roughly the second business day after listing). Offer prices are from memory or
press and are UNVERIFIED against prospectuses. The set is every name the author could
verify, not a complete $1B+ list.

Run: uv run python research/ipo-fade/ipo_comps.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

# (ticker, offer price, first trade date)
COMPS = [
    ("ARM", 51, "2023-09-14"),
    ("CART", 30, "2023-09-19"),
    ("KVYO", 30, "2023-09-20"),
    ("ALAB", 36, "2024-03-20"),
    ("RDDT", 34, "2024-03-21"),
    ("RBRK", 32, "2024-04-25"),
    ("VIK", 24, "2024-05-01"),
    ("LINE", 78, "2024-07-25"),
    ("SARO", 24, "2024-10-02"),
    ("VG", 25, "2025-01-24"),
    ("CRWV", 40, "2025-03-28"),
    ("CRCL", 31, "2025-06-05"),
    ("FIG", 33, "2025-07-31"),
    ("FLY", 45, "2025-08-07"),
    ("BLSH", 37, "2025-08-13"),
    ("KLAR", 40, "2025-09-10"),
    ("FRMI", 21, "2025-10-01"),
    ("NAVN", 25, "2025-10-30"),
    ("CBRS", 185, "2026-05-14"),
    ("SPCX", 135, "2026-06-12"),
    ("SKHY", 149, "2026-07-10"),
]
# Illustrative only: a 20-day ATM put at an ASSUMED 100% IV costs ~11% of spot.
ASSUMED_PUT_COST = 0.11


def forward_return(closes: pd.Series, k: int) -> float:
    """Return from the day-2 close (index 1) to k trading days later."""
    return closes.iloc[1 + k] / closes.iloc[1] - 1 if 1 + k < len(closes) else float("nan")


def load(ticker: str, offer: float, start: str) -> dict[str, float | str] | None:
    try:
        bars = yf.download(ticker, start=start, progress=False, auto_adjust=False)
    except Exception as exc:  # network/provider failure: skip, never crash the table
        print(ticker, "ERR", exc)
        return None
    if bars is None or len(bars) < 5:
        print(ticker, "no/short data")
        return None
    bars.columns = [c[0] if isinstance(c, tuple) else c for c in bars.columns]
    closes = bars["Close"].reset_index(drop=True)
    return {
        "t": ticker,
        "n": len(closes),
        "pop": closes[0] / offer - 1,
        "pop2": closes[1] / offer - 1,
        "r20": forward_return(closes, 20),
        "r40": forward_return(closes, 40),
        "r60": forward_return(closes, 60),
    }


def summarize(name: str, frame: pd.DataFrame) -> None:
    for col in ("r20", "r40", "r60"):
        v = frame[col].dropna()
        if len(v):
            print(
                f"{name:12s} {col}: n={len(v):2d} down={int((v < 0).sum())}/{len(v)} "
                f"median={v.median():+.1%} mean={v.mean():+.1%} worst(up)={v.max():+.0%}"
            )


def main() -> None:
    rows = [r for r in (load(t, o, d) for t, o, d in COMPS) if r]
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.round(3).to_string(index=False))
    summarize("ALL", df)
    summarize("pop2>=50%", df[df.pop2 >= 0.5])
    summarize("pop2 25-50%", df[(df.pop2 >= 0.25) & (df.pop2 < 0.5)])
    summarize("pop2<25%", df[df.pop2 < 0.25])
    net = np.maximum(-df.r20.dropna(), 0) - ASSUMED_PUT_COST
    print(
        f"ATM put 20d @assumed IV100: mean net/spot {net.mean():+.3f}, "
        f"win {int((net > 0).sum())}/{len(net)} (illustrative)"
    )


if __name__ == "__main__":
    main()
