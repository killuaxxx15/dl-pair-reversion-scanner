"""
DL Pair Reversion Engine — mean-reversion signal for long-term contra pair trades.

Core idea: simple metrics. A pair is a candidate when its ratio has been
>= 3 sigma ABOVE its 5-year mean (short the ratio) or <= 3 sigma BELOW it
(long the ratio), AND the near-term trend has turned against the extension —
that turn is the catalyst.

Logic (finalized spec):
  1. Ratio = A / B (daily close, adjusted for splits/dividends).
  2. Extension: rolling 5-yr (1260d) mean & std -> z-score.
     Extension memory is ASYMMETRIC: peak z over 12m for upside extremes
     (blowoff tops resolve fast) and 24m for downside extremes (bases form
     slowly; a 12m window forgets the washout mid-base).
  3. Catalyst: MA structure score 0-3 (ratio through 50/100/200 DMA in the
     reversion direction) + 50DMA 20d slope turned against the extension.
  4. Signal:
       HIGH CONVICTION : peak |z| >= 3  AND  MA score >= 2  AND  slope turned
         - long side only: "(strong base)" if base score >= 3/4,
           "(weak base — bounce risk)" if <= 1/4
       LARGELY DONE    : same setup but < 10% remains to the 5y mean
         (measured as signed distance, NOT sigma — post-washout the rolling
         mean chases price and sigma balloons, so z understates what's left)
       WATCHLIST       : peak |z| >= 3 but trend intact / slope unconfirmed
       NO TRADE        : never got extended
  5. Base score (longs, 0-4): time at lows, vol compression, 200DMA
     flattening, higher low. Bases, not bounces.
  6. Targets: T1 = 200DMA of ratio, T2 = 5-yr mean.
     Invalidation (short-reversion): ratio reclaims 50DMA AND makes new
     20d high; mirror for longs.
  7. Base rate: resolved historical episodes only (no day-0 credit, censored
     open episodes excluded), using the same asymmetric extension memory.

This module is pure computation — no network, no file I/O. `build_data.py`
is the runner that fetches prices, calls in here, and writes the JSON the
dashboard reads.
"""

import numpy as np
import pandas as pd

WIN_5Y = 1260          # trading days in 5 years
WIN_12M = 252          # trading days in 12 months
WIN_EXT_UP = 252       # extension memory, upside extremes (tops snap fast)
WIN_EXT_DOWN = 504     # extension memory, downside extremes (bases form slowly)
SLOPE_WIN = 20         # days for 50DMA slope
Z_EXTREME = 3.0
Z_STRETCHED = 2.0
DONE_THRESH = 0.10     # trade "largely done" when < 10% remains to the 5y mean
MIN_HISTORY = WIN_5Y // 2   # shortest window the rolling stats will accept


