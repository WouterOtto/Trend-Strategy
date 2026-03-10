# Trend Following OS — Live Integration Guide
**Connecting `TrendFollowingOS.jsx` to the Python Pipeline**  
Architecture v3.2 | React 18 + Flask | March 2026

---

## What This Guide Is

This is a step-by-step implementation guide for replacing every piece of static
hardcoded data in the 1,857-line React dashboard (`TrendFollowingOS.jsx`) with
live reads from the real Python pipeline outputs. It is written to be used as a
source document in a Claude chat — paste it in full and ask Claude to implement
any specific step.

**By the end you will have:**
- A Flask API server (`api_server.py`) that reads pipeline JSON/parquet files
- A fully live React dashboard reading from it via `fetch()`
- Real terminal log streaming from `00_run_pipeline.py`
- All 8 tabs driven by actual pipeline outputs

**Estimated effort:** 5–7 focused working days

---

## Context: The Two Systems

### Python Pipeline (server-side)
```
project/
├── scripts/                     # 24 Python scripts (01–24)
├── data/
│   └── portfolio_state.json     # live positions
├── data_cache/
│   ├── signals/
│   │   ├── momentum_ranked.json      # Script 07 output → SIGNALS tab
│   │   ├── qualified_trends.json     # Script 06 output
│   │   └── exit_signals.json         # Script 10 output
│   ├── indicators/
│   │   └── {SYMBOL}_indicators.parquet  # Script 05 → TECH CHARTS
│   ├── portfolio/
│   │   ├── stop_levels.json          # Script 08 output
│   │   └── position_sizes.json       # Script 09 output
│   ├── validation/
│   │   └── validation_results.json   # Script 19 → BACKTEST tab
│   └── oos_validation/
│       └── oos_validation_results.json  # Script 20 → BACKTEST tab
├── reports/
│   ├── rebalancing/
│   │   └── {YYYY-MM}_recommendations.json  # Script 11/12 → REC tab
│   ├── daily/
│   │   └── {YYYY-MM-DD}_monitoring.json    # Script 14 → PORTFOLIO tab
│   └── charts/
│       └── technical_analysis_{ts}.html    # Script 15 (reference only)
└── 00_run_pipeline.py           # orchestrator
```

### React Dashboard (browser-side)
```
TrendFollowingOS.jsx             # single-file app, currently all static data
```

---

## Prerequisites

```bash
pip install flask flask-cors pandas pyarrow
```

---

---

# PHASE 1 — API Server

## Step 1 — Create `api_server.py`

Create this file at the project root (same level as `00_run_pipeline.py`).

