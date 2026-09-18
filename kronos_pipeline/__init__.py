"""Kronos integration for Catalyst Edge.

Kronos (https://github.com/shiyu-coder/Kronos, MIT) is a foundation model over
OHLCV candlesticks. It is wired in as an OFFLINE scorer, not as a request-path
dependency:

    ohlcv.py   Tradier /markets/history  ->  ohlcv/<TICKER>.csv   (stdlib only)
    score.py   ohlcv/<TICKER>.csv        ->  kronos_forecasts.csv (needs torch)

catalyst_mcp.py then serves kronos_forecasts.csv with _read_csv(), exactly like
it serves convergence_alerts.csv, and reports as_of from the file's mtime. The
MCP server keeps its stdlib-only, millisecond-start property; a 102M-parameter
forward pass never happens inside a request.
"""