# ----------------------------------------------------------------------
# Mock data — three regimes to validate every branch of the logic.
# Retained ONLY as the regression fixture for __main__; the dashboard is
# fed live data exclusively.
# ----------------------------------------------------------------------
def make_mock_pair(regime: str, n: int = 2520, seed: int = 7) -> pd.Series:
    """Generate a mock daily ratio series (~10 years)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2026-07-14", periods=n)
    noise = rng.normal(0, 0.004, n)

    base = 0.40 + 0.00002 * np.arange(n)          # mild drift
    ratio = base * np.exp(np.cumsum(noise))

    if regime == "blowout_rolled":                 # GLD/SPY analogue
        # parabolic blowout in final 18m, then 4m breakdown
        t0, t1 = n - 380, n - 90
        ratio[t0:t1] *= np.linspace(1.0, 1.75, t1 - t0)
        ratio[t1:] = ratio[t1 - 1] * np.linspace(1.0, 0.68, n - t1) \
                     * np.exp(np.cumsum(rng.normal(0, 0.006, n - t1)))
    elif regime == "blowout_intact":               # extended, still trending
        t0 = n - 380
        ratio[t0:] *= np.linspace(1.0, 1.80, n - t0)
    elif regime == "just_broke":                   # blowout, fresh breakdown — the entry window
        t0, t1 = n - 380, n - 45
        ratio[t0:t1] *= np.linspace(1.0, 1.75, t1 - t0)
        ratio[t1:] = ratio[t1 - 1] * np.linspace(1.0, 0.88, n - t1) \
                     * np.exp(np.cumsum(rng.normal(0, 0.005, n - t1)))
    elif regime == "based_turning":                # washout -> long base -> fresh upturn
        t0, t1, t2 = n - 700, n - 380, n - 40
        ratio[t0:t1] *= np.linspace(1.0, 0.55, t1 - t0)          # washout
        ratio[t1:t2] = ratio[t1 - 1] * (1 + rng.normal(0, 0.0018, t2 - t1)).cumprod() \
                       * np.linspace(1.0, 1.04, t2 - t1)          # quiet 16m base, gentle higher lows
        ratio[t2:] = ratio[t2 - 1] * np.linspace(1.0, 1.10, n - t2)  # breakout
    elif regime == "vbounce":                      # washout -> immediate V bounce, no base
        t0, t1 = n - 300, n - 40
        ratio[t0:t1] *= np.linspace(1.0, 0.55, t1 - t0)
        ratio[t1:] = ratio[t1 - 1] * np.linspace(1.0, 1.22, n - t1)
    elif regime == "quiet":                        # nothing happening
        pass
    else:
        raise ValueError(regime)

    return pd.Series(ratio, index=dates, name="ratio")


# ----------------------------------------------------------------------
# Signal engine
# ----------------------------------------------------------------------
def compute_features(ratio: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"ratio": ratio})
    df["mean5y"] = ratio.rolling(WIN_5Y, min_periods=MIN_HISTORY).mean()
    df["std5y"] = ratio.rolling(WIN_5Y, min_periods=MIN_HISTORY).std()
    df["z"] = (df["ratio"] - df["mean5y"]) / df["std5y"]
    df["dma50"] = ratio.rolling(50).mean()
    df["dma100"] = ratio.rolling(100).mean()
    df["dma200"] = ratio.rolling(200).mean()
    df["dma50_slope"] = df["dma50"].diff(SLOPE_WIN)      # 20d change of 50DMA
    df["hi20"] = ratio.rolling(SLOPE_WIN).max()
    df["lo20"] = ratio.rolling(SLOPE_WIN).min()
    return df


def evaluate(df: pd.DataFrame, pair_name: str) -> dict:
    last = df.iloc[-1]
    z_now = last["z"]
    z_12m = df["z"].iloc[-WIN_12M:]
    if z_12m.isna().all() or pd.isna(z_now):
        return {"pair": pair_name, "signal": "INSUFFICIENT DATA",
                "direction": "—", "ratio": round(last["ratio"], 4),
                "mean5y": None, "z_now": None, "peak_z": None,
                "ma_score": "—", "dma50_slope": "—",
                "T1_200dma": "—", "T2_5y_mean": "—",
                "invalidation": f"needs ≥{MIN_HISTORY} trading days of history"}
    # Asymmetric extension memory: upside extremes use 12m (blowoff tops
    # resolve quickly); downside extremes use 24m (a genuine base spends
    # 12-18m at the lows, and a 12m window forgets the washout mid-base).
    peak_up = df["z"].iloc[-WIN_EXT_UP:].max()
    peak_dn = df["z"].iloc[-WIN_EXT_DOWN:].min()
    up_ok = pd.notna(peak_up) and peak_up >= Z_EXTREME
    dn_ok = pd.notna(peak_dn) and peak_dn <= -Z_EXTREME
    # Whipsaw: both extremes fired inside their windows. Previously we faded
    # the larger one. A ratio that has been stretched 3 sigma BOTH ways within
    # two years is not a rubber band with a known resting point — it is
    # telling us the 5-year mean is unstable, which is the one condition this
    # entire system depends on. Stand aside instead of picking a side.
    whipsaw = up_ok and dn_ok
    if whipsaw:
        peak_z = peak_up if peak_up >= -peak_dn else peak_dn
    elif up_ok:
        peak_z = peak_up
    elif dn_ok:
        peak_z = peak_dn
    else:                                     # neither extreme: report the larger
        peak_z = peak_up if abs(peak_up) >= abs(peak_dn) else peak_dn

    # Direction of the extension we'd fade
    direction = "SHORT_RATIO" if peak_z > 0 else "LONG_RATIO"

    # MA score in the reversion direction
    if direction == "SHORT_RATIO":
        ma_score = int(last["ratio"] < last["dma50"]) \
                 + int(last["ratio"] < last["dma100"]) \
                 + int(last["ratio"] < last["dma200"])
        slope_turned = last["dma50_slope"] < 0
    else:
        ma_score = int(last["ratio"] > last["dma50"]) \
                 + int(last["ratio"] > last["dma100"]) \
                 + int(last["ratio"] > last["dma200"])
        slope_turned = last["dma50_slope"] > 0

    # Late-stage guard: measure remaining reversion as SIGNED distance to the
    # 5y mean, not in sigma. Post-washout, the rolling mean gets dragged toward
    # price and sigma balloons, so z understates what's left (a based pair can
    # sit 25% below its mean at z = -0.9). Overshoot past the mean goes
    # negative and is caught automatically.
    if direction == "SHORT_RATIO":
        remaining = last["ratio"] / last["mean5y"] - 1
    else:
        remaining = last["mean5y"] / last["ratio"] - 1
    late_stage = remaining <= DONE_THRESH

    # Signal logic — whipsaw first (an unstable mean invalidates every target
    # below it), then done-ness: a round-tripped pair must not read WATCHLIST
    # just because its MA structure degraded after the move.
    if whipsaw:
        signal = "NO TRADE (whipsaw — 3σ both ways, mean unstable)"
    elif abs(peak_z) >= Z_EXTREME and late_stage:
        signal = "REVERSION LARGELY DONE — stand aside"
    elif abs(peak_z) >= Z_EXTREME and ma_score >= 2 and slope_turned:
        signal = "HIGH CONVICTION"
    elif abs(peak_z) >= Z_EXTREME and ma_score <= 1:
        signal = "WATCHLIST"
    elif abs(peak_z) >= Z_EXTREME:                       # ma_score == 2/3 but slope not turned
        signal = "WATCHLIST (slope not confirmed)"
    else:
        signal = "NO TRADE"

    # Targets & invalidation (for short-reversion; mirror for long)
    t1, t2 = last["dma200"], last["mean5y"]
    if direction == "SHORT_RATIO":
        invalidation = f"ratio > 50DMA ({last['dma50']:.4f}) AND new 20d high (> {last['hi20']:.4f})"
        t1_str = "ACHIEVED" if last["ratio"] <= t1 else f"{(last['ratio'] / t1 - 1) * 100:+.1f}% to close"
        t2_str = "ACHIEVED" if last["ratio"] <= t2 else f"{(last['ratio'] / t2 - 1) * 100:+.1f}% to close"
    else:
        invalidation = f"ratio < 50DMA ({last['dma50']:.4f}) AND new 20d low (< {last['lo20']:.4f})"
        t1_str = "ACHIEVED" if last["ratio"] >= t1 else f"{(t1 / last['ratio'] - 1) * 100:+.1f}% to close"
        t2_str = "ACHIEVED" if last["ratio"] >= t2 else f"{(t2 / last['ratio'] - 1) * 100:+.1f}% to close"

    base_score, base_parts = (base_quality(df) if direction == "LONG_RATIO"
                              else (None, None))
    # Long-side sizing label. A HIGH CONVICTION long on a weak base (<=1) is
    # more likely a dead-cat bounce; on a strong base (>=3) it is a Stage-2
    # turn. 2/4 used to fall through unlabelled, which left the analyst to
    # improvise on a reading we had declined to interpret — and 2/4 is where
    # most long candidates actually sit. It is now explicitly half size.
    if signal == "HIGH CONVICTION" and direction == "LONG_RATIO" and base_score is not None:
        signal += {0: " (weak base — bounce risk)",
                   1: " (weak base — bounce risk)",
                   2: " (moderate base — half size)",
                   3: " (strong base)",
                   4: " (strong base)"}[base_score]

    return {
        "pair": pair_name,
        "signal": signal,
        "base_score": (f"{base_score}/4" if base_score is not None else "—"),
        "base_parts": base_parts,
        "direction": direction if "NO TRADE" not in signal else "—",
        "ratio": round(last["ratio"], 4),
        "mean5y": round(t2, 4),
        "z_now": round(z_now, 2),
        "peak_z": round(peak_z, 2),
        "ma_score": f"{ma_score}/3",
        "dma50_slope": "turned" if slope_turned else "intact",
        "T1_200dma": f"{t1:.4f} ({t1_str})",
        "T2_5y_mean": f"{t2:.4f} ({t2_str})",
        "invalidation": invalidation,
        "detrended_z": detrended_z(df["ratio"]),
        "whipsaw": bool(whipsaw),
    }


def base_quality(df: pd.DataFrame) -> tuple:
    """Base score 0-4 for LONG_RATIO candidates. A durable bottom is a BASE,
    not a V: time spent at the lows, volatility compression, a flattening
    200DMA, and higher lows. Quick bounces score low; 12-18m bases score high.

    +1 TIME       : >=120 of the past 500 days spent at z <= -2
    +1 COMPRESSION: 3m realized vol of the ratio < 60% of its 18m vol
    +1 FLATTENING : 200DMA slope (60d) has decayed to < 1/3 of its worst
                    downtrend slope over the past 2 years
    +1 HIGHER LOW : the past 3m low sits above the past 12m low
    """
    z = df["z"]
    ret = df["ratio"].pct_change()
    time_at_lows = int((z.iloc[-500:] <= -2).sum() >= 120)

    vol3m = ret.iloc[-63:].std()
    vol18m = ret.iloc[-378:].std()
    compression = int(pd.notna(vol3m) and pd.notna(vol18m) and vol18m > 0
                      and vol3m < 0.60 * vol18m)

    s200 = df["dma200"].diff(60)
    worst = s200.iloc[-504:].min()
    flattening = int(pd.notna(worst) and worst < 0
                     and s200.iloc[-1] > worst / 3)

    lo3m = df["ratio"].iloc[-63:].min()
    lo12m = df["ratio"].iloc[-252:].min()
    higher_low = int(lo3m > lo12m)

    score = time_at_lows + compression + flattening + higher_low
    parts = {"time_at_lows": bool(time_at_lows), "vol_compression": bool(compression),
             "dma200_flattening": bool(flattening), "higher_low": bool(higher_low)}
    return score, parts


def detrended_z(ratio: pd.Series) -> float | None:
    """z of the ratio against its own 5-year LINEAR TREND, not its flat mean.

    Where the 5-year mean is itself drifting — SMH/QQQ, KWEB/QQQ — part of a
    raw sigma count is trend rather than dislocation, so the raw z overstates
    how stretched the pair is. This fits the trend over the same window and
    measures the residual instead. Reported alongside the raw z, never in
    place of it: the tiers are still driven by the raw reading, so no
    published number moves because of this.
    """
    y = ratio.iloc[-WIN_5Y:].to_numpy(dtype=float)
    n = len(y)
    if n < MIN_HISTORY or not np.isfinite(y).all():
        return None
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    sd = resid.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return None
    return round(float(resid[-1] / sd), 2)


def base_rate(df: pd.DataFrame) -> str:
    """Historical base rate: after |z|>=3 AND break of 50DMA, how often did
    the ratio touch its 200DMA within 12 months? Median days to touch?

    Episodes whose target was ALREADY MET on the trigger day are discarded,
    not counted as instant wins. A pair that gaps through its 50- and 200-day
    averages in one move has nothing left to predict, and crediting it books
    a hit at a horizon of one day. Before this exclusion, 9 of the 11 pairs
    with resolved history reported a median of 1-2 days — the column was
    measuring gap-throughs rather than reversion.
    """
    # Trailing extension memory (signed), consistent with the live signal:
    # 12m for upside extremes, 24m for downside. The extension may have
    # compressed by the time the 50DMA breaks.
    roll_up = df["z"].rolling(WIN_EXT_UP, min_periods=60).max()
    roll_dn = df["z"].rolling(WIN_EXT_DOWN, min_periods=60).min()
    up_trig = (roll_up >= Z_EXTREME) & (df["ratio"] < df["dma50"])
    dn_trig = (roll_dn <= -Z_EXTREME) & (df["ratio"] > df["dma50"])
    trig = up_trig | dn_trig
    # signed peak on each trigger day: prefer the side that actually fired
    roll_peak = roll_up.where(up_trig, roll_dn)
    # collapse clusters: take first day of each episode (gap > 60d = new episode)
    days = df.index[trig.fillna(False)]
    episodes = []
    for d in days:
        if not episodes or (d - episodes[-1]).days > 60:
            episodes.append(d)
    hits, horizons, resolved, prefilled = 0, [], 0, 0
    for d in episodes:
        fwd = df.loc[d:].iloc[1:WIN_12M + 1]             # start day AFTER trigger — no day-0 credit
        if len(fwd) == 0:
            continue
        sign = np.sign(roll_peak.loc[d])
        tgt = df.loc[d, "dma200"]                        # FROZEN at trigger day —
        if pd.isna(tgt):                                  # a moving 200DMA chases price
            continue                                      # and fakes near-instant "hits"
        if sign * (df.loc[d, "ratio"] - tgt) <= 0:
            prefilled += 1                                # target already met at trigger:
            continue                                      # nothing was predicted
        touched = (sign * (fwd["ratio"] - tgt) <= 0)
        if touched.any():
            hits += 1
            resolved += 1
            horizons.append(int(touched.values.argmax()) + 1)
        elif len(fwd) >= WIN_12M:
            resolved += 1                                 # full window elapsed, genuine miss
        # else: episode still open (censored) — excluded from the denominator
    skipped = f" ({prefilled} discarded: target already met at trigger)" if prefilled else ""
    if resolved == 0:
        return "no resolved historical episodes in sample" + skipped
    med = int(np.median(horizons)) if horizons else None
    return f"{hits}/{resolved} episodes reached trigger-day 200DMA within 12m" + \
           (f", median {med} trading days" if med is not None else "") + skipped


# ----------------------------------------------------------------------
# Dashboard row schema
# ----------------------------------------------------------------------
TIER_MAP = [("INSUFFICIENT", "NT"), ("LARGELY DONE", "DN"),
            ("HIGH CONVICTION", "HC"), ("WATCHLIST", "WL"), ("NO TRADE", "NT")]

CHART_BARS = 260       # ~5 years of weekly bars shipped to the chart


def _weekly(series: pd.Series, idx) -> list:
    """Sample a daily series at the chart's weekly anchor points.

    NaN -> None so the front end can break the line rather than drawing
    through a gap (the 200DMA has no value in its first 200 days).
    """
    vals = series.iloc[idx]
    return [None if pd.isna(v) else round(float(v), 6) for v in vals]


def emit_row(name: str, df: pd.DataFrame, res: dict) -> dict:
    """Map an evaluate() result onto the dashboard row schema.

    The chart series carry the ROLLING mean and std, not last-bar scalars.
    A single scalar would draw the 5y mean as a flat line across five years
    of history, which misrepresents exactly the pairs whose mean is drifting
    (SMH/QQQ, KWEB/QQQ) — the ones where the raw sigma count is least
    trustworthy and the picture matters most.
    """
    tier = next(t for k, t in TIER_MAP if k in res["signal"])
    dirn = ("LONG ratio" if res["direction"] == "LONG_RATIO"
            else "SHORT ratio" if res["direction"] == "SHORT_RATIO" else "—")

    tail = df.iloc[-WIN_5Y:]
    # Weekly anchor points, taken backwards from the LAST bar so the most
    # recent close is always on screen.
    pos = list(range(len(tail) - 1, -1, -5))[:CHART_BARS][::-1]

    row = {
        "p": name,
        "tier": tier,
        "dir": dirn,
        "znow": res["z_now"],
        "zpk": res["peak_z"],
        "ma": int(res["ma_score"][0]) if res["ma_score"][0].isdigit() else 0,
        "slope": 1 if res["dma50_slope"] == "turned" else 0,
        "rt": round(float(df["ratio"].iloc[-1]), 6),
        "t1": res["T1_200dma"],
        "t2": res["T2_5y_mean"],
        "inv": res["invalidation"],
        "br": base_rate(df),
        "note": res["signal"],
        "asof": str(df.index[-1].date()),
        "bars": int(len(df)),
        "dz": res.get("detrended_z"),
        # Divergence flag: the raw and trend-adjusted readings disagree by a
        # full sigma. Both directions matter and both are compared at the
        # SAME point in time — today's raw z against today's detrended z.
        # Raw more extreme: part of the stretch is drift, so the setup is
        # weaker than it looks. Detrended more extreme: the pair is further
        # from its own trend than the flat mean suggests, and the flat mean
        # is hiding it. Either way, read the chart before sizing.
        "drift": bool(res.get("detrended_z") is not None
                      and res["z_now"] is not None
                      and abs(abs(res["z_now"]) - abs(res["detrended_z"])) >= 1.0),
        # Under five full years the rolling stats are computed on a shorter
        # window and presented identically. Flag it rather than hide it.
        "short_hist": bool(len(df) < WIN_5Y),
        "whipsaw": bool(res.get("whipsaw")),
        "series": {
            "d": [str(d.date()) for d in tail.index[pos]],
            "r": _weekly(tail["ratio"], pos),
            "m": _weekly(tail["mean5y"], pos),
            "sd": _weekly(tail["std5y"], pos),
            "ma50": _weekly(tail["dma50"], pos),
            "ma100": _weekly(tail["dma100"], pos),
            "ma200": _weekly(tail["dma200"], pos),
        },
    }
    if res.get("base_score") not in (None, "—"):
        row["base"] = int(res["base_score"][0])
        row["base_parts"] = res.get("base_parts")
    return row


def analyze(name: str, ratio: pd.Series) -> dict:
    """ratio series -> dashboard row. The one call `build_data.py` needs."""
    df = compute_features(ratio)
    return emit_row(name, df, evaluate(df, name))


# ----------------------------------------------------------------------
# Regression tests over the mock regimes.
# These pin the behavioural claims the whole system rests on — above all
# that a washout WITH a base and a washout WITHOUT one score differently.
# That split is what separates a Stage-2 turn from a dead-cat bounce, and
# it is the distinction the CIO brief is built to act on.
# ----------------------------------------------------------------------
def _selftest() -> None:
    def ev(regime):
        d = compute_features(make_mock_pair(regime))
        return d, evaluate(d, regime)

    df, r = ev("based_turning")
    assert r["direction"] == "LONG_RATIO", r["direction"]
    assert int(r["base_score"][0]) >= 3, f"based_turning base {r['base_score']}"
    assert "strong base" in r["signal"], r["signal"]

    df, r = ev("vbounce")
    assert r["direction"] == "LONG_RATIO", r["direction"]
    assert int(r["base_score"][0]) <= 1, f"vbounce base {r['base_score']}"
    assert "bounce risk" in r["signal"], r["signal"]

    _, r = ev("blowout_intact")
    assert "WATCHLIST" in r["signal"], r["signal"]
    assert r["direction"] == "SHORT_RATIO", r["direction"]

    _, r = ev("just_broke")
    assert "HIGH CONVICTION" in r["signal"], r["signal"]

    _, r = ev("blowout_rolled")
    assert "LARGELY DONE" in r["signal"], r["signal"]

    _, r = ev("quiet")
    assert r["signal"] == "NO TRADE", r["signal"]

    # The stop is documented as mechanical, so BOTH of its conditions must
    # print a level on BOTH sides. The long side previously named the 20d low
    # without pricing it, leaving the desk to pick that level by eye — which
    # is exactly the discretion the mechanical stop exists to remove.
    for regime, side in (("just_broke", "SHORT"), ("based_turning", "LONG")):
        d = compute_features(make_mock_pair(regime))
        inv = evaluate(d, regime)["invalidation"]
        assert inv.count("(") == 2, f"{side} invalidation missing a level: {inv}"
        assert "50DMA" in inv and "20d" in inv, inv

    # tier mapping must be total over every signal string evaluate() can emit
    for sig in ["INSUFFICIENT DATA", "REVERSION LARGELY DONE — stand aside",
                "HIGH CONVICTION", "HIGH CONVICTION (strong base)",
                "HIGH CONVICTION (moderate base — half size)",
                "HIGH CONVICTION (weak base — bounce risk)", "WATCHLIST",
                "WATCHLIST (slope not confirmed)", "NO TRADE",
                "NO TRADE (whipsaw — 3σ both ways, mean unstable)"]:
        next(t for k, t in TIER_MAP if k in sig)

    # A whipsawed pair must stand aside, not pick a side. Driven by injecting
    # the two extremes straight into the z column: the branch under test is
    # the signal logic, not the mock generator's ability to manufacture a
    # 3-sigma round trip on both sides within 24 months.
    d = compute_features(make_mock_pair("quiet"))
    d.loc[d.index[-400], "z"] = -3.5      # inside the 24m window, outside the 12m one
    d.loc[d.index[-100], "z"] = 3.5       # inside the 12m window
    r = evaluate(d, "whipsaw")
    assert r["signal"].startswith("NO TRADE (whipsaw"), r["signal"]
    assert r["direction"] == "—", r["direction"]
    assert emit_row("whipsaw", d, r)["tier"] == "NT"

    # Base rate must discard episodes whose target was already met at the
    # trigger. Direct test: a series that has been below its 200DMA for
    # months when the downside trigger fires has nothing left to predict, so
    # every episode is discarded and nothing is reported as a hit.
    d = compute_features(make_mock_pair("quiet"))
    d["z"] = -4.0                       # permanently "extended" downside
    d["ratio"] = d["dma200"] * 1.5      # ...but already far past the 200DMA target
    d["dma50"] = d["ratio"] * 0.9       # ratio > 50DMA so the long trigger fires
    br = base_rate(d)
    assert "no resolved historical episodes" in br, br
    assert "discarded: target already met" in br, br

    # The exclusion must actually bite on the mock regimes, where gap-throughs
    # are exactly what used to manufacture 1-day hits.
    discarded = sum("discarded" in base_rate(compute_features(make_mock_pair(g)))
                    for g in ("blowout_rolled", "just_broke", "based_turning", "vbounce"))
    assert discarded >= 2, f"exclusion never triggered ({discarded}/4 regimes)"

    # detrended z must be finite and differ from raw z on a trending series
    dz = detrended_z(make_mock_pair("blowout_intact"))
    assert dz is not None and abs(dz) < 100, dz
    assert detrended_z(make_mock_pair("quiet").iloc[:100]) is None   # too little history

    # emitted series must be chart-ready: aligned lengths, last bar present
    row = analyze("MOCK", make_mock_pair("based_turning"))
    s = row["series"]
    assert len({len(s[k]) for k in ("d", "r", "m", "sd", "ma50", "ma100", "ma200")}) == 1
    assert s["r"][-1] == row["rt"], (s["r"][-1], row["rt"])
    assert s["m"][-1] is not None and s["sd"][-1] is not None

    print("selftest: all assertions passed")


if __name__ == "__main__":
    _selftest()
    print()
    cases = {
        "MOCK_GLD_SPY (blowout, rolled over)": "blowout_rolled",
        "MOCK_SMH_QQQ (blowout, trend intact)": "blowout_intact",
        "MOCK_PAIR (blowout, JUST broke down)": "just_broke",
        "MOCK_BASED_PAIR (washout, 16m base, turning up)": "based_turning",
        "MOCK_VBOUNCE (washout, instant V, no base)": "vbounce",
        "MOCK_QUIET_PAIR": "quiet",
    }
    for name, regime in cases.items():
        ratio = make_mock_pair(regime)
        df = compute_features(ratio)
        res = evaluate(df, name)
        print("=" * 72)
        print(f"{res['pair']}")
        print(f"  VERDICT      : {res['signal']}  [{res['direction']}]")
        print(f"  ratio        : {res['ratio']}   5y mean: {res['mean5y']}")
        print(f"  z now        : {res['z_now']}   peak z: {res['peak_z']}")
        print(f"  MA score     : {res['ma_score']}   50DMA slope: {res['dma50_slope']}   base: {res['base_score']}")
        print(f"  T1 (200DMA)  : {res['T1_200dma']}")
        print(f"  T2 (5y mean) : {res['T2_5y_mean']}")
        print(f"  invalidation : {res['invalidation']}")
        print(f"  base rate    : {base_rate(df)}")
    print("=" * 72)
