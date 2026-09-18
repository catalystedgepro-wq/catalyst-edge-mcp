#!/usr/bin/env python3
"""test_pipeline.py — cover the Kronos integration without weights or a droplet.

  python3 kronos_pipeline/test_pipeline.py

The MCP-tool half is stdlib-only and always runs. The scorer half needs torch
and a Kronos checkout; it is skipped (not failed) when either is absent, so
this stays runnable on the serving droplet.

  KRONOS_SRC=/opt/kronos python3 kronos_pipeline/test_pipeline.py   # both halves
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FAILURES: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok  {msg}")


def bad(msg: str) -> None:
    print(f"  !!  {msg}")
    FAILURES.append(msg)


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else bad(msg)


# ── the MCP tool, over the real protocol ─────────────────────────────────────

SAMPLE = [
    dict(ticker="AAPL", as_of_bar="2026-09-17", horizon_days=5, last_close=230.1,
         pred_close=234.8, pred_return_pct=2.04, p_up=0.75, ret_p10_pct=-1.2,
         ret_p90_pct=5.4, samples=20, invariant_repairs=0, model="test",
         generated_at="2026-09-18T04:00:00Z"),
    dict(ticker="NVDA", as_of_bar="2026-09-17", horizon_days=5, last_close=880.0,
         pred_close=871.2, pred_return_pct=-1.0, p_up=0.35, ret_p10_pct=-6.1,
         ret_p90_pct=4.0, samples=20, invariant_repairs=1, model="test",
         generated_at="2026-09-18T04:00:00Z"),
]


def rpc(data_root: Path, args: dict, key: str = "") -> dict:
    env = dict(os.environ)
    env["CATALYST_DATA_ROOT"] = str(data_root)
    env["CATALYST_MCP_KEY"] = key
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "get_price_forecast", "arguments": args}}
    r = subprocess.run([sys.executable, str(REPO / "catalyst_mcp.py")],
                       input=json.dumps(msg) + "\n", capture_output=True,
                       text=True, timeout=30, env=env)
    lines = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    return lines[0].get("result", {}) if lines else {}


def body_of(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_tool() -> None:
    print("get_price_forecast (MCP protocol)")
    root = Path(tempfile.mkdtemp())
    with (root / "kronos_forecasts.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(SAMPLE[0]))
        w.writeheader()
        w.writerows(SAMPLE)

    check(rpc(root, {}).get("isError") is True,
          "free tier is gated")

    keyfile = REPO / "mcp_keys.json"
    if keyfile.exists():
        print("  --  tier-gated cases skipped (mcp_keys.json already present)")
        return
    keyfile.write_text(json.dumps({"keys": {"t": "intelligence"}}))
    try:
        res = rpc(root, {}, key="t")
        if res.get("isError"):
            bad(f"board query errored: {res}")
        else:
            b = body_of(res)
            check([f["ticker"] for f in b["forecasts"]] == ["AAPL", "NVDA"],
                  "board is sorted by p_up descending")
            check(bool(b.get("as_of")), "as_of reported from file mtime")

        res = rpc(root, {"ticker": "nvda"}, key="t")
        check(not res.get("isError") and body_of(res)["forecasts"][0]["p_up"] == 0.35,
              "single ticker resolves, input case-insensitive")

        check(rpc(root, {"ticker": "ZZZZ"}, key="t").get("isError") is True,
              "unknown ticker gives a clean error")
    finally:
        keyfile.unlink(missing_ok=True)

    (root / "kronos_forecasts.csv").unlink()
    check(rpc(root, {}, key="t").get("isError") is True,
          "missing forecasts file gives a clean error, not a traceback")


# ── the scorer, against real Kronos classes with random weights ──────────────

def test_scorer() -> None:
    print("scorer (random weights)")
    src = os.environ.get("KRONOS_SRC", "/opt/kronos")
    try:
        import numpy as np
        import pandas as pd
        import torch
        sys.path.insert(0, src)
        from model import Kronos, KronosPredictor, KronosTokenizer
    except ImportError as e:
        print(f"  --  skipped: {e} (set KRONOS_SRC, pip install -r requirements.txt)")
        return

    root = Path(tempfile.mkdtemp())
    os.environ["CATALYST_DATA_ROOT"] = str(root)
    sys.path.insert(0, str(REPO))
    from kronos_pipeline import score

    broken = pd.DataFrame({"open": [100., 10.], "high": [99., 10.5],
                           "low": [101., 9.5], "close": [100.5, 10.]})
    fixed, n = score.repair_ohlc(broken)
    check(n == 1, "repair_ohlc counts exactly the impossible bar")
    check((fixed["high"] >= fixed[["open", "close"]].max(axis=1)).all()
          and (fixed["low"] <= fixed[["open", "close"]].min(axis=1)).all(),
          "repair_ohlc restores the high/low invariant")
    check(fixed.loc[1].equals(broken.loc[1]), "repair_ohlc leaves a valid bar alone")

    (root / "ohlcv").mkdir(parents=True)
    rng = np.random.default_rng(7)
    close = 50 + np.cumsum(rng.normal(0, .8, 300))
    pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=300).strftime("%Y-%m-%d"),
                  "open": close, "high": close + 1, "low": close - 1,
                  "close": close, "volume": rng.integers(1e5, 9e5, 300).astype(float)}
                 ).to_csv(root / "ohlcv/TEST.csv", index=False)
    df, stamps = score.load_window("TEST", 64)
    check(list(df.columns) == ["open", "high", "low", "close", "volume"] and len(df) == 64,
          "load_window returns a Kronos-shaped window")

    torch.manual_seed(0)
    tok = KronosTokenizer(d_in=6, d_model=32, n_heads=2, ff_dim=64, n_enc_layers=1,
                          n_dec_layers=1, ffn_dropout_p=0., attn_dropout_p=0.,
                          resid_dropout_p=0., s1_bits=4, s2_bits=4, beta=1.,
                          gamma0=1., gamma=1., zeta=1., group_size=2)
    mdl = Kronos(s1_bits=4, s2_bits=4, n_layers=1, d_model=32, n_heads=2, ff_dim=64,
                 ffn_dropout_p=0., attn_dropout_p=0., resid_dropout_p=0.,
                 token_dropout_p=0., learn_te=True)
    pred = KronosPredictor(mdl, tok, device="cpu", max_context=512)

    res = score.forecast(pred, df, stamps, horizon=5, samples=8, temperature=1.0)
    check(0.0 <= res["p_up"] <= 1.0, "p_up is a probability in [0,1]")
    check(res["ret_p10_pct"] <= res["pred_return_pct"] <= res["ret_p90_pct"],
          "p10 <= median <= p90")
    check(res["as_of_bar"] == str(stamps.iloc[-1].date()),
          "as_of_bar is the last input bar, not today")

    out = score.write_forecasts([{"ticker": "TEST", "model": "rand",
                                  "generated_at": "now", **res}])
    check(list(next(iter(csv.DictReader(out.open())))) == score.FIELDS,
          "written header matches FIELDS")


def main() -> int:
    test_tool()
    test_scorer()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
