"""Markdown summary of a build, for the GitHub Actions run page.

Prints to stdout; the workflow appends it to $GITHUB_STEP_SUMMARY so the
actionable pairs are readable from the Actions tab without opening the site.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIER_ORDER = {"HC": 0, "WL": 1, "DN": 2, "NT": 3}
TIER_NAME = {"HC": "HIGH CONVICTION", "WL": "WATCHLIST",
             "DN": "LARGELY DONE", "NT": "NO TRADE"}


def main() -> None:
    p = ROOT / "pairs_signals.json"
    if not p.exists():
        print("### Daily signals\n\nNo data was produced — the build failed "
              "before writing `pairs_signals.json`.")
        return

    d = json.loads(p.read_text(encoding="utf-8"))
    meta, rows = d["meta"], d["pairs"]
    print("### Daily signals")
    print(f"\n**As of {meta.get('asof')}** · {meta.get('pairs_ok')} pairs built"
          + (f" · **{meta['pairs_failed']} failed**" if meta.get("pairs_failed") else ""))

    live = [r for r in rows if r["tier"] in ("HC", "WL")]
    if live:
        print("\n| Pair | Tier | Direction | z | peak z | Base | Flags | Base rate |")
        print("|---|---|---|--:|--:|--:|---|---|")
        for r in sorted(live, key=lambda r: (TIER_ORDER[r["tier"]], -abs(r["zpk"] or 0))):
            flags = " ".join(f for f, on in
                             (("`z DIVERGES`", r.get("drift")),
                              ("`SHORT HISTORY`", r.get("short_hist")),
                              ("`WHIPSAW`", r.get("whipsaw"))) if on) or "—"
            base = f"{r['base']}/4" if "base" in r else "—"
            print(f"| **{r['p']}** | {TIER_NAME[r['tier']]} | {r['dir']} | "
                  f"{r['znow']} | {r['zpk']} | {base} | {flags} | {r['br']} |")
    else:
        print("\nNothing actionable — no pair is HIGH CONVICTION or WATCHLIST today.")

    den = meta.get("actionable_denominators") or {}
    if den and max(den.values()) > 1:
        top, n = max(den.items(), key=lambda kv: kv[1])
        print(f"\n> **Concentration:** {n} of {meta.get('actionable_count')} "
              f"actionable pairs share a `{top}` denominator.")

    b = ROOT / "cio_brief.json"
    if b.exists():
        bd = json.loads(b.read_text(encoding="utf-8"))
        bm = bd.get("meta", {})
        print(f"\n**CIO brief** — {bm.get('reviewed')}/{bm.get('requested')} "
              f"reviewed by {bm.get('model')}"
              + (" ⚠️ built on older signals"
                 if bm.get("signals_asof") != meta.get("asof") else ""))
        for e in bd.get("brief", []):
            print(f"- **{e['pair']}** — {e['verdict']} ({e['regime_risk']} regime risk): "
                  f"{e['report_line']}")
    else:
        print("\n_No CIO brief in this run._")

    for e in meta.get("errors", []):
        print(f"\n> ⚠️ `{e['p']}` failed — {e['error']}")


if __name__ == "__main__":
    main()
