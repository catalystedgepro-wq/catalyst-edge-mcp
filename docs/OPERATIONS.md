# Operations

Runbook for the hosted Catalyst Edge MCP server. Every routine action is one
command; the scripts live in [`scripts/`](../scripts).

## Layout on the droplet

```
/opt/catalyst/                     DATA_ROOT — the tools read snapshots here
├── convergence_alerts.csv         get_convergence_picks, get_ticker_signal
├── orphan_sector_lean.csv         get_sector_lean
├── sec_outcome_summary.csv        get_track_record
├── docs/data/theses.json          get_thesis
├── .env                           TRADIER_TOKEN (gitignored, never deployed)
├── releases/<ts>/                 deploy snapshots, last 10 kept
├── runner-reports/<date>.md       runner output
└── mcp_server/                    this repo, deployed
    ├── catalyst_mcp.py
    ├── mcp_keys.json              key→tier map (gitignored, never deployed)
    └── scripts/
```

The service binds `127.0.0.1:8848`; nginx terminates TLS and proxies
`https://catalystedgescanner.com/mcp/` to it.

## Deploy

```bash
./scripts/deploy.sh              # ship HEAD, restart, verify, auto-rollback
DRY_RUN=1 ./scripts/deploy.sh    # show exactly what would be sent
```

Ships only git-tracked files at `HEAD`. `mcp_keys.json` and `.env` on the
droplet are excluded from the tarball and never overwritten — clobbering the
first would drop every customer API key. The release is byte-compiled before
the restart, so a syntax error fails the deploy instead of taking the service
down, and a failed post-restart health check restores the previous snapshot
automatically.

Before changing `deploy.sh`, run its test — it exercises the droplet-side
block against a sandbox with stubbed `systemctl`/`curl`, so the paths that
could lose customer keys or leave a broken release running are covered
without touching production:

```bash
./scripts/test-deploy-logic.sh
```

Rollback restores the files the previous release contained. A file *added* by
the new release stays (harmless — nothing imports it); to remove it, deploy
an older commit.

## Logs

```bash
./scripts/pull-logs.sh                    # everything new since the last pull
SINCE='2 hours ago' ./scripts/pull-logs.sh
FOLLOW=1 ./scripts/pull-logs.sh           # live stream
```

Incremental pulls use a journald cursor in `logs/.cursor`, so repeated runs
never duplicate lines. Output lands in `logs/` (gitignored).

## Registry publish

```bash
CHECK_ONLY=1 ./scripts/publish-registry.sh   # preflight, publishes nothing
./scripts/publish-registry.sh                # preflight, then publish
```

Runs on the droplet by default: the `com.catalystedgescanner/*` namespace is
authenticated by proving control of `catalystedgescanner.com`, which the
droplet serves. `LOCAL=1` runs the publisher on your machine instead.

The preflight refuses to publish when:

- `server.json` is malformed
- `server.json`'s `version` disagrees with `SERVER_VERSION` in `catalyst_mcp.py`
- that version is already in the registry (the registry rejects duplicates)
- the `remotes[].url` in the listing does not answer `tools/list`

**Bump both `server.json` and `SERVER_VERSION` together.** They are separately
enforced by the preflight and by `smoke_test.py`.

If your namespace was authenticated some way other than DNS, change
`PUBLISH_LOGIN_CMD` at the top of the script — that is the only line that
encodes the login method.

## Scheduled runner

[`scripts/runner.sh`](../scripts/runner.sh) checks unit state, the health
endpoint, data-snapshot freshness, and new journal errors. It is read-only:
it does not deploy, publish, restart anything, call an LLM, or make any
outbound request. Exit 0 = green, exit 1 = a problem on stdout.

**Run it on the droplet, not from a laptop over ssh.** Droplet-side it needs
no ssh key, no permission grant, and keeps working when your laptop is shut.

### systemd timer (preferred — the box already uses systemd)

```bash
cp /opt/catalyst/mcp_server/catalyst-runner.service /etc/systemd/system/
cp /opt/catalyst/mcp_server/catalyst-runner.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now catalyst-runner.timer
systemctl list-timers catalyst-runner.timer   # confirm the next firing
```