```python
#!/usr/bin/env python3
"""
api_server.py — Trend Following OS API Bridge
Serves pipeline output files as JSON endpoints for the React dashboard.
Run: python api_server.py          (default port 5050)
     python api_server.py --port 8080
"""

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # allow requests from any origin (React dev server / Claude artifact)

ROOT = Path(__file__).resolve().parent

# ── Path helpers ──────────────────────────────────────────────────────────────

def _load(path: Path):
    """Load a JSON file; return None if missing."""
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None

def _latest(directory: Path, pattern: str):
    """Return the most recently modified file matching glob pattern."""
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None

# ── Endpoint 1 — Signals (Script 07 output) ──────────────────────────────────

@app.route("/api/signals")
def signals():
    path = ROOT / "data_cache/signals/momentum_ranked.json"
    data = _load(path)
    if not data:
        return jsonify({"error": "momentum_ranked.json not found. Run Script 07."}), 404
    return jsonify(data)   # shape: {"metadata":{}, "ranked":[...]}

# ── Endpoint 2 — Portfolio (portfolio_state.json) ─────────────────────────────

@app.route("/api/portfolio")
def portfolio():
    path = ROOT / "data/portfolio_state.json"
    data = _load(path)
    if not data:
        return jsonify({"error": "portfolio_state.json not found."}), 404

    # Normalise layout: unwrap "positions" key if present
    positions = data.get("positions", data)
    positions = {k: v for k, v in positions.items() if not k.startswith("_")}

    # Merge stop levels
    stop_path = ROOT / "data_cache/portfolio/stop_levels.json"
    stops_raw = _load(stop_path) or {}
    stops = stops_raw.get("stops", stops_raw)

    # Merge position sizes
    size_path = ROOT / "data_cache/portfolio/position_sizes.json"
    sizes_raw = _load(size_path) or {}
    sizes = sizes_raw.get("positions", sizes_raw)

    for sym, pos in positions.items():
        pos["_stop"] = stops.get(sym, {})
        pos["_size"] = sizes.get(sym, {})

    return jsonify({"positions": positions})

# ── Endpoint 3 — Recommendations (Script 11/12 output) ───────────────────────

@app.route("/api/recommendations")
def recommendations():
    rec_dir = ROOT / "reports/rebalancing"
    latest = _latest(rec_dir, "*_recommendations.json")
    if not latest:
        return jsonify({"error": "No recommendations JSON found. Run Script 11."}), 404
    data = _load(latest)
    if not data:
        return jsonify({"error": f"Could not parse {latest.name}"}), 500
    data["_source_file"] = latest.name
    return jsonify(data)

# ── Endpoint 4 — Chart data (Script 05 parquet → JSON) ───────────────────────

@app.route("/api/chart/<symbol>")
def chart(symbol):
    # symbol arrives as e.g. "NVDA.US" — sanitise the dot for filesystem safety
    safe = symbol.replace("/", "_")
    path = ROOT / f"data_cache/indicators/{safe}_indicators.parquet"
    if not path.exists():
        return jsonify({"error": f"Indicator file not found for {symbol}"}), 404

    cols = ["date", "close", "sma_50", "sma_200", "volume", "adx_14", "atr_20_pct"]
    try:
        df = pd.read_parquet(path, columns=cols)
    except Exception as e:
        # Try with all available columns and subset
        df = pd.read_parquet(path)
        available = [c for c in cols if c in df.columns]
        df = df[available]

    # Keep last 300 bars, fill nulls with None (JSON-serialisable)
    df = df.tail(300).where(pd.notnull(df), None)
    return jsonify({"symbol": symbol, "bars": df.to_dict(orient="records")})

# ── Endpoint 5 — Daily monitoring (Script 14 output) ─────────────────────────

@app.route("/api/monitoring")
def monitoring():
    mon_dir = ROOT / "reports/daily"
    latest = _latest(mon_dir, "*_monitoring.json")
    if not latest:
        return jsonify({"error": "No monitoring report found. Run Script 14."}), 404
    data = _load(latest)
    data["_source_file"] = latest.name
    return jsonify(data)

# ── Endpoint 6 — Risk analytics (Script 23 output) ───────────────────────────

@app.route("/api/risk")
def risk():
    risk_dir = ROOT / "data/performance/risk"
    latest = _latest(risk_dir, "*.json")
    if not latest:
        # Fallback: check reports/
        risk_dir = ROOT / "reports"
        latest = _latest(risk_dir, "*risk*.json")
    if not latest:
        return jsonify({"error": "No risk analytics file found. Run Script 23."}), 404
    data = _load(latest)
    data["_source_file"] = latest.name
    return jsonify(data)

# ── Endpoint 7 — Performance attribution (Script 22 output) ──────────────────

@app.route("/api/attribution")
def attribution():
    attr_dir = ROOT / "data/performance/attribution"
    latest = _latest(attr_dir, "*.json")
    if not latest:
        return jsonify({"error": "No attribution file found. Run Script 22."}), 404
    data = _load(latest)
    data["_source_file"] = latest.name
    return jsonify(data)

# ── Endpoint 8 — Backtest validation (Scripts 19 + 20 output) ────────────────

@app.route("/api/backtest")
def backtest():
    v19_path = ROOT / "data_cache/validation/validation_results.json"
    v20_path = ROOT / "data_cache/oos_validation/oos_validation_results.json"
    mc_path  = ROOT / "data_cache/monte_carlo/mc_summary.json"

    result = {}
    if v19_path.exists():
        result["validation"] = _load(v19_path)
    if v20_path.exists():
        result["oos"] = _load(v20_path)
    if mc_path.exists():
        result["monte_carlo"] = _load(mc_path)

    if not result:
        return jsonify({"error": "No backtest outputs found. Run Scripts 16–20."}), 404
    return jsonify(result)

# ── Endpoint 9 — Pipeline status (last_update + logs) ────────────────────────

@app.route("/api/status")
def status():
    meta_path = ROOT / "data_cache/metadata/last_update.json"
    meta = _load(meta_path) or {}

    # Read the most recent log file to check for errors
    log_dir = ROOT / "logs"
    latest_log = _latest(log_dir, "*.log") if log_dir.exists() else None
    last_log_lines = []
    if latest_log:
        try:
            with open(latest_log, encoding="utf-8", errors="replace") as f:
                last_log_lines = f.readlines()[-50:]  # last 50 lines
        except Exception:
            pass

    return jsonify({
        "last_update": meta,
        "log_file": str(latest_log) if latest_log else None,
        "log_tail": [l.rstrip() for l in last_log_lines],
    })

# ── Endpoint 10 — Pipeline execution (SSE streaming) ─────────────────────────

@app.route("/api/pipeline/run/<mode>")
def run_pipeline(mode):
    """
    Triggers 00_run_pipeline.py --mode=<mode> and streams stdout
    back to the browser as Server-Sent Events (SSE).

    Consumed by PipelineTab's EventSource in the React app.
    """
    ALLOWED_MODES = {"daily", "weekly", "monthly", "backtest",
                     "analytics", "validation", "report"}
    if mode not in ALLOWED_MODES:
        return jsonify({"error": f"Unknown mode: {mode}"}), 400

    script = ROOT / "00_run_pipeline.py"
    if not script.exists():
        return jsonify({"error": "00_run_pipeline.py not found"}), 404

    def generate():
        proc = subprocess.Popen(
            [sys.executable, str(script), f"--mode={mode}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(ROOT),
        )
        for line in proc.stdout:
            yield f"data: {line.rstrip()}\n\n"
        proc.wait()
        yield f"data: [DONE] exit_code={proc.returncode}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    checks = {
        "momentum_ranked":    (ROOT / "data_cache/signals/momentum_ranked.json").exists(),
        "portfolio_state":    (ROOT / "data/portfolio_state.json").exists(),
        "stop_levels":        (ROOT / "data_cache/portfolio/stop_levels.json").exists(),
        "recommendations":    bool(_latest(ROOT / "reports/rebalancing", "*_recommendations.json")),
        "validation_results": (ROOT / "data_cache/validation/validation_results.json").exists(),
        "indicators_dir":     (ROOT / "data_cache/indicators").exists(),
    }
    all_ok = all(checks.values())
    return jsonify({"status": "ok" if all_ok else "partial", "files": checks}), 200

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Trend Following OS API → http://{args.host}:{args.port}/api/health")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
```

