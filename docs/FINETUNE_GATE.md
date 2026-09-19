# Fine-tune decision gate

Whether to fine-tune Kronos on the Catalyst Edge universe. Written down in
advance, because a criterion invented after seeing the numbers is not a
criterion.

**The December 2026 target date is superseded.** It assumed no per-pick
outcome record existed and that evaluation data had to accumulate forward.
That was wrong: `catalystedgepro-wq/sec-catalyst-data` carries ~28.7k scored
picks with realized outcomes over 42 pick dates. Steps 1-3 below run today.

## RESULT — Numerai contradicts the internal ledger (2026-09-19)

Run on the droplet against the live API, model `catalystedge`:

```
1,191 raw records · 76 scored rounds · metric corr20V2
mean correlation  +0.00954
t vs zero         +7.20
positive rounds   57/76  (75%)
```

**Reliably positive.** The prediction recorded here — mean correlation at or
below zero, following the internal ledger's finding that convergence_score is
anti-predictive — was wrong. Inverting the signal, which that finding pointed
towards, would have destroyed a working edge.

### Correct the t before quoting it

Signals scores a 20-day forward window and submissions are weekly, so
consecutive rounds share roughly 15 of 20 days of return. The 76 observations
are not independent and t=7.20 is inflated:

| assumed overlap | effective n | corrected t |
|---|---|---|
| 2x | 38 | 5.09 |
| 3x | 25 | 4.16 |
| 4x (most conservative) | 19 | 3.60 |

It clears t=2 under every correction, so the signal is real. Per-round Sharpe
is 0.83; the naive annualisation of 6.0 is not credible, ~3.0 is closer.
Sign consistency alone is decisive: 57/76 positive has probability 7.4e-06
under a fair coin.

### Reconciling the two results

They are not actually in conflict. They measure different things:

| | internal ledger | Numerai |
|---|---|---|
| horizon | next day | 20 days |
| returns | raw | neutralised against risk factors |
| universe | ~1,900 US tickers | Signals universe |
| fat tails | dominate (one +3,770% name) | ranked, so bounded |

The internal finding stands where it was measured: on **raw next-day** moves
the score does not separate, and the short/RegSHO cohort it concentrates at
the top genuinely underperforms. The Numerai result says that once returns
are ranked and factor-neutralised over 20 days, what is left does carry
signal. A score can be poor for next-day raw moves and good for medium-horizon
residual returns — those are different questions, and the answer is different.

### What this does NOT establish

The submission is the percentile rank of convergence_score **plus a ±0.10 DCF
grade tilt**. The +0.00954 validates that combination, not convergence_score
alone. The DCF component could be carrying all of it.

Isolating it is cheap and is the obvious next step: Numerai allows multiple
models. Submit a second model with the rank only and no DCF tilt, run both for
a quarter, and compare. Until that is done, do not attribute the edge to
either component.

## Step 0 — run the baseline first


```bash
git clone https://github.com/catalystedgepro-wq/sec-catalyst-data ../sec-catalyst-data
python3 -m backtest.evaluate --data-repo ../sec-catalyst-data
python3 -m backtest.evaluate --data-repo ../sec-catalyst-data \
    --join-predictor convergence_score
```

As of 2026-09-18 neither `base_score` nor `convergence_score` separates
outcomes on any list or any exit rule: every |rho| < 0.11, and the two
quintile spreads reaching |t| >= 2 are exactly what 20 tests produce by
chance. **This matters more than anything about Kronos.** If the existing
score has no measurable edge, then "does Kronos add alpha over it" is the
wrong question — there is no established baseline to add to, and a Kronos
signal would be the whole edge rather than an increment.

Re-run this before anything below. If the baseline is genuinely flat, decide
whether to fix the existing score first; fine-tuning a second model to overlay
on a flat one is an expensive way to avoid that question.

## Step 1 — enough data

Already satisfied for the existing scores (28,667 picks, 42 dates). For
Kronos it is not: the ledger has no Kronos column. Either
- accumulate `kronos_forecasts.csv` snapshots forward (clean, slow), or
- backfill by scoring each historical pick date from price history up to that
  date (fast, but a pretrained forecaster may have seen the period in
  training, which flatters it).

Do the backfill for a fast read and the forward test to confirm it.

## Step 2 — does the base model show any signal at all

Run all three horizons:

```bash
for h in 1 5 20; do python3 -m backtest.evaluate --horizon $h --json; done
```

Look at `kronos_p_up`'s `spearman_vs_fwd_return`:

| Observation | Reading |
|---|---|
| \|ρ\| < 0.05 at every horizon | No signal. Fine-tuning a model showing nothing on this universe is a long shot — **stop**. |
| ρ > 0.05 at one horizon only | Almost certainly noise found by looking three times — **stop**. |
| ρ > 0.05 consistent in sign across horizons | Something is there. Continue to step 3. |

A *negative* consistent ρ is also a finding — the model is anti-predictive
here, and that is worth understanding before spending a GPU on it.

## Step 3 — the question that actually decides it

From the same output, read `incremental`:

```
incremental_pct   the return gap between high and low p_up picks,
                  measured WITHIN bands of equal convergence score
t_stat            that gap against its own standard error
```

This is stratified deliberately. An unstratified split of the top slice would
credit Kronos for the convergence score's own skill whenever the two are
correlated. Inside a narrow convergence band the base is near-constant, so
anything `p_up` separates there is information the base does not carry.

| `t_stat` | Reading |
|---|---|
| \|t\| < 2 | Kronos adds nothing over the existing score. **Do not fine-tune** — improving a signal that is redundant still leaves it redundant. |
| t > 2 | Kronos carries information the 500-engine score misses. **Fine-tuning is justified**: there is a real effect to sharpen. |
| t < −2 | Inverse signal. Do not fine-tune; investigate. Possibly an entry-timing or alignment bug rather than a real effect. |

`backtest/test_backtest.py` validates this on synthetic data: it rejects pure
noise and an overlay that is a function of the base, and credits one carrying
a genuinely new driver.

## Step 4 — only if steps 1–3 pass

Fine-tuning needs infrastructure this project does not have:

- **A GPU.** The droplet is CPU-only. This is a Colab / rented-box job.
- **A training corpus.** `finetune_csv/` wants OHLCV across the universe over
  years. The `ohlcv/` cache holds 600 bars per ticker for today's board only —
  enough to score, nowhere near enough to train.
- **A held-out period.** Fine-tuning on the same window the gate was measured
  on will report an improvement that does not exist. Reserve the most recent
  ~20% of dates and never let the training set see them.

Re-run steps 2–3 against the fine-tuned model on the held-out period. If the
incremental `t_stat` does not improve on the base model's, the fine-tune
failed regardless of what its training loss did.

## What "verified" means here

Nothing in this document has been run against real data — there is none yet.
The tooling is tested on synthetic fixtures with known answers, which
establishes that it measures what it claims to measure. It does **not**
establish that Kronos has an edge on this universe. That is what the
2026-12-14 check is for, and "no edge" is a perfectly good outcome that saves
the cost of step 4.
