"""
DL Pair Reversion — data builder.

The only script you run. It does two jobs, and writes one JSON file for each:

    python build_data.py                 pairs.json -> yfinance -> pairs_signals.json
    python build_data.py --brief         also: signals -> Claude API -> cio_brief.json
    python build_data.py --brief-only    reuse existing signals, redo the brief

The dashboard reads those two JSON files and does no computation and no
network calls of its own. Nothing in the HTML needs editing when the data
changes.

Setup:
    pip install -r requirements.txt
    copy .env.example .env      # then put your key in .env  (--brief only)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import pair_reversion as engine

ROOT = Path(__file__).resolve().parent
PAIRS_FILE = ROOT / "pairs.json"
SIGNALS_FILE = ROOT / "pairs_signals.json"
BRIEF_FILE = ROOT / "cio_brief.json"

HISTORY = "10y"          # 5y of signal needs 5y of lookback to build the rolling stats
DEFAULT_MODEL = "claude-opus-4-8"


# ----------------------------------------------------------------------
# Prices
# ----------------------------------------------------------------------
def load_pairs(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        pairs = json.load(f)
    seen, out = set(), []
    for p in pairs:
        a, b = p["numerator"].strip().upper(), p["denominator"].strip().upper()
        if a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        out.append({"numerator": a, "denominator": b, "core": bool(p.get("core"))})
    return out


def fetch_closes(tickers: list[str]) -> pd.DataFrame:
    """One batched download for the whole universe.

    Per-pair fetching would re-download the denominator once per pair — SPY
    is the denominator of most of this universe — which is both slow and the
    fastest way to get rate-limited by Yahoo at ~50 pairs.
    """
    import yfinance as yf

    print(f"downloading {len(tickers)} tickers ({HISTORY}) …")
    px = yf.download(tickers, period=HISTORY, auto_adjust=True,
                     progress=False, group_by="column")
    if px is None or px.empty:
        raise RuntimeError("yfinance returned no data at all — check network access")

    close = px["Close"] if isinstance(px.columns, pd.MultiIndex) else px[["Close"]]
    if not isinstance(px.columns, pd.MultiIndex):
        close.columns = tickers[:1]          # single-ticker download has flat columns

    close = close.sort_index()
    missing = [t for t in tickers if t not in close.columns or close[t].dropna().empty]
    if missing:
        print(f"WARNING: no price data for {', '.join(missing)}")
    return close


def build_signals(pairs: list[dict]) -> dict:
    tickers = sorted({p["numerator"] for p in pairs} | {p["denominator"] for p in pairs})
    close = fetch_closes(tickers)

    rows, errors = [], []
    for p in pairs:
        a, b, name = p["numerator"], p["denominator"], f"{p['numerator']}/{p['denominator']}"
        try:
            if a not in close.columns or b not in close.columns:
                raise KeyError(f"missing price series for {a if a not in close.columns else b}")
            ratio = (close[a] / close[b]).replace([float("inf"), float("-inf")], pd.NA).dropna()
            if len(ratio) < engine.MIN_HISTORY:
                raise ValueError(f"only {len(ratio)} trading days, need {engine.MIN_HISTORY}")
            row = engine.analyze(name, ratio)
            row["core"] = p["core"]
            rows.append(row)
            print(f"  {name:<12} {row['tier']:<3} z={row['znow']:>6} peak={row['zpk']:>6}  {row['note']}")
        except Exception as e:                # one bad ticker must not kill the batch
            errors.append({"p": name, "error": f"{type(e).__name__}: {e}"})
            print(f"  {name:<12} SKIPPED — {e}")

    asof = max((r["asof"] for r in rows), default=None)

    univ_den = Counter(r["p"].split("/")[1] for r in rows)

    # Reference price for an at-a-glance freshness check. SPY is the
    # denominator of most of this universe, so its last close is a number the
    # reader can compare against a live quote to confirm the data is current.
    # Its date is carried too: if it lags `asof`, one leg is stale. Falls back
    # to the most common denominator when SPY is not in the universe. This is
    # the last bar, so the auto-adjusted close equals the actual closing price.
    ref = "SPY" if "SPY" in close.columns else (
        univ_den.most_common(1)[0][0] if univ_den else None)
    ref_price = ref_date = None
    if ref and ref in close.columns:
        s = close[ref].dropna()
        if not s.empty:
            ref_price = round(float(s.iloc[-1]), 2)
            ref_date = s.index[-1].date().isoformat()

    # Denominator concentration. The signals are not independent: where most
    # of the book is measured against one leg, a single move in that leg
    # re-rates many pairs at once. Counted over the ACTIONABLE tiers, since
    # that is what would actually be held.
    live = [r for r in rows if r["tier"] in ("HC", "WL")]
    den = Counter(r["p"].split("/")[1] for r in live)

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "asof": asof,
            "ref_ticker": ref,
            "ref_price": ref_price,
            "ref_date": ref_date,
            "source": "Yahoo Finance (yfinance, auto_adjust=True)",
            "history": HISTORY,
            "pairs_ok": len(rows),
            "pairs_failed": len(errors),
            "errors": errors,
            "universe_denominators": dict(univ_den.most_common()),
            "actionable_denominators": dict(den.most_common()),
            "actionable_count": len(live),
        },
        "pairs": rows,
    }


# ----------------------------------------------------------------------
# CIO brief (Claude API)
# ----------------------------------------------------------------------
BRIEF_SYSTEM = """You are the risk reviewer for a systematic mean-reversion pair-trade book at a family office.

