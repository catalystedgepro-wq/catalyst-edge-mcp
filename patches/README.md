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

## Not fixed here

The root cause is that prices are not split-adjusted upstream. The price
floor is a guard, not a repair — it will keep excluding legitimate low-priced
picks along with the artifacts. The real fix is adjusting for corporate
actions when the OHLCV is fetched, which needs a split/dividend feed this
codebase does not currently pull.