**Test it:**
```bash
python api_server.py
curl http://localhost:5050/api/health
```

You should see a JSON object with `"status": "ok"` or `"partial"` and a file
check for each pipeline output.

---

---

# PHASE 2 — React App Wiring

The pattern for every tab is identical:

1. Replace the static module-scope constant with a `useState([])` / `useState(null)` default
2. Add a `useEffect` that `fetch()`es the API endpoint on mount
3. Pass a `loading` boolean to the JSX so the tab shows a spinner while data loads
4. Map the API response fields onto the exact field names the existing JSX already uses

The base URL should be a single constant at the top of the file:

```javascript
const API = "http://localhost:5050/api";
```

---

## Step 2 — Wire the SIGNALS Tab

**Current static data** (lines ~44–62 in the file):
```javascript
const SIGNALS = [
  { rank:1, symbol:"NVDA.US", name:"NVIDIA", exch:"NASDAQ", assetClass:"US Stock",
    sector:"Technology", close:875.4, sma50:820.1, sma200:634.2,
    adx:38.4, atrP:3.2, score:38.02, r20:12.1, r60:24.3, r120:37.8 },
  // ... 14 more rows
];
```

**Replace with (inside the `App()` component or at module scope using a hook):**
```javascript
// At module scope — replace the static SIGNALS array:
// const SIGNALS = [...];   ← DELETE THIS

// Inside App() or a custom hook:
const [SIGNALS, setSIGNALS] = useState([]);
const [signalsLoading, setSignalsLoading] = useState(true);

useEffect(() => {
  fetch(`${API}/signals`)
    .then(r => r.json())
    .then(data => {
      const ranked = data.ranked ?? [];
      // Map API field names → dashboard field names
      setSIGNALS(ranked.map(r => ({
        rank:       r.rank,
        symbol:     r.symbol,
        name:       r.name       ?? "",
        exch:       r.exchange   ?? "",
        assetClass: r.asset_class ?? "",
        sector:     r.sector     ?? "",
        close:      r.close,
        sma50:      r.sma_50,
        sma200:     r.sma_200,
        adx:        r.adx_14,
        atrP:       r.atr_20_pct,
        score:      r.momentum_score,
        r20:        r.roc_20d,
        r60:        r.roc_60d,
        r120:       r.roc_120d,
      })));
      setSignalsLoading(false);
    })
    .catch(() => setSignalsLoading(false));
}, []);
```

**Field mapping — `momentum_ranked.json` → dashboard:**

| Dashboard field | JSON field | Notes |
|---|---|---|
| `rank` | `rank` | integer, 1-based |
| `symbol` | `symbol` | e.g. `"NVDA.US"` |
| `name` | `name` | company name |
| `exch` | `exchange` | `"NYSE"`, `"NASDAQ"`, `"XETRA"` etc. |
| `assetClass` | `asset_class` | `"US Stock"`, `"EU Stock"`, `"ETF"`, `"Cryptocurrency"` |
| `sector` | `sector` | `"Technology"`, `"Healthcare"` etc. |
| `close` | `close` | last price |
| `sma50` | `sma_50` | 50-day SMA |
| `sma200` | `sma_200` | 200-day SMA |
| `adx` | `adx_14` | ADX(14) |
| `atrP` | `atr_20_pct` | ATR as % of price |
| `score` | `momentum_score` | `((close - sma_200) / sma_200) × 100` |
| `r20` | `roc_20d` | 20-day rate of change |
| `r60` | `roc_60d` | 60-day rate of change |
| `r120` | `roc_120d` | 120-day rate of change |

---

## Step 3 — Wire the PORTFOLIO Tab

**Current static data** (lines ~62–82 in the file):
```javascript
const PORTFOLIO = [
  { symbol:"NVDA.US", assetClass:"US Stock", sector:"Technology",
    shares:10, entry:625.3, curr:875.4, stop:782.1, stopType:"trailing",
    days:47, entryDate:"2025-12-18", name:"NVIDIA" },
  // ... 11 more rows
];
```

