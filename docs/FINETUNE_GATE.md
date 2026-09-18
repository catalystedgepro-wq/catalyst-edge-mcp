# Fine-tune decision gate

Whether to fine-tune Kronos on the Catalyst Edge universe. Written down in
advance, because a criterion invented after seeing the numbers is not a
criterion.

**Target date: 2026-12-14** — 60 trading days from 2026-09-18, assuming
`backtest.snapshot` starts running immediately. Every day it does not run
pushes this out one day; the clock measures snapshots, not calendar time.

## Step 0 — is the clock even running

```bash
python3 -m backtest.snapshot --status
```

If `convergence.days` is 0 or has not grown since the last check, **nothing
below applies** and the only action is to fix the archiver. Past picks cannot
be reconstructed, so a stalled archiver is not a delay, it is permanent data
loss. `runner.sh` fails when the newest snapshot is more than 3 days old.

## Step 1 — enough data

```bash
python3 -m backtest.evaluate --horizon 5
```

Required before reading anything else:

- **≥ 200 picks** and **≥ 20 distinct pick dates**

Below this the evaluator prints a warning and it should be believed. Bucket
means at small n are dominated by noise; a spread of a few percent is not
evidence of anything.

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