Check on it later:

```bash
journalctl -u catalyst-runner.service --since today
cat /opt/catalyst/runner-reports/$(date -u +%F).md
```

Timer over cron because it inherits the unit's sandboxing, `Persistent=true`
catches up a window missed across a reboot, and the output lands in the
journal next to the service's own.

### cron (alternative)

`crontab -e` on the droplet, then:

```cron
*/30 * * * * QUIET=1 CATALYST_ROOT=/opt/catalyst /opt/catalyst/mcp_server/scripts/runner.sh >> /var/log/catalyst-runner.log 2>&1
```

`QUIET=1` means cron only mails you when a check fails.

## Kronos forecast pipeline

Two offline stages feed `get_price_forecast`. Both run in the nightly pipeline;
neither is ever imported by the MCP server.

```bash
# 1. refresh the OHLCV cache (stdlib only — no torch needed)
python3 -m kronos_pipeline.ohlcv --from-convergence

# 2. score it (needs torch + a Kronos checkout)
python3 -m kronos_pipeline.score --from-convergence --kronos-src /opt/kronos

# 3. archive the day, so the board can be evaluated later (stdlib only)
python3 -m backtest.snapshot
```

Step 3 is not optional if you ever intend to measure whether any of this
works — see [Backtesting](#backtesting--and-why-it-can-only-start-now).

Stage 1 writes `/opt/catalyst/ohlcv/<TICKER>.csv`, retaining 600 bars and
re-pulling the last 5 sessions each run (exchanges restate volume after close).
Stage 2 writes `/opt/catalyst/kronos_forecasts.csv`, which the MCP server reads
with `_read_csv()` and dates from its mtime, exactly like every other snapshot.
Both write atomically via a `.tmp` rename, so a reader never sees a half-file.

### Setup, once

```bash
git clone https://github.com/shiyu-coder/Kronos /opt/kronos
pip install -r /opt/catalyst/mcp_server/kronos_pipeline/requirements.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu
```

The CPU index is not optional on a droplet: a plain `pip install torch` pulls
~5.5GB of CUDA libraries that a CPU-only box cannot use.

Weights come from Hugging Face on first run (`NeoQuasar/Kronos-small` +
`NeoQuasar/Kronos-Tokenizer-base`, ~25M params). If the host cannot reach
huggingface.co, download them elsewhere and pass local directories to
`--model` / `--tokenizer`.

### Cost, and the knob that drives it

`--samples` is the runtime lever. Kronos is generative, and
`predict(sample_count=N)` averages the paths internally — throwing away the
distribution. So the scorer passes the same window as N separate series to
`predict_batch`, getting N independent paths in one batched pass, and reports
`p_up` from their spread. Runtime scales linearly in `--samples` and in
universe size. Start at `--samples 20` and measure before raising it.

`Kronos-small` (25M) is the sane default on CPU. `Kronos-base` (102M) is 4x the
compute for accuracy you have not yet shown you need — see the backtest note
below.

### Watch `invariant_repairs`

Kronos samples OHLC from a discrete codebook with nothing enforcing
`high >= max(open,close) >= min(open,close) >= low`, so a sampled bar can have
its low above its high. `score.py` clamps these and counts them per ticker;
`runner.sh` sums the column and reports it. A persistently nonzero count means
the model is producing incoherent bars — worth investigating before trusting
the forecasts, not something to leave running.

### Not yet validated

No backtest has established that these forecasts add alpha over the existing
convergence score. `sec_outcome_summary.csv` is aggregated per list
(`list_name, rows, wins, losses, hit_rate_*, avg_alpha_*`) with no per-pick
rows, so there is nothing to join a forecast against. Until a per-pick outcome
ledger exists, treat `get_price_forecast` as an independent signal on offer,
not as a validated edge — and do not fine-tune against a baseline you cannot
measure.

## Backtesting

A per-pick outcome ledger already exists, in
[catalystedgepro-wq/sec-catalyst-data](https://github.com/catalystedgepro-wq/sec-catalyst-data):
`snapshots/<date>/outcomes.csv`, cumulative, ~28.7k picks over 42 pick dates
with `alpha_close_pct` already SPY-adjusted and execution cost modelled. No
waiting is required — the backtest runs today.

```bash
git clone https://github.com/catalystedgepro-wq/sec-catalyst-data ../sec-catalyst-data

# the score carried inside the ledger
python3 -m backtest.evaluate --data-repo ../sec-catalyst-data

# a predictor held in dated files beside it
python3 -m backtest.evaluate --data-repo ../sec-catalyst-data \
    --join-predictor convergence_score

# does one add anything on top of the other
python3 -m backtest.evaluate --data-repo ../sec-catalyst-data \
    --incremental base_score convergence_score
```

Any predictor joins the ledger on `(list_date, ticker)`, so Kronos forecasts
are evaluated the same way once `kronos_forecasts.csv` snapshots accumulate —
or once they are backfilled, since Kronos needs only price history up to each
pick date.

### Three things the evaluator refuses to do quietly

**It does not pool lists.** `base_score` spans 15..27 on `sec_clean_gappers`
and -11..27 on `sec_top_gappers`. A pooled rank correlation across
incompatible scales measures the mix of lists, not the score. Per-list is the
default; `--pooled` prints a warning beside the number.

**It reports list nesting.** `sec_top_gappers` (13,760 picks) *is* the
universe — its pick set equals the union of all six lists, and every other
list is a nested subset. Six lists are one universe plus five filtered views,
so per-list results re-examine largely the same picks.

**It counts hypothesis tests.** Every quintile spread is one test. The run
prints how many were made and how many chance alone would be expected to
produce, because 2 hits at |t| >= 2 across 20 tests is noise, not a finding.

### Horizons

Every outcome column in the ledger is **next-day**. The horizon axis is
therefore the exit rule inside day one — `gap_next_open_pct`,
`alpha_close_pct`, `next_day_vwap_pct`, `realistic_pnl_net_pct` (a +2%/-1.5%
bracket net of cost) — not a multi-day hold. Multi-day holds would need price
history the ledger does not carry.

### Subgroup cuts

`backtest/cuts.py` asks a different question from `evaluate.py`: not "does
this score rank picks" but "does any subgroup behave differently".

```bash
python3 -m backtest.cuts --data-repo ../sec-catalyst-data --conviction
python3 -m backtest.cuts --data-repo ../sec-catalyst-data --signals
python3 -m backtest.cuts --data-repo ../sec-catalyst-data --hold 5 \
    --hold-signals finra_short regsho ftd gap finra_regsho
```

Subgroup analysis fails in three specific ways, and all three were hit for
real on this ledger, so the tool defends against each:

**Confounding by date.** The conviction scheme changed mid-period — August
boards are ~90% AVOID, September ~80% WATCH, and ELEVATED barely existed
before September. Unblocked, `ELEVATED` looked significant at t = -2.92.
Compared only against picks from the *same pick date* it is t = -0.76: the
entire effect was the calendar. Every subgroup test is date-blocked.

**Right-skew.** `alpha_close_pct` has mean +4.3% and median -0.3% in the same
cohort — a few enormous winners drag every mean positive while the typical
pick loses. Tests are therefore Mann-Whitney within each date, pooled (van
Elteren), reporting medians. Switching from means to ranks cut the
"significant" signal count from 35 to 16.

**The search itself.** 68 signal tokens and ~900 `*_pts` columns; at p < 0.05
roughly 45 come back significant on pure noise. Every family prints its
Bonferroni-corrected bar, the uncorrected count, and how many hits chance
alone predicts.

Five signals fire on >=99% of picks (`auto_macro_crypto_fear_greed`,
`auto_macro_eia_petroleum`, `auto_macro_options_gex`,
`auto_macro_term_premium`, `auto_macro_wiki_pageviews`). They cannot
discriminate and only inflate the score; the tool lists and excludes them.

### The one finding that survived

Picks firing the short / Reg SHO family — `finra_short`, `regsho`,
`auto_regsho_threshold`, `finra_regsho`, `ftd`, `gap`, `pill` — **underperform
systematically**. These overlap heavily (`regsho` and `auto_regsho_threshold`
are identical sets; `finra_regsho` and `finra_short` overlap 95%), so it is
one effect, not seven.

| hold | median, family | median, rest | z | n |
|---|---|---|---|---|
| 1 day | -0.66% | -0.10% | -6.82 | 4071 |
| 5 days | -4.47% | +0.00% | -7.62 | 626 |
| 10 days | -4.03% | +0.00% | -5.13 | 488 |

It holds on all four next-day exit rules (z -6.3 to -12.1), survives
Bonferroni, survives date-blocking, and grows with holding period. The 5d and
10d rows come from the reconstructed-hold subset and carry its selection
bias; the 1d row does not.

If the scanner treats these as bullish squeeze setups, the data says the
opposite over every horizon measurable here. That is worth acting on before
anything is fine-tuned.

### Quantifying it: exclude vs invert

```bash
python3 -m backtest.portfolio --data-repo ../sec-catalyst-data
python3 -m backtest.portfolio --data-repo ../sec-catalyst-data --top 25 --borrow-bps 50
```

Each pick date forms an equal-weight basket; variants are compared to the
baseline paired by day (Wilcoxon signed-rank), so both always face the same
tape.

**The score concentrates the bad cohort.** Share of the board that is short /
Reg SHO, by score rank: top 10 **70.6%**, top 25 66.0%, top 50 60.8%, top 100
52.8%, whole board 35.7%. The higher the convergence score, the likelier the
pick is in the cohort that underperforms.

Top 25 by score, 32 days, net of the ledger's execution cost:

| strategy | mean/day | median/day | win days | compounded |
|---|---|---|---|---|
| long all (baseline) | −1.69% | −1.52% | 22% | **−42.3%** |
| exclude cohort | −0.94% | −0.65% | 38% | −28.2% |
| invert cohort | +0.70% | +0.77% | 62% | +24.4% |
| short cohort only | +1.52% | +1.19% | 78% | +60.9% |

The tradeable top of the board lost money over this window, and removing the
cohort roughly halves the loss without needing to borrow anything.

### The weight is deliberate, and it is large

`build_convergence_score.py` in `catalyst-edge-os/scanner` awards these
points on an explicit "squeeze fuel" thesis — the code comments say so:

```
# --- RegSHO threshold list (FTD persistence = short squeeze fuel) ---   +5
# --- FINRA short volume (short ratio = squeeze fuel) ---              +1/3/5
# --- Short interest data (squeeze setup fuel) ---                     +2/5/8
# --- SEC FTD (fails-to-deliver persistence = squeeze fuel) ---        +1/3/5
# --- FINRA Reg SHO ---                                                  +2/3
```

Up to **+26 points** on a board whose median score is 6 and maximum is 53.
That is why 70% of the top 10 is this cohort: the family can carry a pick to
the top of the board on its own.

The thesis is coherent — high short interest plus delivery failures is the
textbook squeeze setup. The outcomes in this ledger simply do not support it
at any horizon measured.

### Zeroing the weight beats filtering the picks

```bash
python3 -m backtest.portfolio --data-repo ../sec-catalyst-data --top 25 --reweight
python3 -m backtest.portfolio --data-repo ../sec-catalyst-data --sweep
```

`--reweight` subtracts the family's `*_pts` from the score and reranks, which
is a different intervention from dropping the picks: a name still competes on
its other signals, and everything the family had been outranking moves up.

Top 25, 32 days:

| | cohort share | mean/day | median/day | win days | compounded |
|---|---|---|---|---|---|
| published score | 65.9% | −1.69% | −1.52% | 22% | −42.3% |
| filter cohort out | — | −0.94% | −0.65% | 38% | −28.2% |
| **zero the weight** | **39.0%** | **−0.65%** | **−0.29%** | **44%** | **−19.2%** |

Rescoring beats filtering on every column, and once it is applied the further
variants stop helping — excluding the residual cohort makes compounding
slightly *worse* (−21.9%). Most of the harm is in the weighting, not in the
names.

Paired by day, rescored vs published: top 10 −42.5% → −9.9% (z=+2.53),
top 25 −42.3% → −19.2% (z=+3.19), top 50 +59.7% → +36.2% (z=+1.89, noise).
The gain is concentrated where the family dominates the ranking.

**It is an improvement, not a fix.** The rescored top 25 still loses 19% over
32 days.

### The ranking is anti-predictive

`--sweep` compares basket depths:

| basket | cohort share | median/day | win days | compounded |
|---|---|---|---|---|
| top 10 | 69.4% | −0.91% | 28% | −42.5% |
| top 25 | 65.9% | −1.52% | 22% | −42.3% |
| top 50 | 60.9% | −1.12% | 16% | +59.7% |
| top 100 | 52.9% | −0.96% | 12% | +111.9% |
| all | 35.7% | −0.39% | 28% | +133.7% |

Median daily return is negative at **every** depth — the typical day loses
money whatever the basket. As cohort share falls, the median improves
monotonically. Compounding rises with depth for a different reason: a wider
net catches more of the right tail, and that column is a handful of names
rather than a strategy.

### Why the short is not the answer

On the **full** board the same short returns **−100.9% compounded** with a
worst day of −102%, while still winning 72% of days. The cohort contains a
single position that gained **+3,770%** in a day, and ten that gained over
100%. One such name at 0.8% weight costs a short book 29.7% in a session.

The top-25 cohort happens to contain nothing above +25% across these 32 days.
That is a property of a small window, not a safety guarantee — the cohort
demonstrably produces +3,770% moves, and one landing in the top 25 erases
years of the drift. `--borrow-bps` shows the rest: the full-board short is
already gone at 50bps/day, and Reg SHO threshold listing is by construction a
hard-to-borrow marker that this ledger's flat `exec_cost_pct` does not price.

**A median-based test measures the typical day; a short book is killed by the
tail.** The same skew that makes rank tests the right way to *detect* this
effect makes them the wrong way to *size* a short against it. The simulator
prints the right-tail distribution on every run for that reason.

Exclusion is the change that needs no borrow, has no unbounded downside, and
is testable against 42 dates of real outcomes today.

### Reconstructing longer holds

The ledger carries one forward day per row. A longer hold is recoverable only
where the same ticker reappears on a later pick date, supplying another dated
close — about 19% of picks at 5 days, 14% at 10.

**That subset is self-selected**: a ticker reappears because it keeps firing
signals. Treat reconstructed holds as a hypothesis generator, never as a
backtest. `--hold N` prints coverage and this warning on every run.

### Forward accumulation still matters

`backtest.snapshot` remains worth running nightly: the published ledger has no
Kronos column, so forward snapshots of `kronos_forecasts.csv` are how a clean,
out-of-sample Kronos evaluation gets built. A retrospective backfill is faster
but a pretrained forecaster may have seen the period in training, which
flatters it.

## Numerai: collect the scoring, permanently

`build_numerai_signals.py` submits the **percentile rank of
convergence_score** (plus a ±0.10 DCF tilt) to Numerai Signals, weekly.
Numerai has been grading that exact score since at least round 1253
(2026-04-30) — out-of-sample, on 20-day forward returns, neutralised against
its own risk factors, by a party with no reason to flatter it.

None of it was ever collected. `submit_numerai.py` writes a receipt saying a
submission was accepted; no file anywhere recorded how it scored. That is six
months of the best evidence in this project, lost by default.

It is a better referee than any internal backtest:

| | internal ledger | Numerai |
|---|---|---|
| sample | 32 pick dates | ~20+ resolved rounds |
| horizon | next day | 20 days forward |
| returns | raw, self-computed | neutralised against their factors |
| scored by | our own join logic | them |

### Deploy it once

```bash
# on the box that already submits — the API must be reachable from there
sudo cp numerai_pipeline/catalyst-numerai.{service,timer} /etc/systemd/system/
sudo sed -i 's/REPLACE_WITH_YOUR_MODEL/<your model name>/' \
    /etc/systemd/system/catalyst-numerai.service
sudo systemctl daemon-reload
sudo systemctl enable --now catalyst-numerai.timer
sudo systemctl start catalyst-numerai.service     # backfill everything now
python3 -m numerai_pipeline.collect --status-only  # confirm
```

Every run fetches the **full** round history and upserts into
`/opt/catalyst/numerai/rounds.csv`. A missed day heals itself. A round that
resolves later is updated in place. `pip install numerapi` is optional but
recommended — it tracks Numerai's schema changes.

If you would rather run it in the existing GitHub Actions pipeline, add it
after the submit step — **without** the `|| echo` the other steps use:

```yaml
- name: Collect Numerai scores
  env:
    NUMERAI_MODELS: <your model name>
    CATALYST_DATA_ROOT: ${{ github.workspace }}
  run: python3 -m numerai_pipeline.collect
```

### Three rules it enforces

**Never lose history.** The CSV is read, merged and written through a temp
file and an atomic rename. A failed fetch leaves the previous file untouched
and exits 1 — it never truncates. One model failing does not touch another's
rows.

**Never silently succeed.** Every social step in the existing daily pipeline
is `python3 x.py || echo "x failed"`, which is exactly how six months went
unnoticed. This exits non-zero, writes the reason to `numerai/status.json`,
and `runner.sh` fails when the file is more than 48h old or when status.json
names a failed model. Do not wrap it in `|| echo`.

**Never assume a schema.** Numerai has renamed the Signals correlation field
repeatedly (`corr20`, `corr20V2`, `corrV4`, …). The field actually read is
stored in every row as `corr_metric`, and an unrecognised field yields blank
rather than a fabricated `0.0`, so a rename appears in the data instead of
quietly becoming a flat line of zeros.

### Then read it

```bash
python3 -m backtest.numerai_scores --from-json <(python3 - <<'EOF'
import csv, json; print(json.dumps([dict(r) for r in csv.DictReader(open("/opt/catalyst/numerai/rounds.csv"))]))
EOF
)
```

The prediction to write down first: the internal ledger found
`convergence_score` anti-predictive, and the submission ranks **bullish** by
it, so mean correlation should come back at or below zero. Negative and
consistent confirms the finding on six times the data and the fix is a
one-line sign flip in `load_convergence_rank()`. Indistinguishable from zero
matches "no separation at any horizon". Reliably positive contradicts the
ledger — and since Numerai scores 20 days out after neutralisation while the
ledger measures raw next-day moves, that disagreement would be worth more
than either result alone.

## Usage logging — the gap

`get_convergence_picks` and `get_track_record` are open on the free tier, so
anyone can call the server without a key. **Nothing about those calls is
recorded.** `run_http()` sets `Handler.log_message` to a no-op, which
suppresses the built-in access log, and no per-request line is emitted
anywhere else. The journal holds startup lines and crashes only.

So there is currently nothing to mine for leads: `pull-logs.sh` will pull a
near-empty journal and `runner.sh` will report that usage cannot be measured.
Fixing it means adding a deliberate request log to `run_http()`.

If you add one, two things matter:

- **Never log the Bearer token.** Log a short digest of it
  (`hashlib.sha256(key).hexdigest()[:12]`) so you can count distinct callers
  and correlate with `mcp_keys.json` without the journal becoming a file of
  live credentials.
- Decide what you retain about anonymous free-tier callers. IP + user-agent +
  tool name is enough to spot an evaluating team; it is also personal data
  with a retention question attached.

That change is not in this commit — it is a product decision, not a fix.

## ssh permissions in Claude Code

The scripts do not make an ssh grant narrower. Allowing
`Bash(./scripts/deploy.sh:*)` allows whatever that script does, and the script
is editable — it is a *broader* grant than it looks, not a tighter one.

What they do give you is one reviewable command per operation, with the
preflight and rollback logic in version control where it can be read and
diffed, instead of being improvised per session. Grant whatever ssh access you
are comfortable granting, on its own merits.

The scheduled runner needs no grant at all, because it runs on the droplet.