**Replace with:**
```javascript
const [PORTFOLIO, setPORTFOLIO] = useState([]);

useEffect(() => {
  fetch(`${API}/portfolio`)
    .then(r => r.json())
    .then(data => {
      const positions = data.positions ?? {};
      setPORTFOLIO(
        Object.entries(positions).map(([symbol, pos]) => {
          const stop = pos._stop ?? {};
          const currentStop = stop.stop_price
                           ?? stop.effective_stop_price
                           ?? pos.current_stop_price;
          const entryDate  = pos.entry_date ?? "";
          const days = entryDate
            ? Math.round((Date.now() - new Date(entryDate)) / 86400000)
            : pos.days_held ?? 0;

          return {
            symbol,
            name:       pos.name        ?? symbol,
            assetClass: pos.asset_class ?? "",
            sector:     pos.sector      ?? "",
            shares:     pos.shares      ?? 0,
            entry:      pos.entry_price ?? 0,
            curr:       pos.current_price ?? pos.close ?? 0,
            stop:       currentStop      ?? 0,
            stopType:   stop.stop_type   ?? pos.stop_type ?? "initial",
            days,
            entryDate,
          };
        })
      );
    });
}, []);
```

**portfolio_state.json schema (per position):**
```json
{
  "AAPL.US": {
    "entry_price":           150.0,
    "entry_date":            "2025-12-01",
    "shares":                10,
    "current_price":         162.0,
    "current_value":         1620.0,
    "unrealized_pnl":        120.0,
    "unrealized_pnl_pct":    8.0,
    "current_stop_price":    139.5,
    "initial_stop_price":    135.0,
    "trailing_stop_price":   139.5,
    "stop_type":             "trailing",
    "stop_last_update_date": "2026-01-31",
    "asset_class":           "US Stock",
    "sector":                "Technology",
    "name":                  "Apple Inc."
  }
}
```

---

## Step 4 — Wire the RECOMMENDATIONS Tab

This is the most complex mapping. The API returns Script 11's multi-scenario JSON.

**API response shape:**
```json
{
  "month":            "2026-02",
  "rebalance_date":   "2026-01-31",
  "execution_date":   "2026-02-03",
  "account_equity":   250000,
  "status":           "REBALANCE_REQUIRED",
  "circuit_breakers": { "halt_all": false, "drawdown_exceeded": false, ... },

  "scenario_1_pure_momentum": {
    "name": "Pure Momentum",
    "philosophy": "...",
    "entries": {
      "total": 3,
      "new": [
        { "symbol":"RHM.DE", "name":"Rheinmetall AG", "exchange":"XETRA",
          "asset_class":"EU Stock", "sector":"Industrials",
          "shares":12, "entry_price":580.5, "position_value_eur":6966,
          "initial_stop":522.45, "adx_14":41.2, "atr_20_pct":2.8,
          "momentum_score":32.1, "momentum_rank":1 }
      ]
    },
    "exits": {
      "mandatory": [ { "symbol":"XYZ.US", "reason":"stop_loss_hit", ... } ],
      "rotation":  [ { "symbol":"ABC.US", "reason":"rank_dropped", ... } ]
    },
    "holds": {
      "total": 10,
      "positions": [ { "symbol":"NVDA.US", "shares":10, ... } ]
    },
    "capital_summary": {
      "proceeds_from_exits": 15000,
      "cost_of_entries":     20958,
      "cash_before":         25000,
      "cash_after":          19042,
      "cash_pct":            7.6
    },
    "warnings": [ "Sector concentration: Technology 28% (limit 30%)" ]
  },

  "scenario_2_force_diversity": { ... },
  "scenario_3_balanced":        { ... }
}
```

**Mapping to the `SCENARIOS` array used by `RecTab`:**

The existing `RecTab` uses a `SCENARIOS` array with this shape:
```javascript
{
  id:         1,
  name:       "Scenario 1 — Pure Momentum",
  color:      "#4ea8f0",
  philosophy: "...",
  entries:    [ { rk, sy, nm, ex, sc, sh, lp, pv, pp, st, sd, adx, atr, ms } ],
  ...
}
```

**Replacement code inside `RecTab`:**
```javascript
const [recData, setRecData] = useState(null);
const SCENARIO_COLORS = ["#4ea8f0", "#22d3a0", "#a78bfa"];
const SCENARIO_KEYS   = [
  "scenario_1_pure_momentum",
  "scenario_2_force_diversity",
  "scenario_3_balanced",
];

useEffect(() => {
  fetch(`${API}/recommendations`)
    .then(r => r.json())
    .then(raw => {
      const built = SCENARIO_KEYS.map((key, i) => {
        const sc = raw[key] ?? {};
        return {
          id:          i + 1,
          name:        sc.name ?? `Scenario ${i+1}`,
          philosophy:  sc.philosophy ?? "",
          color:       SCENARIO_COLORS[i],
          entries: (sc.entries?.new ?? []).map((e, j) => ({
            rk:  e.momentum_rank   ?? j + 1,
            sy:  e.symbol,
            nm:  e.name            ?? "",
            ex:  e.exchange        ?? "",
            sc:  e.sector          ?? "",
            sh:  e.shares          ?? 0,
            lp:  e.entry_price     ?? 0,
            pv:  e.position_value_eur ?? 0,
            pp:  e.position_pct    ?? 0,
            st:  e.initial_stop    ?? e.stop_loss ?? 0,
            sd:  e.stop_distance_pct ?? 0,
            adx: e.adx_14          ?? 0,
            atr: e.atr_20_pct      ?? 0,
            ms:  e.momentum_score  ?? 0,
          })),
          mandExits:  sc.exits?.mandatory ?? [],
          rotExits:   sc.exits?.rotation  ?? [],
          holds:      sc.holds?.positions  ?? [],
          holdCount:  sc.holds?.total      ?? 0,
          capital:    sc.capital_summary   ?? {},
          warnings:   sc.warnings          ?? [],
          newCount:   sc.entries?.total    ?? 0,
          rotCount:   sc.exits?.rotation?.length  ?? 0,
          mandCount:  sc.exits?.mandatory?.length ?? 0,
        };
      });
      setRecData({ meta: raw, scenarios: built });
    });
}, []);
```