A quant engine has flagged pairs whose price ratio reached >= 3 sigma from its 5-year mean, with moving-average confirmation for the HIGH CONVICTION tier. Your single job, for each pair: judge whether the stretched relationship is a RUBBER BAND (temporary dislocation, mean reversion valid) or a BROKEN BAND (structural regime change — central bank gold buying, AI capex cycles, rate regime shifts, secular index-share changes — making the old 5-year mean an invalid target).

Search the web to check current conditions before judging. Search efficiently: a few searches covering the major themes across pairs, not one per pair.

Verdicts:
  ACT     — clean reversion, regime risk low.
  CAUTION — tradeable but size down, regime risk real.
  AVOID   — the mean itself has likely moved; the statistics are a trap.

Judge each pair independently. A HIGH CONVICTION quant flag does NOT mean the answer is ACT. For LONG ratio pairs weight base_quality heavily: 3/4+ suggests a durable Stage-1 base turning up; 2/4 is a half-size case; 0-1/4 suggests a dead-cat bounce even when the z-score case looks identical.

Two fields carry specific meaning. `detrended_z` measures the ratio against its own 5-year linear trend rather than a flat mean; when `raw_and_detrended_diverge` is true the two readings disagree by more than a sigma, which is direct evidence about whether the mean is drifting — cite it rather than speculating. `base_rate` counts only episodes that still had ground to cover when they triggered, so it is a genuine forward hit rate; do not discount it as noise.

Your verdict informs the desk and never gates it: it cannot block a trade the engine flagged, nor originate one it did not. Write to help a portfolio manager disagree with you productively.

Write in plain English for a reader who does not live in quant vocabulary: no "sigma", "z-score", "detrended", "regime" or "basis" without saying what it means in the same breath. Prefer "silver has run far ahead of gold" to "the ratio is 6 sigma rich". The analysis and report_line must read aloud naturally in an investment-committee meeting."""

BRIEF_TASK = """Pairs data:
{payload}

Respond with ONLY a JSON array, no markdown fences and no preamble. One object per pair, every field required:

{{"pair": "...", "verdict": "ACT" | "CAUTION" | "AVOID", "regime_risk": "LOW" | "MEDIUM" | "HIGH", "analysis": "2-3 plain sentences: the strongest argument FOR regime change, and your judgment on whether the 5-year mean remains a valid target. Concrete, no hedging.", "report_line": "One sentence, copy-ready for a CIO report, stating the position and the key risk."}}"""

VERDICTS = {"ACT", "CAUTION", "AVOID"}
RISKS = {"LOW", "MEDIUM", "HIGH"}


def _snapshot(rows: list[dict], include_wl: bool) -> list[dict]:
    tiers = {"HC", "WL"} if include_wl else {"HC"}
    return [{
        "pair": r["p"],
        "tier": r["tier"],
        "direction": r["dir"],
        "current_z": r["znow"],
        "peak_z": r["zpk"],
        "ma_score": f"{r['ma']}/3",
        "dma50_slope": "turned" if r["slope"] else "intact",
        "ratio": r["rt"],
        "target_200dma": r["t1"],
        "target_5y_mean": r["t2"],
        "invalidation": r["inv"],
        "base_rate": r["br"],
        "base_quality": f"{r['base']}/4" if "base" in r else "n/a — short side",
        "detrended_z": r.get("dz"),
        "raw_and_detrended_diverge": bool(r.get("drift")),
        "short_history": bool(r.get("short_hist")),
        "engine_note": r["note"],
    } for r in rows if r["tier"] in tiers]


def _extract_json_array(text: str) -> list:
    start = text.find("[")
    if start < 0:
        raise ValueError("model response contained no JSON array")
    body = text[start:]
    end = body.rfind("]")
    if end >= 0:
        try:
            return json.loads(body[:end + 1])
        except json.JSONDecodeError:
            pass
    # Salvage a response that ran out of tokens mid-object rather than losing
    # the whole batch. With max_tokens where it is now this should not fire;
    # if it does, the count check below reports how many pairs were lost.
    cut = body.rfind("},")
    if cut < 0:
        raise ValueError("model response was truncated beyond recovery")
    return json.loads(body[:cut + 1] + "]")


def _validate(brief: list, expected: set[str]) -> list[dict]:
    clean = []
    for i, b in enumerate(brief):
        if not isinstance(b, dict):
            raise ValueError(f"entry {i} is not an object")
        pair = str(b.get("pair", "")).strip()
        if pair not in expected:
            print(f"WARNING: dropping unrecognised pair {pair!r} from brief")
            continue
        verdict = str(b.get("verdict", "")).strip().upper()
        risk = str(b.get("regime_risk", "")).strip().upper()
        if verdict not in VERDICTS:
            raise ValueError(f"{pair}: bad verdict {b.get('verdict')!r}")
        if risk not in RISKS:
            raise ValueError(f"{pair}: bad regime_risk {b.get('regime_risk')!r}")
        clean.append({
            "pair": pair,
            "verdict": verdict,
            "regime_risk": risk,
            "analysis": str(b.get("analysis", "")).strip(),
            "report_line": str(b.get("report_line", "")).strip(),
        })
    return clean


def build_brief(signals: dict, include_wl: bool, model: str) -> dict:
    from anthropic import Anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set.\n"
            "  1. copy .env.example to .env\n"
            "  2. put your key in it\n"
            "  3. re-run. (Get a key at https://console.anthropic.com/settings/keys)"
        )

    snapshot = _snapshot(signals["pairs"], include_wl)
    if not snapshot:
        raise SystemExit("No HIGH CONVICTION pairs to review. "
                         "Use --include-watchlist to review WATCHLIST pairs too.")

    print(f"reviewing {len(snapshot)} pairs with {model} (web search enabled) …")
    client = Anthropic()
    # Streaming: a web-search turn can run long, and a non-streaming request
    # with max_tokens this size risks an HTTP timeout.
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        system=BRIEF_SYSTEM,
        messages=[{"role": "user",
                   "content": BRIEF_TASK.format(payload=json.dumps(snapshot, indent=1))}],
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise SystemExit("The model declined this request. Nothing was written.")

    text = "\n".join(b.text for b in message.content if b.type == "text")
    expected = {s["pair"] for s in snapshot}
    entries = _validate(_extract_json_array(text.replace("```json", "").replace("```", "")),
                        expected)

    missing = sorted(expected - {e["pair"] for e in entries})
    if missing:
        print(f"WARNING: no verdict returned for {', '.join(missing)}")
    if message.stop_reason == "max_tokens":
        print("WARNING: response hit max_tokens — raise it or reduce the pair count")

    order = {"AVOID": 0, "CAUTION": 1, "ACT": 2}
    entries.sort(key=lambda e: (order[e["verdict"]], e["pair"]))
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": message.model,
            "signals_asof": signals["meta"].get("asof"),
            "reviewed": len(entries),
            "requested": len(snapshot),
            "missing": missing,
            "included_watchlist": include_wl,
            "usage": {"input_tokens": message.usage.input_tokens,
                      "output_tokens": message.usage.output_tokens},
        },
        "brief": entries,
    }


# ----------------------------------------------------------------------
def write_json(path: Path, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)
    print(f"wrote {path.name}  ({path.stat().st_size / 1024:.0f} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the dashboard's JSON data files.")
    ap.add_argument("--brief", action="store_true",
                    help="also run the Claude regime-change review")
    ap.add_argument("--brief-only", action="store_true",
                    help="skip the price fetch; re-run the review on existing signals")
    ap.add_argument("--include-watchlist", action="store_true",
                    help="review WATCHLIST pairs as well as HIGH CONVICTION")
    ap.add_argument("--pairs", default=str(PAIRS_FILE), help="pair universe JSON")
    ap.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL))
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass                                   # only needed for --brief

    if args.brief_only:
        if not SIGNALS_FILE.exists():
            print(f"{SIGNALS_FILE.name} not found — run without --brief-only first.")
            return 1
        signals = json.loads(SIGNALS_FILE.read_text(encoding="utf-8"))
    else:
        signals = build_signals(load_pairs(Path(args.pairs)))
        if not signals["pairs"]:
            print("No pairs produced a signal — nothing written.")
            return 1
        write_json(SIGNALS_FILE, signals)

    if args.brief or args.brief_only:
        write_json(BRIEF_FILE, build_brief(signals, args.include_watchlist, args.model))

    print("\nServe the folder and open the dashboard:")
    print("  python -m http.server 8000")
    print("  http://localhost:8000/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
