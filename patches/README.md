# Patches for `catalyst-edge-os/scanner`

This session can read that repository but not push to it, so the fix ships
as patches. Both are small and reversible.

## What they fix

`avg_alpha_close_pct` is published on the public receipts as **+1.602%** for
`sec_top_gappers`. Over the investable universe it is **−0.149%**. The gap is
nineteen rows out of 13,760 — unadjusted reverse splits on sub-$1 stocks,
booked as returns:

```
NYMXF  $0.0002 -> $0.0200   +9900%
NFE    $0.3300 -> $12.7700  +3770%
ALP    $0.0990 -> $3.7100   +3647%
IPDN   $0.1200 -> $3.3300   +2675%
```

A reverse split multiplies the price and divides the share count. The
holder's value does not change, so none of that is return.

The published figure was arithmetically correct — it reproduces the raw rows
exactly. The arithmetic was never the problem; the input was.

## Apply

```bash
cd /path/to/scanner
git apply --check patches/0001-investable-alpha.patch   # dry run first
git apply patches/0001-investable-alpha.patch
git apply patches/0002-site-display.patch
```

`0001` needs two constants near the top of `evaluate_sec_outcomes.py`:

```python
PRICE_FLOOR = 1.00          # below this the quote is not investable
MAX_PLAUSIBLE_MOVE = 100.0  # a larger one-day move is a corporate action
```

## What changes, and what deliberately does not

`avg_alpha_close_pct` is **unchanged**. Rewriting it would break the
historical series and any consumer reading it, and silently restating a
published number is worse than correcting it in the open.

Three columns are added — `avg_alpha_close_pct_investable`,
`median_alpha_close_pct`, `excluded_rows` — and the site displays the
investable one, falling back to the raw mean for older summary files.

The median is worth publishing on its own merit: for a distribution where
1.6% of rows move the mean by 1.9 percentage points, the median is the
statistic that describes a typical pick.

## Already-published file

`fix_published_summary.py` recomputes a live `outcomes_summary.csv` without
waiting for the next pipeline run:

```bash
python3 patches/fix_published_summary.py \
    --outcomes /opt/catalyst/outcomes.csv \
    --summary  /opt/catalyst/outcomes_summary.csv
```

It prints a before/after table and only writes with `--write`.

## One command

```bash
./scripts/repair-published-alpha.sh            # dry run, changes nothing
./scripts/repair-published-alpha.sh --write    # apply it
```

Four steps with real ordering dependencies — fetch adjusted prices for the
affected tickers, repair the ledger, recompute the summary, audit — run in
order, stopping on the first failure. No step is wrapped in `|| true`: a
half-applied repair would leave the summary claiming a correction the ledger
does not support, which is worse than not starting.

It only fetches the 19 tickers with a move over 100%, not the whole board, so
it is a handful of API calls rather than hundreds. Dry run prints the whole
before/after and touches nothing.

Preflight refuses to start without a Tradier token or the ledger repo, and
says which is missing.

Then the two patches below, which touch the scanner repo and have to be
applied by hand.

## The root cause, and the actual repair

The price floor above is a guard. `backtest/split_repair.py` is the repair.

The bug is a units mismatch: `filing_day_close` is captured live before the
split and `next_close` after it, so the stored pair is quoted in two
different price regimes and their ratio is meaningless.

```bash
# best: read the truth off a split-adjusted series
python3 -m kronos_pipeline.ohlcv --from-convergence          # build the cache
python3 -m backtest.split_repair --data-repo ../sec-catalyst-data \
        --ohlcv-dir /opt/catalyst/ohlcv --write
```

Tradier's history endpoint returns split-adjusted prices, so both legs land
in the same post-split units and the corporate action cancels out by
construction. No ratio is inferred and nothing is assumed about the size of
the true return. Verified on a fixture: a ledger pair reading +5100% across a
split resolves to the real +4.0%.

Without the cache the tool falls back to the ledger's own evidence — a
reverse split is a *persistent* level shift, so a ticker observed on both
sides of it reveals its ratio:

```
NYMXF  $0.0002, $0.0002  ->  $0.02   exactly 100x, and it stays
       repaired to 0.00%, which is correct: a reverse split moves no value
```

That path repaired 4 of 39 on the current ledger. The other 35 are the last
observation of that ticker, so there is no "after" to compare against and the
tool reports them rather than guessing. **Run it with `--ohlcv-dir` on the
droplet and all 39 resolve** — that is the one-line difference between
detecting the problem and fixing it.