**Handling the HALT_ALL circuit breaker state:**
```javascript
// Before rendering, check for HALT_ALL
if (recData?.meta?.circuit_breakers?.halt_all) {
  return (
    <div style={{ color: A.red, padding: 40 }}>
      ⛔ CIRCUIT BREAKER — HALT ALL TRADING<br/>
      {recData.meta.circuit_breakers.reason ?? ""}
    </div>
  );
}
```

---

## Step 5 — Wire the TECH CHARTS Tab

The charts tab currently uses `genSeries()` to generate fake data. Replace it with
a real fetch to `/api/chart/{symbol}` whenever the selected symbol changes.

**Current code (inside `ChartsTab`):**
```javascript
const series = useMemo(() => genSeries(sel, sig.sma200*0.78, 1+idx*0.04, lb+50).slice(-lb),
  [sel, lb]);
```

**Replace with:**
```javascript
const [seriesData, setSeriesData] = useState([]);
const [chartLoading, setChartLoading] = useState(false);

useEffect(() => {
  if (!sel) return;
  setChartLoading(true);
  fetch(`${API}/chart/${encodeURIComponent(sel)}`)
    .then(r => r.json())
    .then(data => {
      // API returns { symbol, bars: [{date, close, sma_50, sma_200, volume, adx_14, atr_20_pct}] }
      const bars = (data.bars ?? []).slice(-lb);
      // Rename fields to match what Recharts expects in the chart JSX
      setSeriesData(bars.map(b => ({
        date:   b.date,
        close:  b.close,
        sma50:  b.sma_50,
        sma200: b.sma_200,
        vol:    b.volume,
        volAvg: null,          // compute 50-day avg client-side if needed
        adx:    b.adx_14,
        atr:    b.atr_20_pct,
      })));
      setChartLoading(false);
    })
    .catch(() => setChartLoading(false));
}, [sel, lb]);
```

Then replace `series` with `seriesData` everywhere in the chart JSX.

**Field mapping — `{SYMBOL}_indicators.parquet` → chart:**

| Chart field | Parquet column | Notes |
|---|---|---|
| `date` | `date` | YYYY-MM-DD string |
| `close` | `close` | closing price |
| `sma50` | `sma_50` | 50-day SMA |
| `sma200` | `sma_200` | 200-day SMA |
| `vol` | `volume` | daily volume |
| `adx` | `adx_14` | ADX(14) |
| `atr` | `atr_20_pct` | ATR as % of price |

---

## Step 6 — Wire the ANALYTICS Tab

The analytics tab uses `EQUITY_CURVE`, `ROLLING_SHARPE`, `HEATMAP`, and `ALLOC_PIE`.
These come from the performance attribution (Script 22) and risk analytics (Script 23) outputs.

```javascript
const [analyticsData, setAnalyticsData] = useState(null);

useEffect(() => {
  Promise.all([
    fetch(`${API}/attribution`).then(r => r.json()).catch(() => null),
    fetch(`${API}/risk`).then(r => r.json()).catch(() => null),
  ]).then(([attr, risk]) => {
    setAnalyticsData({ attribution: attr, risk });
  });
}, []);
```

**Performance attribution JSON shape (Script 22 output):**
```json
{
  "period_start":              "2021-01-01",
  "period_end":                "2026-01-31",
  "account_equity_eur":        250000,
  "ending_portfolio_value":    248500,
  "total_return_pct":          148.5,
  "total_pnl_eur":             148500,
  "trade_statistics": {
    "total_trades":   312,
    "win_rate_pct":   54.2,
    "profit_factor":  2.18
  },
  "asset_class_attribution": [ {"name":"US Stock","return_pct":22.1, ...} ],
  "sector_attribution":      [ {"name":"Technology","return_pct":31.4, ...} ],
  "position_attribution":    [ {"symbol":"NVDA.US","return_pct":142.3, ...} ],
  "top_contributors":        [ ... ],
  "top_detractors":          [ ... ]
}
```

