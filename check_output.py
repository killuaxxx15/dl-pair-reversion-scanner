"""Gate between building the data and publishing it.

The dashboard is deployed straight from the committed JSON, so a bad build
would be live within a minute of being pushed. This refuses to let an empty,
truncated, or stale dataset through. Exit non-zero fails the workflow step
and nothing gets committed.

Run it by hand any time:  python check_output.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIGNALS = ROOT / "pairs_signals.json"
BRIEF = ROOT / "cio_brief.json"

# Yahoo can lag, and a long weekend plus a US holiday is a genuine 4-day gap.
# Beyond that the feed is stuck and we should not republish it as today's view.
MAX_STALE_DAYS = 5


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def main() -> int:
    if not SIGNALS.exists():
        fail(f"{SIGNALS.name} was not written")

    try:
        data = json.loads(SIGNALS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{SIGNALS.name} is not valid JSON: {e}")

    meta, rows = data.get("meta", {}), data.get("pairs", [])
    if not rows:
        fail("no pairs in output — refusing to publish an empty dataset")

    # Every row the dashboard draws needs its series, or the chart is blank.
    for key in ("r", "m", "sd"):
        broken = [r["p"] for r in rows if not r.get("series", {}).get(key)]
        if broken:
            fail(f"missing series '{key}' for: {', '.join(broken)}")

    ragged = [r["p"] for r in rows
              if len({len(r["series"][k]) for k in
                      ("d", "r", "m", "sd", "ma50", "ma100", "ma200")}) != 1]
    if ragged:
        fail(f"series of unequal length for: {', '.join(ragged)}")

    asof = meta.get("asof")
    if not asof:
        fail("meta.asof is missing")
    age = (date.today() - datetime.fromisoformat(asof).date()).days
    if age > MAX_STALE_DAYS:
        fail(f"data is {age} days old (as of {asof}) — the price feed looks stuck")

    if BRIEF.exists():
        brief = json.loads(BRIEF.read_text(encoding="utf-8"))
        b_asof = brief.get("meta", {}).get("signals_asof")
        if b_asof != asof:
            # Not fatal: the dashboard shows its own staleness warning, and a
            # stale brief is better than none. Surface it in the run log.
            print(f"::warning::cio_brief.json is built on signals from "
                  f"{b_asof}, not {asof}")

    tiers = Counter(r["tier"] for r in rows)
    print(f"{len(rows)} pairs, as of {asof} ({age}d old), "
          f"{meta.get('pairs_failed', 0)} failed")
    print("tiers: " + ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    for e in meta.get("errors", []):
        print(f"::warning::{e['p']} failed — {e['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
