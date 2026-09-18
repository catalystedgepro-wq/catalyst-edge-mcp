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

## Backtesting — and why it can only start now

The question "does Kronos add alpha over the convergence score" needs a
per-pick outcome record: which ticker was picked on which day, and what it
then did. **Catalyst Edge has never kept one.** `sec_outcome_summary.csv` is
aggregated per list (`list_name, rows, wins, losses, hit_rate_*, avg_alpha_*`)
with no per-pick rows, and `convergence_alerts.csv` is overwritten in place on
every run — so each day's board is destroyed by the next.

Confirm this on your own droplet rather than taking it on faith:

```bash
python3 -m backtest.inspect_data
```

It inventories every CSV under the data root, reports real headers, row
counts, value samples and date ranges, and states whether any file carries
both a ticker column and a date column. It assumes no schema — `--emit-mapping`
writes a stub with each guess and the evidence behind it, for you to correct.

### Start the clock

Past picks are unrecoverable. Evaluation data can only accumulate forward, so
this wants to be running today:

```bash
python3 -m backtest.snapshot          # after the scoring pipeline, daily
python3 -m backtest.snapshot --status # how many days have accumulated
```

It archives `convergence_alerts.csv`, `kronos_forecasts.csv` and
`orphan_sector_lean.csv` into `snapshots/<kind>/<YYYY-MM-DD>.csv`, keyed by
each source file's **mtime date**, not today's — a pipeline finishing at 23:50
and a snapshot at 00:05 still agree on which day the picks belong to. It is
idempotent and never overwrites without `--force`.

### Evaluate, once there is enough

```bash
python3 -m backtest.evaluate --horizon 5
```

Joins the snapshots to the OHLCV cache, writes `backtest_ledger.csv` (the
per-pick record that never existed), and ranks each predictor by quantile
bucket plus Spearman correlation against realized forward return.

**Entry timing matters more than the statistics.** Picks are ranked before the
open from data through the prior close, so entry is the pick date's OPEN and
exit is the close `--horizon` sessions later. Using the pick date's close as
entry would let a pick see the move it is being judged on — an easy way to
manufacture an edge that does not exist. If your pipeline actually publishes
intraday, `--entry close` is the honest setting and the default is wrong for
you.

`--horizon 5` is a choice, not a default worth trusting. Run 1, 5 and 20 and
see whether any apparent edge survives; one that appears at a single horizon
usually is not one.

### Do not read it too early

Below ~200 picks over 20+ days the tool prints a warning and you should
believe it. Bucket means at small n are dominated by noise, and a few percent
of spread is not evidence. `backtest/test_backtest.py` plants a known signal
in one predictor and pure noise in another and requires the evaluator to find
the first and reject the second — that is what keeps it from reporting edge
where there is none, but it cannot save you from reading 30 picks as a result.

### Only then, fine-tuning

Fine-tuning Kronos is the last step, not the next one. It needs a GPU the
droplet does not have, and until the backtest shows the base model
contributing something, there is no baseline to improve on and no way to tell
whether a fine-tune helped.

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
