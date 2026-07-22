# DL Pair Reversion Scanner — Team Handoff

Long-term contrarian pair-trade system. The idea in one line: a pair is a
candidate when its ratio has been **3 sigma above or below its 5-year mean**,
and the **near-term trend has turned against the extension** — that turn is
the catalyst. The long side (washed-out ratios building bases) is the
priority book.

The full plain-English methodology lives inside the dashboard — open the
HTML and read the **"How it works"** tab (9 sections, ~5 minutes). The
engine file is the source of truth for the math; its header docstring is
the finalized spec.

## Shape of the system

Everything is computed in Python and written to JSON. The dashboard renders
that JSON and does nothing else — no calculation, no network calls, no API
keys in the browser.

```
pairs.json ──▶ build_data.py ──▶ pairs_signals.json ──┐
                    │                                 ├──▶ dashboard (display only)
                    └── Claude API ──▶ cio_brief.json ─┘
```

| File | What it is |
|---|---|
| `build_data.py` | The only script you run. Fetches prices, runs the engine, optionally calls the Claude API. Writes the two JSON files. |
| `pair_reversion.py` | Signal engine. Pure computation — no network, no file I/O. Run it directly for the regression suite. |
| `index.html` | Dashboard. Four tabs: Scanner, How it works, Pairs, CIO Brief. Reads the JSON files. |
| `check_output.py` | Publish gate — refuses to ship an empty, ragged, or stale dataset. |
| `summarise_run.py` | Markdown digest of a build, for the Actions run page. |
| `.github/workflows/` | Daily 06:00 Singapore build. |
| `pairs.json` | The pair universe. Edit by hand, or from the dashboard's Pairs tab. |
| `pairs_signals.json` | **Generated.** One row per pair incl. 5y weekly price history. |
| `cio_brief.json` | **Generated**, only with `--brief`. AI regime-change verdicts. |
| `.env` | Your Anthropic API key. Gitignored. Needed only for `--brief`. |
| `vercel.json` / `.vercelignore` | Static-deploy config: no build step, no caching of the data files, Python excluded. |

## Quick start

```bash
pip install -r requirements.txt

python build_data.py            # prices only — no API key needed
python -m http.server 8000
# open http://localhost:8000
```

**The page must be served over HTTP.** Opening `index.html` directly from
disk will show an error panel with these instructions — browsers block
`fetch()` on `file://` URLs, so the JSON can't be read. Any static host
works; there is no build step.

To add the CIO Brief tab:

```bash
cp .env.example .env            # then paste your key into .env
python build_data.py --brief
```

| Command | Does |
|---|---|
| `python build_data.py` | Prices → `pairs_signals.json` |
| `python build_data.py --brief` | The above, plus the AI review |
| `python build_data.py --brief-only` | Re-run the review on existing signals (no refetch) |
| `--include-watchlist` | Review WATCHLIST pairs too, not just HIGH CONVICTION |
| `python pair_reversion.py` | Regression suite + the six mock regimes |

Prices come from Yahoo Finance via `yfinance` with `auto_adjust=True`, so
splits and dividends are handled — which settles Open Decision 1 below.
The whole universe is downloaded in **one** batched request; a bad ticker
is skipped with a warning and reported in the dashboard header rather than
killing the batch.

## Automation — GitHub Actions

`.github/workflows/daily-signals.yml` runs the whole pipeline **every day at
06:00 Singapore** and commits the result, which redeploys the site.

GitHub cron is UTC-only, so 06:00 SGT is `20 22 * * *` — 22:20 UTC the
previous day. Singapore observes no daylight saving, so this holds all year
with no seasonal adjustment. The `:20` is deliberate: scheduled runs are
best-effort and the top of the hour is the most contended slot.

One secret to set — **Settings → Secrets and variables → Actions → New
repository secret**:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your key |

Without it the price pipeline still runs and publishes; only the brief is
skipped, with a warning in the run log.

The job runs in this order, and stops before publishing if any step fails:

1. `python pair_reversion.py` — the engine's own regression suite. If the
   signal logic is broken we do not want the data it would produce.
2. `python build_data.py` — fetch prices, compute signals.
3. `python build_data.py --brief-only --include-watchlist` — the AI review.
   Marked `continue-on-error`: a bad day at the API costs the brief, never
   the quant screen.
4. `python check_output.py` — refuses to publish an empty dataset, ragged
   series, or a feed that has gone stale (>5 days, which tolerates a long
   weekend plus a holiday).
5. Commit and push, rebasing first so a manual run cannot collide with the
   scheduled one. No commit if the data is unchanged.
6. A markdown summary of the actionable pairs on the Actions run page, so
   the morning read is available without opening the site.

Run it by hand from the **Actions** tab (`Run workflow`) — worth doing once
after the first push to confirm the secret is wired up. There is a
`skip_brief` input for a prices-only run.

To run the same thing locally: `python build_data.py --brief`.

## Maintaining the pair list

Add or remove pairs in the dashboard's **Pairs** tab (dropdowns, custom
tickers, core pairs starred), then **Download pairs.json**, drop it next to
`build_data.py`, and rebuild. The tab's own copy lives in browser storage
and is a scratchpad — `pairs.json` on disk is what the engine actually
reads. A pair shows PENDING ENGINE RUN until it appears in the signals file.

## Reading the output (Matt, Aditya)

- **HIGH CONVICTION** — actionable. On longs, check the BASE chip:
  3–4/4 = durable base (size normally), 0–1/4 = bounce risk (half size
  or pass). Expanding a row lists which of the four base tests passed.
- **WATCHLIST** — extended but trend intact. Do not fight it.
- **LARGELY DONE** — the setup worked without us; <10% remains to the
  mean. Stand aside.
- **Base rate** column — resolved historical episodes only; e.g.
  `3/4 · med 41d` = 3 of 4 past setups reached the trigger-day 200DMA,
  median 41 trading days. Episodes still open, and episodes whose target was
  already met when they triggered, are both excluded — so the samples are
  small by design and the rate is a genuine forward hit rate.
- **Row flags** — `z DIVERGES` (raw and trend-adjusted z disagree by over a
  sigma: read the chart before sizing), `SHORT HISTORY` (under five full
  years), `WHIPSAW` (3σ both ways — mean unstable, stand aside). Hover any
  flag for the underlying numbers.
- Click any row for the 5-year chart (ratio, rolling 5y mean, ±1/2/3σ
  rails, 50/100/200 DMA) plus targets and the mechanical invalidation. The
  mean and sigma rails are drawn as **curves, not flat lines**, because
  they are rolling — a pair whose mean is visibly sloping is one whose raw
  sigma count is partly trend, not dislocation.
- **CIO Brief** tab — AI regime-change review (rubber band vs broken
  band) with ACT / CAUTION / AVOID per pair and a copy-ready line for
  the report. It is a first draft of the risk review, not the risk
  review. Final judgment stays with us. The tab warns if the brief is
  older than the signals currently loaded.

## Decisions taken

These were open questions; they are now settled in the engine. Raise any of
them if you disagree — each is a small, isolated change to reverse.

**1. Base-rate episodes whose target was already met are discarded.** An
episode fires on `|z|≥3` plus a 50DMA break, but nothing required the ratio
to still be *short of* its 200DMA at that moment. A pair that gapped through
both averages booked a hit at a horizon of one day having predicted nothing.
Before the fix, 9 of the 11 pairs with resolved history showed a median of
1–2 days — the column was measuring gap-throughs. After it, medians run
10–101 days and hit rates fell across the board:

| Pair | Before | After |
|---|---|---|
| SLV/GLD | 9/11, median 1d | **4/6, median 29d** (5 discarded) |
| ITA/SPY | 10/13, median 1d | **3/6, median 35d** (7 discarded) |
| XBI/SPY | 13/15, median 1d | **5/7, median 10d** (8 discarded) |
| TLT/SPY | 6/6, median 1d | **2/2, median 28d** (4 discarded) |