**Risk analytics JSON shape (Script 23 output):**
```json
{
  "var_95":           -0.0182,
  "var_99":           -0.0274,
  "cvar_95":          -0.0241,
  "beta":              0.62,
  "tracking_error":    0.089,
  "information_ratio": 1.31,
  "hhi":               0.11,
  "max_drawdown_pct": -19.8,
  "sharpe_ratio":      1.47,
  "cagr_pct":          18.4,
  "equity_curve": [
    { "date":"2021-01-31", "strategy":100000, "benchmark":100000, "drawdown_pct":0.0 }
  ],
  "monthly_returns": {
    "2021": [2.1, -1.3, 4.2, 1.8, ...],
    "2022": [-3.1, 0.8, ...]
  }
}
```

**Mapping `equity_curve` to the `EQUITY_CURVE` constant shape:**
```javascript
// Script 23 equity_curve → dashboard EQUITY_CURVE shape:
// { label, strategy, benchmark, dd }
const EQUITY_CURVE = (risk?.equity_curve ?? []).map(row => ({
  label:     row.date.slice(0, 7),  // "2021-01"
  strategy:  row.strategy,
  benchmark: row.benchmark,
  dd:        row.drawdown_pct,
}));
```

**Mapping `monthly_returns` to the `HEATMAP` constant shape:**
```javascript
// Script 23 monthly_returns → dashboard HEATMAP shape:
// { "2021": [2.1, -1.3, ...], ... }
const HEATMAP = risk?.monthly_returns ?? {};
```

---

## Step 7 — Wire the BACKTEST Tab

```javascript
const [backtestData, setBacktestData] = useState(null);

useEffect(() => {
  fetch(`${API}/backtest`)
    .then(r => r.json())
    .then(setBacktestData);
}, []);
```

**validation_results.json shape (Script 19 output):**
```json
{
  "verdict":      "GO",
  "tests_passed": 9,
  "tests_total":  10,
  "total_score":  27,
  "rating":       "GOOD",
  "tests": [
    { "test_id":"T01_positive_expectancy", "passed":true,
      "value":0.184, "message":"GOOD — CAGR 18.4% ≥ threshold 10.0%" },
    { "test_id":"T02_risk_adjusted", "passed":true, ... },
    { "test_id":"T03_acceptable_drawdown", "passed":true, ... },
    { "test_id":"T04_trade_count", "passed":true, ... },
    { "test_id":"T05_win_rate", "passed":true, ... },
    { "test_id":"T06_profit_factor", "passed":true, ... },
    { "test_id":"T07_win_loss_ratio", "passed":true, ... },
    { "test_id":"T08_cost_sensitivity", "passed":false, ... },
    { "test_id":"T09_drawdown_recovery", "passed":true, ... },
    { "test_id":"T10_annual_consistency", "passed":true, ... }
  ],
  "scoring": {
    "total_score": 27,
    "max_score":   30,
    "rating":      "GOOD",
    "per_metric_scores": { "cagr": 3, "sharpe": 3, "max_drawdown": 2, ... }
  },
  "red_flags": {
    "critical_count": 0,
    "warning_count":  0,
    "flags": {
      "RF01_curve_fitting":  { "triggered":false, "severity":"critical" },
      "RF02_survivorship":   { "triggered":false, "severity":"critical" },
      "RF03_overfitting":    { "triggered":false, "severity":"critical" },
      "RF04_data_snooping":  { "triggered":false, "severity":"critical" },
      "RF05_costs":          { "triggered":false, "severity":"warning"  },
      "RF06_capacity":       { "triggered":false, "severity":"warning"  },
      "RF07_trade_count":    { "triggered":false, "severity":"warning"  }
    }
  },
  "benchmark_comparison": {
    "comparisons": {
      "SPY":   { "strategy_sharpe":1.47, "benchmark_sharpe":0.82, "beats_on_sharpe":true },
      "60_40": { "strategy_sharpe":1.47, "benchmark_sharpe":0.71, "beats_on_sharpe":true },
      "trend": { "strategy_sharpe":1.47, "benchmark_sharpe":0.95, "beats_on_sharpe":true }
    }
  },
  "raw_metrics": {
    "cagr": 0.184, "sharpe_ratio": 1.47, "max_drawdown_pct": -19.8,
    "win_rate_pct": 54.2, "profit_factor": 2.18, "total_trades": 312
  }
}
```

**Mapping backtest data to the existing JSX constants:**
```javascript
// Replace static TESTS array with:
const TESTS = (backtestData?.validation?.tests ?? []).map(t => ({
  id:     t.test_id,
  pass:   t.passed,
  val:    t.value,
  msg:    t.message,
}));

// Replace static SCORE array with:
const SCORE = Object.entries(
  backtestData?.validation?.scoring?.per_metric_scores ?? {}
).map(([k, v]) => ({ metric: k, pts: v, max: 3 }));

// Replace static RF array with:
const RF = Object.entries(
  backtestData?.validation?.red_flags?.flags ?? {}
).map(([id, info]) => ({
  id,
  triggered: info.triggered,
  severity:  info.severity,
  name:      info.name ?? id,
}));
```

---

## Step 8 — Wire the PIPELINE Tab (real execution)

Replace the `setInterval` simulation with a real `EventSource` connection.

**Current simulation code (inside `PipelineTab`):**
```javascript
const timer = setInterval(() => {
  // appends fake log entries
}, 500);
```