Smaller samples and worse-looking rates, both of which are the honest
numbers. **Any base rate quoted in a report before this change should be
treated as withdrawn.** The discarded count is shown in the column.

**2. A 2/4 base score means half size.** ≥3/4 size normally, 2/4 half size,
≤1/4 bounce risk. 2/4 previously fell through unlabelled, which is where
most long candidates actually sit — the desk was improvising on a reading
the tool had declined to interpret.

**3. A whipsawed pair stands aside.** Where a ratio reached 3σ in *both*
directions inside the lookback, the engine used to fade the larger extreme.
It now drops to NO TRADE with a `WHIPSAW` flag. A pair that has done both is
telling us its 5-year mean is unstable, and every target we derive — the
200DMA, the mean, the invalidation level — inherits that instability.

**4. Detrended z is reported alongside the raw z, never instead of it.**
Each pair now carries a second z-score measured against its own 5-year
*trend line*. Where the two disagree by more than a sigma the row is flagged
`z DIVERGES` and both are shown on the expanded row. Tiers are still driven
by the raw number, so nothing was re-tiered by this. It earns its keep: the
CIO brief now cites it directly — ITA/SPY moved from CAUTION to AVOID on the
evidence that its mean is drifting.

**5. Denominator concentration is surfaced, not just documented.** The
scanner shows a banner when the actionable pairs share a denominator, since
one move in that leg re-rates them together. Counted over HC and WL only —
what would actually be held.

**6. Pairs under five years of history are flagged `SHORT HISTORY`.** The
rolling stats will compute on as little as 2.5 years and previously looked
identical to a full-window reading. Nothing in the current universe trips
it; the first newer ETF added will.

**7. The AI brief informs and never gates.** An AVOID cannot block a trade
the engine flagged; an ACT cannot originate one it did not. Stated on the
tab itself so it cannot drift into being a veto by default.

**Left deliberately alone:** the flat 10% "largely done" threshold (legible,
and volatility-scaling it adds a tuned parameter for an unmeasured gain);
Yahoo Finance as the source (fine for research — move to a vendor feed
before real capital, and note the daily commits give us a free audit trail
of revisions). Hedge ratios remain sizing, not signal.

## Deployment — GitHub + Vercel (Jermaine)

The deploy is **static**. Nothing runs on Vercel: the pipeline runs on your
machine and the JSON it produces is committed as the deployed artifact.

```bash
git init && git add . && git commit -m "DL pair reversion scanner"
git remote add origin git@github.com:<org>/<repo>.git
git push -u origin main
```

Then import the repo at [vercel.com/new](https://vercel.com/new) — framework
preset **Other**, no build command, no environment variables. `vercel.json`
sets the JSON and HTML to revalidate on every request so a rebuilt dataset
appears immediately rather than sitting behind a CDN cache.

**The generated JSON files are committed on purpose.** They are the data the
site serves; `.gitignore` calls this out so nobody "tidies" them away. The
site shows a No data loaded panel without them. `.vercelignore` keeps the
Python out of the deploy — it is never executed there.

To publish an update:

```bash
python build_data.py --brief
git commit -am "signals $(date +%F)" && git push
```

Vercel redeploys on push. A daily cron on your side (or a GitHub Action with
the key in repo secrets, if you want it hands-off) is enough — these are
long-horizon signals.

**`.env` is gitignored and must stay that way.** The API key never reaches
the browser and never reaches Vercel: the brief is generated locally and
shipped as data, so there is no serverless proxy to build and nothing to
leak. If a key is ever committed, rotate it at
[console.anthropic.com](https://console.anthropic.com/settings/keys) —
scrubbing the commit is not sufficient.

`pairs_signals.json` is ~470 KB for 16 pairs (each carries 5 years of weekly
history for the charts). At ~50 pairs expect ~1.5 MB. Vercel gzips static
assets automatically, which takes it to roughly a tenth of that.

— Built and audited July 2026. Engine and dashboard describe the same
system; if they ever disagree, the engine docstring wins.