**Replace with:**
```javascript
const [run, setRun] = useState(null);
const [log, setLog] = useState([]);
const esRef = useRef(null);

const startPipeline = (mode) => {
  // Close any existing stream
  if (esRef.current) {
    esRef.current.close();
    esRef.current = null;
  }
  setLog([]);
  setRun(mode);

  const es = new EventSource(`${API}/pipeline/run/${mode}`);
  esRef.current = es;

  es.onmessage = (event) => {
    const line = event.data;
    if (line.startsWith("[DONE]")) {
      setRun(null);
      es.close();
      esRef.current = null;
    } else {
      setLog(prev => [...prev, { t: new Date().toLocaleTimeString(), msg: line }]);
    }
  };

  es.onerror = () => {
    setLog(prev => [...prev, { t: new Date().toLocaleTimeString(),
      msg: "[ERROR] Connection lost. Check api_server.py is running." }]);
    setRun(null);
    es.close();
  };
};

// Cleanup on unmount
useEffect(() => () => esRef.current?.close(), []);
```

Then in the button handlers, call `startPipeline("daily")` etc. instead of the
existing simulation timer.

---

---

# PHASE 3 — Field Reference Tables

## Complete File → Tab Mapping

| Tab | Primary file | Secondary files |
|---|---|---|
| OVERVIEW | none (static strategy rules) | — |
| PIPELINE | `logs/*.log` via `/api/status` | `data_cache/metadata/last_update.json` |
| SIGNALS | `data_cache/signals/momentum_ranked.json` | — |
| PORTFOLIO | `data/portfolio_state.json` | `data_cache/portfolio/stop_levels.json`, `position_sizes.json` |
| RECOMMENDATIONS | `reports/rebalancing/{YYYY-MM}_recommendations.json` | — |
| TECH CHARTS | `data_cache/indicators/{symbol}_indicators.parquet` | `data_cache/signals/momentum_ranked.json` (for sidebar list) |
| ANALYTICS | `data/performance/attribution/*.json` | `data/performance/risk/*.json` |
| BACKTEST | `data_cache/validation/validation_results.json` | `data_cache/oos_validation/oos_validation_results.json`, `data_cache/monte_carlo/mc_summary.json` |

---

## Scenario Keys

The recommendations JSON uses these exact top-level keys for the three scenarios:

| Scenario | JSON key | Dashboard color |
|---|---|---|
| S1 Pure Momentum | `scenario_1_pure_momentum` | `#4ea8f0` (blue) |
| S2 Force Diversity | `scenario_2_force_diversity` | `#22d3a0` (green) |
| S3 Balanced | `scenario_3_balanced` | `#a78bfa` (purple) |

---

## Entry Object Fields (within scenario entries.new[])

| Field | Type | Description |
|---|---|---|
| `symbol` | string | e.g. `"RHM.DE"` |
| `name` | string | company name |
| `exchange` | string | `"XETRA"`, `"NYSE"` etc. |
| `asset_class` | string | `"EU Stock"`, `"US Stock"`, `"ETF"`, `"Cryptocurrency"` |
| `sector` | string | sector name |
| `shares` | int | number of shares to buy |
| `entry_price` | float | close price at recommendation time |
| `position_value_eur` | float | shares × entry_price |
| `position_pct` | float | % of portfolio equity |
| `initial_stop` | float | entry − 3×ATR |
| `stop_distance_pct` | float | % from entry to stop |
| `adx_14` | float | ADX value |
| `atr_20_pct` | float | ATR as % of price |
| `momentum_score` | float | `((close − sma_200) / sma_200) × 100` |
| `momentum_rank` | int | rank in universe (1 = highest) |

---

## Capital Summary Fields

```json
"capital_summary": {
  "proceeds_from_exits":  15000.0,
  "cost_of_entries":      20958.0,
  "cash_before":          25000.0,
  "cash_after":           19042.0,
  "cash_pct":             7.6,
  "invested_value":       230958.0,
  "invested_pct":         92.4,
  "account_equity":       250000.0
}
```

---

---

# PHASE 4 — Error Handling Patterns

## Standard fetch wrapper

Add this utility near the top of the JSX file:

```javascript
const apiFetch = (path, setter, fallback = null) => {
  fetch(`${API}${path}`)
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(setter)
    .catch(err => {
      console.warn(`API ${path}:`, err.message);
      if (fallback !== null) setter(fallback);
    });
};

// Usage:
useEffect(() => { apiFetch("/signals", data => setSIGNALS(data.ranked ?? [])); }, []);
```

## Loading state pattern

```javascript
// In each tab component:
if (loading) return (
  <div className="anim" style={{ padding: 40, color: A.amber, fontFamily:"JetBrains Mono" }}>
    ▋ Loading data...
  </div>
);
```

## Stale data warning

```javascript
// Show a warning banner if the data file is more than 2 days old:
const isStale = (isoString) => {
  if (!isoString) return false;
  return (Date.now() - new Date(isoString)) > 2 * 86400 * 1000;
};

// In JSX:
{isStale(signals?.metadata?.generated_at) && (
  <div style={{ background:"#f0b42922", border:"1px solid #f0b429",
    color:"#f0b429", padding:"6px 12px", borderRadius:4, marginBottom:8 }}>
    ⚠ Data may be stale. Last update: {signals?.metadata?.generated_at}
  </div>
)}
```

---

---

# PHASE 5 — Deployment Checklist

## Running the full stack

```bash
# Terminal 1 — API server
cd /path/to/project
python api_server.py --port 5050

# Terminal 2 — React (if running via Vite outside Claude.ai)
npm run dev
```

## CORS in production

If the React app is served from a different origin than the API, the Flask
`CORS(app)` call in `api_server.py` handles it. To restrict to specific origins:

```python
CORS(app, origins=["http://localhost:5173", "https://your-domain.com"])
```

## Using inside Claude.ai artifact

The artifact renderer has access to `fetch()`. Set:
```javascript
const API = "http://localhost:5050/api";
```
and run `api_server.py` locally. The artifact will call your local server.
This requires that `api_server.py` binds to `0.0.0.0` if the artifact renderer
is hosted remotely:
```bash
python api_server.py --host 0.0.0.0 --port 5050
```

## Firewall / security note

Never expose `api_server.py` to the public internet — it can trigger real
pipeline execution. Run it on localhost only, or behind a VPN.

---

---

# PHASE 6 — Implementation Order

Work through this in sequence. Each step is independently testable before moving on.

### Day 1 — Foundation
- [ ] Create and start `api_server.py`
- [ ] Verify `/api/health` returns correct file status
- [ ] Add `const API = "http://localhost:5050/api"` to `TrendFollowingOS.jsx`
- [ ] Add the `apiFetch` utility function

### Day 2 — Signals + Portfolio (easiest tabs)
- [ ] Wire SIGNALS tab → `/api/signals` → `momentum_ranked.json`
- [ ] Verify 15 rows render with correct field values
- [ ] Wire PORTFOLIO tab → `/api/portfolio` → `portfolio_state.json` + `stop_levels.json`
- [ ] Verify positions table shows live P&L

### Day 3 — Analytics + Backtest (read-only JSON)
- [ ] Wire ANALYTICS tab → `/api/attribution` + `/api/risk`
- [ ] Map equity curve, heatmap, risk strip fields
- [ ] Wire BACKTEST tab → `/api/backtest`
- [ ] Map tests, scoring, red flags from `validation_results.json`

### Day 4 — Recommendations (most complex mapping)
- [ ] Inspect actual `{YYYY-MM}_recommendations.json` file manually
- [ ] Confirm `scenario_1_pure_momentum` key exists (vs legacy `scenario1_pure_momentum`)
- [ ] Wire RecTab → `/api/recommendations`
- [ ] Test Comparison view renders all 3 scenarios
- [ ] Test individual scenario views render entries table and capital summary

### Day 5 — Tech Charts (requires parquet conversion)
- [ ] Verify `/api/chart/NVDA.US` returns correct bar data
- [ ] Replace `genSeries()` with real fetch in `ChartsTab`
- [ ] Test chart renders with real price + SMA + volume + ADX + ATR panels
- [ ] Verify sidebar filters still work against live SIGNALS data

### Day 6 — Pipeline execution
- [ ] Test `EventSource` connection to `/api/pipeline/run/daily`
- [ ] Replace simulation timer in `PipelineTab` with `EventSource`
- [ ] Verify terminal log streams real output
- [ ] Test each mode button triggers the correct scripts

### Day 7 — Polish
- [ ] Add loading spinners to all tabs
- [ ] Add stale data warnings where `generated_at` is > 2 days old
- [ ] Add error states (API down, file missing)
- [ ] Test full MONTHLY pipeline run end-to-end with dashboard open

---

---

# Appendix A — Quick Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| `/api/signals` returns 404 | Script 07 hasn't run | Run `python scripts/07_rank_momentum.py` |
| `/api/chart/{symbol}` returns 404 | Parquet file missing | Run `python scripts/05_calculate_indicators.py` |
| `/api/recommendations` returns 404 | Script 11 hasn't run | Run `python scripts/11_monthly_rebalancing.py` |
| CORS error in browser | `flask-cors` not installed | `pip install flask-cors` |
| EventSource not firing | api_server.py not running | Start with `python api_server.py` |
| Recharts shows empty chart | `bars` array is empty | Check parquet file has data: `pd.read_parquet(path).tail(5)` |
| Recommendations show HALT_ALL banner | Circuit breaker triggered | Normal — shows correct state |
| `scenario_1_pure_momentum` key missing | Legacy Script 11 format | Check for `scenario1_pure_momentum` (no underscore between "scenario" and number) — update `SCENARIO_KEYS` array |

---

# Appendix B — Adding a `config.js` for Multi-Environment

To support switching between local dev, staging, and production without editing
`TrendFollowingOS.jsx`:

```javascript
// At the very top of TrendFollowingOS.jsx — the only line that changes per environment:
const API = window.__TREND_API__ ?? "http://localhost:5050/api";
```

Then set `window.__TREND_API__` in a separate HTML file or environment variable
before the React app loads.

---

*Guide version 1.0 — March 2026 — Trend Following OS Architecture v3.2*
