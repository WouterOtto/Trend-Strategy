import { useState, useEffect, useRef, useMemo } from "react";
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, ReferenceLine, ComposedChart, Cell, PieChart, Pie
} from "recharts";

const Fonts = () => (
  <style>{`
    @import url('https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=JetBrains+Mono:wght@300;400;500;600&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap');
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#04060e}
    ::-webkit-scrollbar{width:4px;height:4px}::-webkit-scrollbar-track{background:#0d1117}::-webkit-scrollbar-thumb{background:#f0b429;border-radius:2px}
    @keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
    .anim{animation:fadeIn 0.3s ease forwards}
    .blink{animation:blink 1.1s step-start infinite}
    button:hover{opacity:0.85;transition:opacity 0.12s}
    .hr:hover{background:rgba(255,255,255,0.06)!important}
  `}</style>
);

// ── Palettes ──────────────────────────────────────────────────────────────────
const A = {
  bg:"#04060e", card:"#080d16", card2:"#0d1520", border:"#141d2e", border2:"#1e2d45",
  amber:"#f0b429", text:"#dce6f5", dim:"#6b7f9e", green:"#22d3a0", red:"#f05454",
  blue:"#4ea8f0", purple:"#a78bfa", cyan:"#22d3ee", orange:"#fb923c"
};
const D = {
  equity:"#4FC3F7", bm:"#B0BEC5", profit:"#69F0AE", loss:"#FF5252",
  dd:"#FF8A65", sharpe:"#CE93D8", panelBg:"#1A1A2E", plotBg:"#16213E",
  grid:"#2A2A4A", text:"#E0E0E0", sub:"#9E9E9E"
};
const CH = {
  price:"#2563EB", sma50:"#F59E0B", sma200:"#EF4444", volume:"#94A3B8",
  adx:"#8B5CF6", atr:"#F97316", stop:"#DC2626", sidebar:"#1E293B", header:"#0F172A"
};
const P = {
  navy:"#1B2A47", accent:"#2E86DE", success:"#27AE60", danger:"#E74C3C",
  orange:"#E67E22", gold:"#F39C12", hold:"#7F8C8D"
};

// ── Core data ─────────────────────────────────────────────────────────────────
const SIGNALS = [
  {rank:1,  symbol:"NVDA.US",    name:"NVIDIA Corp",         exch:"NYSE",   assetClass:"US Stock",     sector:"Technology",    close:875.20,  sma50:812.0,  sma200:620.0,  adx:42.1, atrP:3.1, score:41.2, r20:8.1,  r60:38.5, r120:58.4},
  {rank:2,  symbol:"AVGO.US",    name:"Broadcom Inc",        exch:"NYSE",   assetClass:"US Stock",     sector:"Technology",    close:1640.0,  sma50:1510.0, sma200:1210.0, adx:38.4, atrP:2.8, score:35.5, r20:6.2,  r60:29.8, r120:44.1},
  {rank:3,  symbol:"META.US",    name:"Meta Platforms",      exch:"NASDAQ", assetClass:"US Stock",     sector:"Technology",    close:505.00,  sma50:478.0,  sma200:390.0,  adx:35.2, atrP:2.4, score:29.5, r20:4.8,  r60:24.1, r120:38.6},
  {rank:4,  symbol:"GE.US",      name:"GE Aerospace",        exch:"NYSE",   assetClass:"US Stock",     sector:"Industrials",   close:168.50,  sma50:157.0,  sma200:133.0,  adx:33.7, atrP:2.1, score:26.7, r20:3.6,  r60:21.0, r120:31.2},
  {rank:5,  symbol:"LLY.US",     name:"Eli Lilly & Co",      exch:"NYSE",   assetClass:"US Stock",     sector:"Healthcare",    close:780.00,  sma50:740.0,  sma200:625.0,  adx:31.9, atrP:1.9, score:24.8, r20:3.1,  r60:18.4, r120:29.3},
  {rank:6,  symbol:"RHM.DE",     name:"Rheinmetall AG",      exch:"XETRA",  assetClass:"EU Stock",     sector:"Defense",       close:780.00,  sma50:720.0,  sma200:628.0,  adx:37.8, atrP:2.6, score:24.2, r20:5.8,  r60:28.3, r120:42.7},
  {rank:7,  symbol:"NOVO-B.CO",  name:"Novo Nordisk A/S",    exch:"XETRA",  assetClass:"EU Stock",     sector:"Healthcare",    close:820.00,  sma50:775.0,  sma200:660.0,  adx:30.5, atrP:1.8, score:24.2, r20:2.8,  r60:17.9, r120:27.8},
  {rank:8,  symbol:"ORCL.US",    name:"Oracle Corp",         exch:"NYSE",   assetClass:"US Stock",     sector:"Technology",    close:128.50,  sma50:121.0,  sma200:104.0,  adx:29.4, atrP:1.7, score:23.6, r20:2.4,  r60:16.2, r120:24.1},
  {rank:9,  symbol:"JPM.US",     name:"JPMorgan Chase",      exch:"NYSE",   assetClass:"US Stock",     sector:"Financials",    close:198.00,  sma50:188.0,  sma200:162.0,  adx:28.1, atrP:1.6, score:22.2, r20:2.2,  r60:15.8, r120:22.6},
  {rank:10, symbol:"BTC-EUR",    name:"Bitcoin (EUR)",       exch:"CRYPTO", assetClass:"Cryptocurrency",sector:"Crypto",       close:62800,   sma50:58200,  sma200:51400,  adx:34.2, atrP:4.8, score:22.2, r20:7.4,  r60:31.2, r120:48.9},
  {rank:11, symbol:"CAT.US",     name:"Caterpillar Inc",     exch:"NYSE",   assetClass:"US Stock",     sector:"Industrials",   close:365.00,  sma50:347.0,  sma200:302.0,  adx:27.9, atrP:1.8, score:20.9, r20:2.0,  r60:14.7, r120:21.4},
  {rank:12, symbol:"ASML.AS",    name:"ASML Holding NV",     exch:"XETRA",  assetClass:"EU Stock",     sector:"Technology",    close:830.00,  sma50:790.0,  sma200:685.0,  adx:25.4, atrP:2.2, score:21.2, r20:3.2,  r60:12.9, r120:19.8},
  {rank:13, symbol:"AXP.US",     name:"Amer. Express Co",    exch:"NYSE",   assetClass:"US Stock",     sector:"Financials",    close:224.00,  sma50:214.0,  sma200:186.0,  adx:26.5, atrP:1.5, score:20.4, r20:1.8,  r60:13.6, r120:20.2},
  {rank:14, symbol:"SAP.DE",     name:"SAP SE",              exch:"XETRA",  assetClass:"EU Stock",     sector:"Technology",    close:188.00,  sma50:182.0,  sma200:159.0,  adx:24.1, atrP:1.6, score:18.2, r20:1.4,  r60:11.4, r120:17.3},
  {rank:15, symbol:"ETF-IBDX",   name:"iShs Eur Govt Bond",  exch:"ETF",    assetClass:"ETF",          sector:"Fixed Income",  close:118.20,  sma50:116.0,  sma200:110.0,  adx:21.3, atrP:0.7, score:7.5,  r20:0.6,  r60:3.2,  r120:4.8},
];

const PORTFOLIO = [
  {symbol:"NVDA.US",  assetClass:"US Stock",      sector:"Technology",   shares:12,   entry:620.00, curr:875.20,  stop:741.50,  stopType:"Trailing", days:477, entryDate:"15-Nov-2023", name:"NVIDIA Corp"},
  {symbol:"AVGO.US",  assetClass:"US Stock",      sector:"Technology",   shares:4,    entry:940.00, curr:1640.0,  stop:1254.0,  stopType:"Trailing", days:450, entryDate:"08-Dec-2023", name:"Broadcom Inc"},
  {symbol:"META.US",  assetClass:"US Stock",      sector:"Technology",   shares:8,    entry:440.00, curr:505.00,  stop:418.30,  stopType:"Trailing", days:362, entryDate:"11-Mar-2024", name:"Meta Platforms"},
  {symbol:"GE.US",    assetClass:"US Stock",      sector:"Industrials",  shares:22,   entry:148.00, curr:168.50,  stop:143.70,  stopType:"Trailing", days:204, entryDate:"15-Aug-2024", name:"GE Aerospace"},
  {symbol:"LLY.US",   assetClass:"US Stock",      sector:"Healthcare",   shares:6,    entry:680.00, curr:780.00,  stop:711.20,  stopType:"Trailing", days:408, entryDate:"19-Jan-2024", name:"Eli Lilly"},
  {symbol:"RHM.DE",   assetClass:"EU Stock",      sector:"Defense",      shares:15,   entry:520.00, curr:780.00,  stop:631.40,  stopType:"Trailing", days:380, entryDate:"20-Feb-2024", name:"Rheinmetall AG"},
  {symbol:"JPM.US",   assetClass:"US Stock",      sector:"Financials",   shares:18,   entry:172.00, curr:198.00,  stop:163.40,  stopType:"Initial",  days:185, entryDate:"04-Sep-2024", name:"JPMorgan Chase"},
  {symbol:"BTC-EUR",  assetClass:"Cryptocurrency",sector:"Crypto",       shares:0.12, entry:44200,  curr:62800,   stop:52180,   stopType:"Trailing", days:407, entryDate:"25-Jan-2024", name:"Bitcoin (EUR)"},
  {symbol:"CAT.US",   assetClass:"US Stock",      sector:"Industrials",  shares:10,   entry:328.00, curr:365.00,  stop:319.20,  stopType:"Initial",  days:137, entryDate:"22-Oct-2024", name:"Caterpillar"},
  {symbol:"ASML.AS",  assetClass:"EU Stock",      sector:"Technology",   shares:7,    entry:768.00, curr:830.00,  stop:748.60,  stopType:"Initial",  days:119, entryDate:"08-Nov-2024", name:"ASML Holding"},
  {symbol:"ORCL.US",  assetClass:"US Stock",      sector:"Technology",   shares:28,   entry:118.00, curr:128.50,  stop:114.20,  stopType:"Initial",  days:129, entryDate:"30-Oct-2024", name:"Oracle Corp"},
  {symbol:"AXP.US",   assetClass:"US Stock",      sector:"Financials",   shares:16,   entry:208.00, curr:224.00,  stop:198.40,  stopType:"Initial",  days:95,  entryDate:"03-Dec-2024", name:"Amer. Express"},
];

// Symbols recommended in each scenario (for the Rec filter in charts)
const REC_SYMBOLS = {
  s1: ["RHM.DE","NOVO-B.CO","AXP.US"],
  s2: ["RHM.DE","NOVO-B.CO","ETF-IBDX"],
  s3: ["RHM.DE","NOVO-B.CO","AXP.US"],
};
const ALL_REC_SYMBOLS = [...new Set(Object.values(REC_SYMBOLS).flat())];

// ── Equity / analytics data ───────────────────────────────────────────────────
const EQUITY_CURVE = (() => {
  const r = []; let s = 100000, b = 100000, pk = 100000;
  [["Jan'20",0.032,0.018],["Feb'20",-0.08,-0.12],["Mar'20",-0.12,-0.18],["Apr'20",0.07,0.13],
   ["May'20",0.04,0.05],["Jun'20",0.06,0.02],["Jul'20",0.05,0.055],["Aug'20",0.07,0.07],
   ["Sep'20",-0.02,-0.04],["Oct'20",-0.01,-0.028],["Nov'20",0.08,0.107],["Dec'20",0.06,0.038],
   ["Jan'21",0.02,-0.011],["Feb'21",0.04,0.028],["Mar'21",0.05,0.044],["Apr'21",0.05,0.053],
   ["May'21",0.01,0.006],["Jun'21",0.04,0.023],["Jul'21",0.03,0.023],["Aug'21",0.04,0.029],
   ["Sep'21",-0.02,-0.047],["Oct'21",0.06,0.069],["Nov'21",-0.01,-0.009],["Dec'21",0.04,0.044],
   ["Jan'22",-0.03,-0.058],["Feb'22",-0.02,-0.03],["Mar'22",0.03,0.037],["Apr'22",-0.04,-0.087],
   ["May'22",-0.01,-0.001],["Jun'22",-0.05,-0.082],["Jul'22",0.04,0.092],["Aug'22",-0.03,-0.042],
   ["Sep'22",-0.04,-0.091],["Oct'22",0.02,0.08],["Nov'22",0.01,0.057],["Dec'22",-0.02,-0.059],
   ["Jan'23",0.04,0.066],["Feb'23",-0.01,-0.026],["Mar'23",0.03,0.035],["Apr'23",0.02,0.015],
   ["May'23",0.01,0.003],["Jun'23",0.06,0.065],["Jul'23",0.03,0.032],["Aug'23",-0.01,-0.017],
   ["Sep'23",-0.02,-0.047],["Oct'23",0.01,-0.022],["Nov'23",0.06,0.089],["Dec'23",0.05,0.046],
   ["Jan'24",0.02,0.016],["Feb'24",0.05,0.052],["Mar'24",0.04,0.032],["Apr'24",-0.02,-0.042],
   ["May'24",0.04,0.048],["Jun'24",0.03,0.034],["Jul'24",0.02,0.011],["Aug'24",0.03,0.025],
   ["Sep'24",0.02,0.022],["Oct'24",-0.01,-0.009],["Nov'24",0.05,0.056],["Dec'24",0.01,0.002],
   ["Jan'25",0.03,0.028],["Feb'25",0.02,-0.018],["Mar'25",0.01,-0.005]
  ].forEach(([l, sr, br]) => {
    s *= (1+sr); b *= (1+br); pk = Math.max(pk, s);
    r.push({ label:l, strategy:Math.round(s), benchmark:Math.round(b), dd:parseFloat(((s/pk-1)*100).toFixed(2)) });
  });
  return r;
})();

const ROLLING_SHARPE = EQUITY_CURVE.slice(12).map((d, i) => {
  const w = EQUITY_CURVE.slice(i, i+12);
  const ret = w.map((v,j) => j===0 ? 0 : (v.strategy-w[j-1].strategy)/w[j-1].strategy);
  const m = ret.reduce((a,v) => a+v, 0) / ret.length;
  const sd = Math.sqrt(ret.reduce((a,v) => a+(v-m)**2, 0) / ret.length) || 0.001;
  return { label:d.label, sharpe:parseFloat((m/sd*Math.sqrt(12)-0.28).toFixed(2)) };
});

const HEATMAP = {
  2020:[3.2,-8.0,-12.0,7.0,4.0,6.0,5.0,7.0,-2.0,-1.0,8.0,6.0],
  2021:[2.0,4.0,5.0,5.0,1.0,4.0,3.0,4.0,-2.0,6.0,-1.0,4.0],
  2022:[-3.0,-2.0,3.0,-4.0,-1.0,-5.0,4.0,-3.0,-4.0,2.0,1.0,-2.0],
  2023:[4.0,-1.0,3.0,2.0,1.0,6.0,3.0,-1.0,-2.0,1.0,6.0,5.0],
  2024:[2.0,5.0,4.0,-2.0,4.0,3.0,2.0,3.0,2.0,-1.0,5.0,1.0],
  2025:[3.0,2.0,1.0,null,null,null,null,null,null,null,null,null],
};
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const ALLOC_PIE = [
  {name:"US Stock",value:38400,color:"#4FC3F7"},
  {name:"EU Stock",value:14200,color:"#81C784"},
  {name:"Cryptocurrency",value:7536,color:"#FF8A65"},
  {name:"ETF",value:2800,color:"#CE93D8"},
];

// Pipeline script list
const PIPELINE = [
  {num:1,  label:"Download EODHD bulk data",      mode:"daily/weekly/monthly", lastRun:"2026-03-07 06:02", status:"OK",   dur:"3m 12s"},
  {num:2,  label:"Download Yahoo fundamentals",    mode:"monthly",              lastRun:"2026-03-01 06:15", status:"OK",   dur:"41m 08s"},
  {num:3,  label:"Consolidate & validate data",    mode:"daily/weekly/monthly", lastRun:"2026-03-07 06:05", status:"OK",   dur:"2m 44s"},
  {num:4,  label:"Screen universe",                mode:"monthly",              lastRun:"2026-03-01 06:58", status:"OK",   dur:"8m 22s"},
  {num:5,  label:"Calculate indicators",           mode:"monthly",              lastRun:"2026-03-01 07:08", status:"OK",   dur:"12m 55s"},
  {num:6,  label:"Qualify trends",                 mode:"monthly",              lastRun:"2026-03-01 07:22", status:"OK",   dur:"1m 14s"},
  {num:7,  label:"Rank momentum",                  mode:"monthly",              lastRun:"2026-03-01 07:24", status:"OK",   dur:"0m 48s"},
  {num:8,  label:"Calculate stop-losses",          mode:"weekly/monthly",       lastRun:"2026-03-01 07:25", status:"OK",   dur:"0m 32s"},
  {num:9,  label:"Calculate position sizes",       mode:"weekly/monthly",       lastRun:"2026-03-01 07:26", status:"OK",   dur:"0m 18s"},
  {num:10, label:"Generate exit signals",          mode:"weekly/monthly",       lastRun:"2026-03-07 06:08", status:"OK",   dur:"0m 22s"},
  {num:11, label:"Monthly rebalancing",            mode:"monthly",              lastRun:"2026-03-01 07:28", status:"OK",   dur:"2m 05s"},
  {num:12, label:"Generate recommendation report", mode:"monthly",              lastRun:"2026-03-01 07:31", status:"OK",   dur:"1m 40s"},
  {num:14, label:"Daily portfolio monitoring",     mode:"daily",                lastRun:"2026-03-07 06:08", status:"WARN", dur:"0m 28s"},
  {num:15, label:"Generate technical charts",      mode:"monthly",              lastRun:"2026-03-01 07:33", status:"OK",   dur:"4m 12s"},
  {num:16, label:"Backtest engine",                mode:"backtest",             lastRun:"2026-02-15 14:02", status:"OK",   dur:"3h 18m"},
  {num:17, label:"Walk-forward optimizer",         mode:"backtest",             lastRun:"2026-02-15 17:22", status:"OK",   dur:"5h 44m"},
  {num:18, label:"Monte Carlo simulator",          mode:"backtest",             lastRun:"2026-02-15 23:06", status:"OK",   dur:"2h 12m"},
  {num:19, label:"Backtest validator",             mode:"backtest",             lastRun:"2026-02-16 01:20", status:"OK",   dur:"0m 55s"},
  {num:20, label:"Out-of-sample validator",        mode:"backtest",             lastRun:"2026-02-16 01:21", status:"OK",   dur:"1m 12s"},
  {num:21, label:"Deployment decision engine",     mode:"backtest",             lastRun:"2026-02-16 01:23", status:"OK",   dur:"0m 08s"},
  {num:22, label:"Performance attribution",        mode:"analytics",            lastRun:"2026-03-01 07:37", status:"OK",   dur:"0m 14s"},
  {num:23, label:"Risk analytics",                 mode:"analytics",            lastRun:"2026-03-01 07:37", status:"OK",   dur:"0m 11s"},
  {num:24, label:"Performance dashboard",          mode:"analytics",            lastRun:"2026-03-01 07:38", status:"OK",   dur:"0m 06s"},
];

// ── Helpers ───────────────────────────────────────────────────────────────────
const fN = (v, d=2) => v==null ? "—" : Number(v).toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d});
const fP = (v, d=1) => v==null ? "—" : `${v>=0?"+":""}${Number(v).toFixed(d)}%`;
const scC = s => ({OK:A.green, WARN:A.amber, FAIL:A.red})[s] || A.dim;
const sectorColor = {Technology:"#4ea8f0", Healthcare:"#22d3a0", Industrials:"#f0b429", Financials:"#a78bfa", Defense:"#f05454", Crypto:"#22d3ee", "Fixed Income":"#fb923c"};

const Tag = ({children, c=A.amber, sm}) => (
  <span style={{fontFamily:"JetBrains Mono", fontSize:sm?9:10, fontWeight:600, padding:sm?"1px 5px":"2px 7px",
    borderRadius:3, background:`${c}18`, border:`1px solid ${c}40`, color:c, whiteSpace:"nowrap"}}>{children}</span>
);

const ChartTip = ({active, payload, label, pct}) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{background:D.panelBg, border:`1px solid ${D.grid}`, borderRadius:6, padding:"9px 13px"}}>
      <div style={{fontFamily:"JetBrains Mono", fontSize:9, color:D.sub, marginBottom:5}}>{label}</div>
      {payload.map((p,i) => (
        <div key={i} style={{fontFamily:"JetBrains Mono", fontSize:11, color:p.color||D.text, marginBottom:1}}>
          {p.name}: {pct ? `${p.value?.toFixed?.(2)}%` : (typeof p.value==="number" && p.value>1000 ? `€${p.value.toLocaleString()}` : p.value)}
        </div>
      ))}
    </div>
  );
};

const KPI = ({l, v, s, c=A.amber}) => (
  <div style={{flex:"1 1 108px", padding:"12px 14px", background:A.card2, border:`1px solid ${A.border2}`, borderRadius:6}}>
    <div style={{fontFamily:"DM Sans", fontSize:9, color:A.dim, marginBottom:4, textTransform:"uppercase", letterSpacing:"0.04em"}}>{l}</div>
    <div style={{fontFamily:"JetBrains Mono", fontSize:20, fontWeight:700, color:c}}>{v}</div>
    {s && <div style={{fontFamily:"DM Sans", fontSize:10, color:A.dim, marginTop:2}}>{s}</div>}
  </div>
);

const SectionBar = ({c}) => <div style={{width:3, height:16, background:c||A.amber, borderRadius:2, flexShrink:0}}/>;

const genSeries = (sym, base, drift, n=190) => {
  const out = []; let p = base * 0.7;
  const seed = sym.charCodeAt(0)*0.6 + sym.charCodeAt(1)*0.4;
  let s50=[], s200=[], vb=[];
  for (let i=0; i<n; i++) {
    const dt = new Date("2025-10-01"); dt.setDate(dt.getDate()-(n-1-i));
    p *= (1 + drift*0.0018 + (Math.random()-0.47)*0.02 + Math.sin(i*0.26+seed)*0.003);
    const hi=p*(1+Math.random()*0.011), lo=p*(1-Math.random()*0.011), vol=Math.round(900000+Math.random()*2e6);
    s50.push(p); s200.push(p); vb.push(vol);
    if (s50.length>50) s50.shift(); if (s200.length>200) s200.shift(); if (vb.length>50) vb.shift();
    out.push({date:dt.toISOString().slice(0,10), close:p, hi, lo, vol,
      sma50:s50.reduce((a,v)=>a+v,0)/s50.length,
      sma200:s200.reduce((a,v)=>a+v,0)/s200.length,
      volAvg:vb.reduce((a,v)=>a+v,0)/vb.length,
      adx:Math.max(15, Math.min(58, 26+14*Math.sin(i*0.055+seed*0.5))),
      atr:(hi-lo)/p*100
    });
  }
  return out;
};

// ═══════════════════════════════════════════════════════════════════════════════
// TAB: OVERVIEW
// ═══════════════════════════════════════════════════════════════════════════════
const OverviewTab = () => {
  const rules = [
    {phase:"01–02 · DATA LAYER",    c:A.blue,   items:[["Universe","NYSE · NASDAQ · XETRA · LSE · Euronext · ETF · BTC/ETH"],["Source","EODHD (OHLCV adj.) + Yahoo Finance (fundamentals)"],["Frequency","Daily OHLCV · Monthly fundamentals"],["Min History","≥ 252 trading days"]]},
    {phase:"04 · UNIVERSE SCREEN",  c:A.blue,   items:[["Price","≥ $4.50 (US) · €5.00 (EU) · £4.00 (UK)"],["Liquidity","ADV ≥ $5M · Crypto 24h vol ≥ €1M"],["Market Cap","≥ $500M (US) · €200M (EU) · €4B (crypto)"],["ETFs","AUM ≥ €50M · no leveraged/inverse"]]},
    {phase:"05–06 · TREND QUALIFY", c:A.purple, items:[["Rule 1","SMA₅₀ > SMA₂₀₀  (golden cross)"],["Rule 2","Close > SMA₅₀  (price in trend)"],["Rule 3","ADX₁₄ > 20  (trend confirmed)"],["Logic","ALL 3 TRUE simultaneously"]]},
    {phase:"07 · MOMENTUM RANK",    c:A.purple, items:[["Score","((Close − SMA₂₀₀) / SMA₂₀₀) × 100"],["Aux","ROC₂₀, ROC₆₀, ROC₁₂₀ (stored, not ranked)"],["Selection","Top-N descending rank"]]},
    {phase:"08 · STOP-LOSS REGIME", c:A.amber,  items:[["Initial","Entry − 3.0 × ATR₂₀"],["Trailing Trigger","≥ Entry × 1.15 (+15% profit)"],["Trailing","max(stop, Friday_close − 4.0 × ATR₂₀)"],["Rule","Fridays only · Ratchet-up only"]]},
    {phase:"09 · POSITION SIZING",  c:A.amber,  items:[["Target Risk","2.0% of account equity"],["Vol Adj","Multiplier = Median_ATR% / Instr_ATR%"],["Clip","Floor 0.5% · Ceiling 8.0% equity"],["Constraints","Crypto ≤ 20% · Sector ≤ 30% · Cash ≥ 5%"]]},
    {phase:"11 · CIRCUIT BREAKERS", c:A.red,    items:[["DD < −15%","Halt entries, allow exits only"],["VIX ≥ 40","Halt until VIX < 30 for 3 days"],["Corr > 0.85","Max pairwise top-10 → halt entries"],["Top-3 > 30%","Force rebalance"],["Staleness > 3d","Halt ALL trading"]]},
    {phase:"11 · REBALANCING",      c:A.green,  items:[["Monthly","Last Saturday — full re-screen"],["Weekly","Saturday — stops + exit signals"],["Daily","Weekdays — monitoring only"],["Exec Order","Exits → Entries → Update stops"]]},
  ];
  return (
    <div className="anim" style={{display:"flex", flexDirection:"column", gap:16}}>
      <div style={{display:"flex", gap:10, flexWrap:"wrap"}}>
        {[["Strategy","Trend Following",A.amber],["Architecture","v3.2 · Feb 2026",A.cyan],["Scripts","24 pipeline",A.blue],["Universe","7 exchanges",A.blue],["IS CAGR","+18.4%",A.green],["IS Sharpe","1.47",A.green],["Max DD","−19.8%",A.red],["Deploy","✓ GO",A.green]].map(([l,v,c]) => <KPI key={l} l={l} v={v} c={c}/>)}
      </div>
      <div style={{display:"grid", gridTemplateColumns:"repeat(auto-fill,minmax(375px,1fr))", gap:12}}>
        {rules.map(({phase, c, items}) => (
          <div key={phase} style={{background:A.card, border:`1px solid ${A.border}`, borderRadius:8, overflow:"hidden"}}>
            <div style={{background:`${c}18`, borderBottom:`1px solid ${c}30`, padding:"7px 14px"}}>
              <span style={{fontFamily:"Syne", fontSize:11, fontWeight:700, color:c, letterSpacing:"0.07em"}}>{phase}</span>
            </div>
            <div style={{padding:"8px 14px"}}>
              {items.map(([k,v]) => (
                <div key={k} style={{display:"grid", gridTemplateColumns:"185px 1fr", gap:8, padding:"3px 0", borderBottom:`1px solid ${A.border}`}}>
                  <span style={{fontFamily:"JetBrains Mono", fontSize:10, color:A.dim}}>{k}</span>
                  <span style={{fontFamily:"JetBrains Mono", fontSize:10, color:A.text}}>{v}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// TAB: PIPELINE
// ═══════════════════════════════════════════════════════════════════════════════
const PipelineTab = () => {
  const [run, setRun] = useState(null);
  const [log, setLog] = useState([]);
  const lRef = useRef(null);
  const MODES = [
    {id:"daily",      l:"DAILY",      s:"1,3,14",         t:"~1 min",  d:"Download + monitor"},
    {id:"weekly",     l:"WEEKLY",     s:"1,3,9,10,14",    t:"~2–8 hr", d:"Stops + exits"},
    {id:"monthly",    l:"MONTHLY",    s:"1–12,15,22–24",  t:"~2–12 hr",d:"Full rebalance"},
    {id:"backtest",   l:"BACKTEST",   s:"16–21",          t:"hours",   d:"Validation pipeline"},
    {id:"analytics",  l:"ANALYTICS",  s:"22,23,24",       t:"~30 sec", d:"Performance review"},
    {id:"validation", l:"VALIDATION", s:"19,20,21",       t:"~5 min",  d:"Validate results"},
    {id:"report",     l:"REPORT",     s:"12",             t:"~2 min",  d:"Re-generate PDF"},
  ];
  const sim = m => {
    if (run) return; setRun(m); setLog([]);
    const mm = {daily:["daily"],weekly:["daily","weekly"],monthly:["daily","weekly","monthly"],backtest:["backtest"],analytics:["analytics"],validation:["backtest"],report:["monthly"]};
    const steps = PIPELINE.filter(s => s.mode.split("/").some(sm => (mm[m]||[]).includes(sm)));
    let i = 0;
    const iv = setInterval(() => {
      if (i >= steps.length) { clearInterval(iv); setRun(null); return; }
      setLog(p => [...p, {time:new Date().toLocaleTimeString(), ...steps[i]}]); i++;
    }, 500);
  };
  useEffect(() => { if (lRef.current) lRef.current.scrollTop = lRef.current.scrollHeight; }, [log]);
  return (
    <div className="anim" style={{display:"flex", gap:16}}>
      <div style={{width:236, flexShrink:0, display:"flex", flexDirection:"column", gap:7}}>
        {MODES.map(m => (
          <button key={m.id} onClick={()=>sim(m.id)} disabled={!!run} style={{background:run===m.id?`${A.amber}18`:A.card2, border:`1px solid ${run===m.id?A.amber:A.border}`, borderRadius:6, padding:"8px 12px", cursor:run?"not-allowed":"pointer", textAlign:"left"}}>
            <div style={{display:"flex", justifyContent:"space-between", marginBottom:2}}>
              <span style={{fontFamily:"Syne", fontSize:11, fontWeight:700, color:run===m.id?A.amber:A.text, letterSpacing:"0.06em"}}>{m.l}</span>
              <Tag c={run===m.id?A.amber:A.dim} sm>{m.t}</Tag>
            </div>
            <div style={{fontFamily:"DM Sans", fontSize:10, color:A.dim, marginBottom:2}}>{m.d}</div>
            <div style={{fontFamily:"JetBrains Mono", fontSize:9, color:A.blue}}>Steps: {m.s}</div>
          </button>
        ))}
        <div style={{marginTop:4, padding:10, background:A.card, border:`1px solid ${A.border}`, borderRadius:6}}>
          <div style={{fontFamily:"DM Sans", fontSize:9, color:A.dim, marginBottom:5}}>CLI</div>
          <div style={{fontFamily:"JetBrains Mono", fontSize:9, color:A.cyan, lineHeight:1.9}}>
            python 00_run_pipeline.py<br/>{"  "}monthly \<br/>{"  "}--as-of-date 2026-03-07 \<br/>{"  "}--equity 50000 \<br/>{"  "}--vix 17.8
          </div>
        </div>
      </div>
      <div style={{flex:1, display:"flex", flexDirection:"column", gap:14}}>
        <div style={{background:"#02040a", border:`1px solid ${A.border}`, borderRadius:8, padding:14}}>
          <div style={{display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:10}}>
            <div style={{display:"flex", gap:5}}>{[A.red,A.amber,A.green].map(c=><div key={c} style={{width:9,height:9,borderRadius:"50%",background:c}}/>)}</div>
            <span style={{fontFamily:"JetBrains Mono", fontSize:10, color:A.dim}}>pipeline.log — {run ? <span style={{color:A.amber}}>RUNNING: {run.toUpperCase()}</span> : "idle"}</span>
          </div>
          <div ref={lRef} style={{overflowY:"auto", maxHeight:240}}>
            {!log.length && <div style={{fontFamily:"JetBrains Mono", fontSize:11, color:A.dim}}>$ Select a mode<span className="blink" style={{color:A.amber}}>_</span></div>}
            {log.map((l,i) => (
              <div key={i} style={{fontFamily:"JetBrains Mono", fontSize:10, display:"flex", gap:10, padding:"2px 0"}}>
                <span style={{color:A.dim}}>{l.time}</span>
                <span style={{color:A.blue}}>[{String(l.num).padStart(2,"0")}]</span>
                <span style={{color:scC(l.status), width:36, fontWeight:600}}>{l.status}</span>
                <span style={{color:A.text, flex:1}}>{l.label}</span>
                <span style={{color:A.dim}}>{l.dur}</span>
              </div>
            ))}
            {run && log.length>0 && <div style={{fontFamily:"JetBrains Mono", fontSize:10, color:A.amber}}>Processing<span className="blink">...</span></div>}
          </div>
        </div>
        <div style={{background:A.card, border:`1px solid ${A.border}`, borderRadius:8, padding:14}}>
          <div style={{display:"flex", alignItems:"center", gap:8, marginBottom:12}}>
            <SectionBar/><span style={{fontFamily:"Syne", fontSize:12, fontWeight:700, color:A.text, letterSpacing:"0.05em", textTransform:"uppercase"}}>All 24 Scripts — Last Execution Status</span>
          </div>
          <div style={{overflowX:"auto"}}>
            <table style={{width:"100%", borderCollapse:"collapse", fontFamily:"JetBrains Mono", fontSize:10}}>
              <thead><tr style={{borderBottom:`1px solid ${A.border2}`}}>{["#","Script","Mode","Last Run","Status","Duration"].map(h=><th key={h} style={{padding:"4px 8px",color:A.dim,fontWeight:500,textAlign:"left"}}>{h}</th>)}</tr></thead>
              <tbody>{PIPELINE.map(s => (
                <tr key={s.num} className="hr" style={{borderBottom:`1px solid ${A.border}`}}>
                  <td style={{padding:"4px 8px",color:A.dim}}>{String(s.num).padStart(2,"0")}</td>
                  <td style={{padding:"4px 8px",color:A.text}}>{s.label}</td>
                  <td style={{padding:"4px 8px",color:A.blue,fontSize:9}}>{s.mode}</td>
                  <td style={{padding:"4px 8px",color:A.dim}}>{s.lastRun}</td>
                  <td style={{padding:"4px 8px"}}><Tag c={scC(s.status)} sm>{s.status}</Tag></td>
                  <td style={{padding:"4px 8px",color:A.dim}}>{s.dur}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// TAB: SIGNALS
// ═══════════════════════════════════════════════════════════════════════════════
const SignalsTab = () => {
  const [sec, setSec] = useState("ALL");
  const secs = ["ALL", ...new Set(SIGNALS.map(s => s.sector))];
  const rows = sec === "ALL" ? SIGNALS : SIGNALS.filter(s => s.sector === sec);
  return (
    <div className="anim" style={{display:"flex", flexDirection:"column", gap:14}}>
      <div style={{display:"flex", gap:10, flexWrap:"wrap"}}>
        <KPI l="Qualified" v="15" s="of 892 screened" c={A.amber}/>
        <KPI l="Qual. Rate" v="1.7%" s="screened" c={A.dim}/>
        <KPI l="Avg Score" v={`+${(SIGNALS.reduce((a,s)=>a+s.score,0)/SIGNALS.length).toFixed(1)}`} s="momentum" c={A.green}/>
        <KPI l="Avg ADX" v={(SIGNALS.reduce((a,s)=>a+s.adx,0)/SIGNALS.length).toFixed(1)} s=">20=trending" c={A.amber}/>
        <KPI l="As-of Date" v="2026-03-07" s="Script 07" c={A.cyan}/>
      </div>
      <div style={{display:"flex", gap:6, flexWrap:"wrap"}}>
        {secs.map(s => (
          <button key={s} onClick={()=>setSec(s)} style={{fontFamily:"JetBrains Mono",fontSize:10,padding:"3px 10px",borderRadius:4,cursor:"pointer",background:sec===s?`${A.amber}20`:"transparent",border:`1px solid ${sec===s?A.amber:A.border2}`,color:sec===s?A.amber:A.dim}}>{s}</button>
        ))}
      </div>
      <div style={{background:A.card, border:`1px solid ${A.border}`, borderRadius:8, padding:14, overflowX:"auto"}}>
        <table style={{width:"100%", borderCollapse:"collapse", fontFamily:"JetBrains Mono", fontSize:10}}>
          <thead><tr style={{borderBottom:`1px solid ${A.border2}`}}>{["RNK","SYMBOL","NAME","EXCH","SECTOR","CLOSE","SMA50","SMA200","ADX","ATR%","SCORE","ROC20","ROC60","ROC120"].map(h=><th key={h} style={{padding:"5px 8px",color:A.dim,fontWeight:500,textAlign:["NAME","SECTOR"].includes(h)?"left":"right"}}>{h}</th>)}</tr></thead>
          <tbody>{rows.map(s => (
            <tr key={s.symbol} className="hr" style={{borderBottom:`1px solid ${A.border}`}}>
              <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>#{s.rank}</td>
              <td style={{padding:"5px 8px",color:A.cyan,textAlign:"right"}}>{s.symbol}</td>
              <td style={{padding:"5px 8px",color:A.text,textAlign:"left"}}>{s.name}</td>
              <td style={{padding:"5px 8px",textAlign:"right"}}><Tag c={A.blue} sm>{s.exch}</Tag></td>
              <td style={{padding:"5px 8px",color:sectorColor[s.sector]||A.dim,textAlign:"left",fontSize:9}}>{s.sector}</td>
              <td style={{padding:"5px 8px",color:A.text,textAlign:"right"}}>{fN(s.close,2)}</td>
              <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>{fN(s.sma50,0)}</td>
              <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>{fN(s.sma200,0)}</td>
              <td style={{padding:"5px 8px",color:s.adx>30?A.green:A.amber,textAlign:"right"}}>{s.adx.toFixed(1)}</td>
              <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>{s.atrP.toFixed(1)}%</td>
              <td style={{padding:"5px 8px",color:s.score>25?A.green:s.score>15?A.amber:A.dim,fontWeight:700,textAlign:"right"}}>+{s.score.toFixed(1)}</td>
              <td style={{padding:"5px 8px",color:A.text,textAlign:"right"}}>{fP(s.r20)}</td>
              <td style={{padding:"5px 8px",color:s.r60>20?A.green:A.text,textAlign:"right"}}>{fP(s.r60)}</td>
              <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>{fP(s.r120)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// TAB: RECOMMENDATIONS  (Script 12 — 3 scenarios)
// ═══════════════════════════════════════════════════════════════════════════════
const RecTab = () => {
  const [activeScen, setActiveScen] = useState(0);

  // ── palette aliases for this tab ──
  const BG   = "#050810";   // near-black page background
  const CARD = "#0a0e1a";   // card surface
  const CARD2= "#0f1422";   // alt row / deeper card
  const BDR  = "#1a2236";   // border
  const BDR2 = "#24334d";   // stronger border

  // ── styling helpers ──
  const SecHead = ({c, children}) => (
    <div style={{display:"flex",alignItems:"center",gap:9,marginTop:20,marginBottom:8}}>
      <div style={{width:3,height:14,background:c,borderRadius:2,flexShrink:0}}/>
      <span style={{fontFamily:"Syne",fontSize:11,fontWeight:700,color:c,letterSpacing:"0.07em",textTransform:"uppercase"}}>{children}</span>
    </div>
  );
  const th = (c, al="right") => ({
    background:CARD2, color:c, fontSize:9, fontWeight:700, padding:"7px 9px",
    textAlign:al, borderBottom:`2px solid ${c}`, whiteSpace:"nowrap",
    fontFamily:"JetBrains Mono", letterSpacing:"0.03em",
  });
  const tdR = (al="right", alt=false) => ({
    fontSize:9, padding:"6px 9px", borderBottom:`1px solid ${BDR}`,
    textAlign:al, background:alt?CARD2:CARD, color:"#c8d8f0",
    fontFamily:"JetBrains Mono",
  });
  const kvRow = (k, v, vc) => (
    <tr key={k}>
      <td style={{...tdR("left",true),color:"#5a7090",fontWeight:500,width:"46%",fontSize:9}}>{k}</td>
      <td style={{...tdR("left",false),color:vc||"#c8d8f0",fontWeight:vc?600:400}}>{v}</td>
    </tr>
  );

  const DCard = ({c, title, children, style}) => (
    <div style={{background:CARD, border:`1px solid ${c?c+"30":BDR}`, borderRadius:8,
      borderLeft:c?`3px solid ${c}`:"none", overflow:"hidden", ...style}}>
      {title && (
        <div style={{padding:"9px 14px", borderBottom:`1px solid ${BDR}`, background:CARD2,
          display:"flex", alignItems:"center", gap:8}}>
          <div style={{width:3,height:12,background:c||A.amber,borderRadius:2}}/>
          <span style={{fontFamily:"Syne",fontSize:10,fontWeight:700,color:c||A.amber,
            letterSpacing:"0.07em",textTransform:"uppercase"}}>{title}</span>
        </div>
      )}
      <div style={{padding:"12px 14px"}}>{children}</div>
    </div>
  );

  const ActionTile = ({label, count, c}) => (
    <div style={{flex:1, background:CARD, border:`1px solid ${c}30`, borderRadius:8, overflow:"hidden", textAlign:"center"}}>
      <div style={{background:`${c}15`, borderBottom:`1px solid ${c}30`, padding:"7px 6px"}}>
        <span style={{fontFamily:"Syne",fontSize:9,fontWeight:700,color:c,letterSpacing:"0.06em"}}>{label}</span>
      </div>
      <div style={{padding:"14px 0"}}>
        <span style={{fontFamily:"Syne",fontSize:36,fontWeight:800,color:c,lineHeight:1}}>{count}</span>
      </div>
    </div>
  );

  const SCENARIOS = [
    {
      id:1, name:"Scenario 1 — Pure Momentum", shortName:"S1  PURE MOMENTUM",
      philosophy:"Select highest-momentum instruments regardless of geographic or sector concentration.",
      candidatePool:"Top-14 by momentum score · full qualified universe · no concentration cap",
      color:"#4ea8f0",
      entries:[
        {rk:6,  sy:"RHM.DE",    nm:"Rheinmetall AG",   ex:"XETRA", sc:"Defense",      sh:15, lp:785.91, pv:11789, pp:23.6, st:631.40, sd:19.3, adx:37.8, atr:2.6, ms:24.2},
        {rk:7,  sy:"NOVO-B.CO", nm:"Novo Nordisk A/S", ex:"XETRA", sc:"Healthcare",   sh:7,  lp:826.11, pv:5754,  pp:11.5, st:710.20, sd:13.6, adx:30.5, atr:1.8, ms:24.2},
        {rk:13, sy:"AXP.US",    nm:"Amer. Express",    ex:"NYSE",  sc:"Financials",   sh:16, lp:226.13, pv:3620,  pp:7.2,  st:198.40, sd:11.8, adx:26.5, atr:1.5, ms:20.4},
      ],
      newCount:3, rotCount:1, mandCount:2, holdCount:10,
      exitProceeds:6036, entryRequired:21163, cashAfter:7.2, cashEur:3600,
      assetBreakdown:[["CRYPTOCURRENCY","€7,536","15.1%","1"],["EU STOCK","€17,543","35.1%","4"],["ETF","€2,800","5.6%","1"],["US STOCK","€38,521","77.0%","8"]],
      warnings:["Technology sector at 41.8% — above 30% guideline (highest concentration across all 3 scenarios). Monitor closely."],
    },
    {
      id:2, name:"Scenario 2 — Force Diversity", shortName:"S2  FORCE DIVERSITY",
      philosophy:"Enforce geographic and sector diversification. Cap ≤4 per exchange and ≤3 per sector.",
      candidatePool:"Top-14 with ≤4 per exchange · ≤3 per sector · from qualified universe",
      color:"#22d3a0",
      entries:[
        {rk:6,  sy:"RHM.DE",    nm:"Rheinmetall AG",   ex:"XETRA", sc:"Defense",      sh:15, lp:785.91, pv:11789, pp:23.6, st:631.40, sd:19.3, adx:37.8, atr:2.6, ms:24.2},
        {rk:7,  sy:"NOVO-B.CO", nm:"Novo Nordisk A/S", ex:"XETRA", sc:"Healthcare",   sh:7,  lp:826.11, pv:5754,  pp:11.5, st:710.20, sd:13.6, adx:30.5, atr:1.8, ms:24.2},
        {rk:11, sy:"ETF-IBDX",  nm:"iShs Eur Govt Bd", ex:"ETF",   sc:"Fixed Income", sh:42, lp:118.79, pv:4990,  pp:10.0, st:111.20, sd:6.4,  adx:21.3, atr:0.7, ms:7.5},
      ],
      newCount:3, rotCount:1, mandCount:2, holdCount:10,
      exitProceeds:6036, entryRequired:22533, cashAfter:5.9, cashEur:2950,
      assetBreakdown:[["CRYPTOCURRENCY","€7,536","15.1%","1"],["ETF","€7,790","15.6%","2"],["EU STOCK","€17,543","35.1%","4"],["US STOCK","€29,531","59.1%","6"]],
      warnings:["Post-rebalance cash 5.9% — minimal buffer above 5% floor. Verify all fills carefully before EOD."],
    },
    {
      id:3, name:"Scenario 3 — Balanced", shortName:"S3  BALANCED",
      philosophy:"Balance momentum strength with moderate diversification. Cap ≤5 per exchange, ≤4 per sector.",
      candidatePool:"Top-14 with ≤5 per exchange · ≤4 per sector · from qualified universe",
      color:"#a78bfa",
      entries:[
        {rk:6,  sy:"RHM.DE",    nm:"Rheinmetall AG",   ex:"XETRA", sc:"Defense",      sh:15, lp:785.91, pv:11789, pp:23.6, st:631.40, sd:19.3, adx:37.8, atr:2.6, ms:24.2},
        {rk:7,  sy:"NOVO-B.CO", nm:"Novo Nordisk A/S", ex:"XETRA", sc:"Healthcare",   sh:7,  lp:826.11, pv:5754,  pp:11.5, st:710.20, sd:13.6, adx:30.5, atr:1.8, ms:24.2},
        {rk:13, sy:"AXP.US",    nm:"Amer. Express",    ex:"NYSE",  sc:"Financials",   sh:16, lp:226.13, pv:3620,  pp:7.2,  st:198.40, sd:11.8, adx:26.5, atr:1.5, ms:20.4},
      ],
      newCount:3, rotCount:1, mandCount:2, holdCount:10,
      exitProceeds:6036, entryRequired:21163, cashAfter:7.2, cashEur:3600,
      assetBreakdown:[["CRYPTOCURRENCY","€7,536","15.1%","1"],["ETF","€2,800","5.6%","1"],["EU STOCK","€17,543","35.1%","4"],["US STOCK","€35,521","71.0%","7"]],
      warnings:["Technology sector at 35.2% — slightly above 30% guideline. Consider trimming on next rebalance cycle."],
    },
  ];

  const holds = [
    {s:"NVDA.US",  n:12,   e:620.00, ed:"15-Nov-2023", cv:10502, pnl:3062, pp:41.2, stop:741.50,  tr:true},
    {s:"AVGO.US",  n:4,    e:940.00, ed:"08-Dec-2023", cv:6560,  pnl:2800, pp:74.5, stop:1254.0,  tr:true},
    {s:"META.US",  n:8,    e:440.00, ed:"11-Mar-2024", cv:4040,  pnl:520,  pp:14.8, stop:418.30,  tr:true},
    {s:"GE.US",    n:22,   e:148.00, ed:"15-Aug-2024", cv:3707,  pnl:451,  pp:13.9, stop:143.70,  tr:true},
    {s:"LLY.US",   n:6,    e:680.00, ed:"19-Jan-2024", cv:4680,  pnl:600,  pp:14.7, stop:711.20,  tr:true},
    {s:"ORCL.US",  n:28,   e:118.00, ed:"30-Oct-2024", cv:3598,  pnl:294,  pp:8.9,  stop:114.20,  tr:false},
    {s:"JPM.US",   n:18,   e:172.00, ed:"04-Sep-2024", cv:3564,  pnl:468,  pp:15.1, stop:163.40,  tr:false},
    {s:"BTC-EUR",  n:0.12, e:44200,  ed:"25-Jan-2024", cv:7536,  pnl:2232, pp:42.1, stop:52180,   tr:true},
    {s:"CAT.US",   n:10,   e:328.00, ed:"22-Oct-2024", cv:3650,  pnl:370,  pp:11.3, stop:319.20,  tr:false},
    {s:"ASML.AS",  n:7,    e:768.00, ed:"08-Nov-2024", cv:5810,  pnl:434,  pp:8.1,  stop:748.60,  tr:false},
  ];

  const scen = activeScen > 0 ? SCENARIOS[activeScen - 1] : null;
  const CBROWS = [
    ["Portfolio Drawdown","-7.2%","< -15%"],
    ["VIX Level","17.8","≥ 40"],
    ["Max Pairwise Correlation","0.62","> 0.85"],
    ["Top-3 Concentration","27.1%","> 30%"],
    ["Data Staleness","0 days","> 3 days"],
  ];

  const SharedExits = () => (
    <>
      <SecHead c={A.red}>Mandatory Exits (2) — Market Order at Open</SecHead>
      <div style={{overflowX:"auto", marginBottom:12}}>
        <table style={{width:"100%", borderCollapse:"collapse", minWidth:820}}>
          <thead><tr>
            {["Pri","Symbol","Reason","Shares","Entry Px","Curr Val","Unreal P&L","Curr Stop","Score","ADX","ATR%","Detail"]
              .map((h,i)=><th key={h} style={th(A.red, i<3?"left":"right")}>{h}</th>)}
          </tr></thead>
          <tbody>
            <tr style={{background:"#130404"}}>
              <td style={tdR("center",true)}><b style={{color:"#c8d8f0"}}>1</b></td>
              <td style={{...tdR("left",true),color:A.red,fontWeight:700}}>INTC.US</td>
              <td style={{...tdR("left",true),color:A.red,fontSize:8}}>STOP HIT</td>
              <td style={tdR("right",true)}>30</td>
              <td style={tdR("right",true)}>€48.00</td>
              <td style={tdR("right",true)}>€936.00</td>
              <td style={{...tdR("right",true),color:A.red,fontWeight:700}}>–€2,040</td>
              <td style={tdR("right",true)}>€31.50</td>
              <td style={{...tdR("right",true),color:A.red}}>–18.40</td>
              <td style={tdR("right",true)}>22.1</td>
              <td style={tdR("right",true)}>+4.2%</td>
              <td style={{...tdR("left",true),color:"#5a7090",fontSize:8}}>Price €31.20 closed below initial stop €31.50. Mandatory liquidation.</td>
            </tr>
            <tr>
              <td style={tdR("center")}><b style={{color:"#c8d8f0"}}>2</b></td>
              <td style={{...tdR("left"),color:A.amber,fontWeight:700}}>AMZN.US</td>
              <td style={{...tdR("left"),color:A.amber,fontSize:8}}>ROTATION EXIT</td>
              <td style={tdR()}>5</td>
              <td style={tdR()}>€182.00</td>
              <td style={tdR()}>€942.00</td>
              <td style={{...tdR(),color:A.green,fontWeight:700}}>+€320</td>
              <td style={tdR()}>€174.20</td>
              <td style={{...tdR(),color:A.green}}>+12.30</td>
              <td style={tdR()}>24.8</td>
              <td style={tdR()}>+2.1%</td>
              <td style={{...tdR("left"),color:"#5a7090",fontSize:8}}>Dropped from Top-14. Score fell from rank #11 to #18.</td>
            </tr>
          </tbody>
        </table>
      </div>

      <SecHead c={A.orange}>Rotation Exits (1) — Market Order at Close</SecHead>
      <div style={{overflowX:"auto", marginBottom:6}}>
        <table style={{width:"100%", borderCollapse:"collapse", minWidth:740}}>
          <thead><tr>
            {["Symbol","Reason","Held","Sell","Buy Px","Curr Px","Total Val","Unreal P&L","Entry Date","Score","ADX","ATR%"]
              .map((h,i)=><th key={h} style={th(A.orange, i<2?"left":"right")}>{h}</th>)}
          </tr></thead>
          <tbody>
            <tr>
              <td style={{...tdR("left"),color:A.amber,fontWeight:700}}>AMZN.US</td>
              <td style={{...tdR("left"),color:"#5a7090",fontSize:8}}>DROPPED FROM TOP-N</td>
              <td style={tdR()}>5</td><td style={tdR()}>5</td>
              <td style={tdR()}>€182.00</td><td style={tdR()}>€188.40</td>
              <td style={tdR()}>€942.00</td>
              <td style={{...tdR(),color:A.green,fontWeight:700}}>+€320</td>
              <td style={{...tdR("center"),color:"#5a7090"}}>22-Nov-2024</td>
              <td style={tdR()}>12.30</td><td style={tdR()}>24.8</td><td style={tdR()}>+2.1%</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p style={{fontFamily:"DM Sans",fontSize:9,fontStyle:"italic",color:"#5a7090",marginTop:4}}>
        ~ prefix on Curr Px = live close unavailable; derived from portfolio state. Verify before settlement.
      </p>
    </>
  );

  const HoldsTable = () => (
    <>
      <SecHead c={"#5a7090"}>Holds (10) — No Action Required</SecHead>
      <div style={{overflowX:"auto"}}>
        <table style={{width:"100%", borderCollapse:"collapse", minWidth:700}}>
          <thead><tr>
            {["Symbol","Shares","Entry Px","Entry Date","Curr Value","Unreal P&L","P&L %","Curr Stop","Stop Type"]
              .map((h,i)=><th key={h} style={th("#5a7090", i<1?"left":"right")}>{h}</th>)}
          </tr></thead>
          <tbody>{holds.map((h,i) => (
            <tr key={h.s}>
              <td style={{...tdR("left",i%2===0),color:A.cyan,fontWeight:600}}>{h.s}</td>
              <td style={tdR("right",i%2===0)}>{h.n}</td>
              <td style={tdR("right",i%2===0)}>€{fN(h.e,2)}</td>
              <td style={{...tdR("center",i%2===0),color:"#5a7090"}}>{h.ed}</td>
              <td style={tdR("right",i%2===0)}>€{fN(h.cv,0)}</td>
              <td style={{...tdR("right",i%2===0),color:A.green,fontWeight:700}}>+€{fN(h.pnl,0)}</td>
              <td style={{...tdR("right",i%2===0),color:A.green,fontWeight:700}}>+{h.pp.toFixed(1)}%</td>
              <td style={{...tdR("right",i%2===0),color:A.red}}>€{fN(h.stop,2)}</td>
              <td style={tdR("center",i%2===0)}>
                <Tag c={h.tr?A.green:"#5a7090"} sm>{h.tr?"Trailing":"Initial"}</Tag>
              </td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </>
  );

  const CBTable = () => (
    <>
      <SecHead c={A.green}>Circuit Breaker Status — All Clear</SecHead>
      <table style={{width:"100%", borderCollapse:"collapse"}}>
        <thead><tr>
          {["Breaker","Current","Threshold","Status"].map((h,i)=><th key={h} style={th(A.green, i<2?"left":"right")}>{h}</th>)}
        </tr></thead>
        <tbody>{CBROWS.map(([n,v,t],i) => (
          <tr key={n}>
            <td style={{...tdR("left",i%2===0),color:"#c8d8f0",fontWeight:500}}>{n}</td>
            <td style={{...tdR("left",i%2===0),color:A.cyan,fontWeight:700}}>{v}</td>
            <td style={{...tdR("right",i%2===0),color:"#5a7090"}}>threshold: {t}</td>
            <td style={tdR("right",i%2===0)}><Tag c={A.green} sm>✓ CLEAR</Tag></td>
          </tr>
        ))}</tbody>
      </table>
    </>
  );

  return (
    <div className="anim" style={{display:"flex",flexDirection:"column",gap:12}}>

      {/* ── Page header ── */}
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
        <div style={{display:"flex",alignItems:"center",gap:8}}>
          <SectionBar/>
          <span style={{fontFamily:"Syne",fontSize:13,fontWeight:700,color:A.text,
            letterSpacing:"0.05em",textTransform:"uppercase"}}>
            Script 12 — Monthly Rebalancing Recommendations
          </span>
        </div>
        <div style={{display:"flex",gap:6,alignItems:"center"}}>
          <Tag c={A.green}>✓ READY FOR REVIEW</Tag>
          <Tag c={A.cyan}>2026-03-01 07:31 · v3.2</Tag>
        </div>
      </div>

      {/* ── Scenario tab bar ── */}
      <div style={{display:"flex",gap:0,background:CARD,border:`1px solid ${BDR}`,borderRadius:8,overflow:"hidden"}}>
        {[{id:0,label:"COMPARISON",color:A.amber},...SCENARIOS.map(s=>({id:s.id,label:s.shortName,color:s.color}))].map(t=>(
          <button key={t.id} onClick={()=>setActiveScen(t.id)}
            style={{flex:1,fontFamily:"Syne",fontSize:10,fontWeight:700,letterSpacing:"0.05em",
              padding:"12px 8px",background:activeScen===t.id?`${t.color}14`:"transparent",
              border:"none",borderBottom:activeScen===t.id?`2px solid ${t.color}`:"2px solid transparent",
              color:activeScen===t.id?t.color:A.dim,cursor:"pointer",whiteSpace:"nowrap",textTransform:"uppercase"}}>
            {t.label}
          </button>
        ))}
      </div>

      {/* ══════════════════════════════════════════
          COMPARISON VIEW
      ══════════════════════════════════════════ */}
      {activeScen === 0 && (
        <div style={{display:"flex",flexDirection:"column",gap:12}}>

          {/* Meta strip */}
          <div style={{display:"flex",gap:8,flexWrap:"wrap"}}>
            {[["Period","2026-03",A.amber],["Execution","04-Mar-2026","#c8d8f0"],
              ["Equity","€50,000",A.green],["Max Positions","14","#c8d8f0"],
              ["Generated","2026-03-01 07:31","#5a7090"],["Architecture","v3.2",A.cyan]
            ].map(([l,v,c])=>(
              <div key={l} style={{padding:"8px 14px",background:CARD,border:`1px solid ${BDR}`,borderRadius:6}}>
                <div style={{fontFamily:"DM Sans",fontSize:8,color:"#5a7090",textTransform:"uppercase",
                  letterSpacing:"0.06em",marginBottom:3}}>{l}</div>
                <div style={{fontFamily:"JetBrains Mono",fontSize:12,fontWeight:600,color:c}}>{v}</div>
              </div>
            ))}
          </div>

          {/* Info banner */}
          <div style={{display:"flex",gap:10,alignItems:"center",padding:"10px 14px",
            background:`${A.blue}0a`,border:`1px solid ${A.blue}25`,borderRadius:7}}>
            <span style={{color:A.blue,fontSize:14,flexShrink:0}}>ℹ</span>
            <span style={{fontFamily:"DM Sans",fontSize:11,color:"#5a7090",lineHeight:1.5}}>
              Exits and holds are <b style={{color:"#c8d8f0"}}>identical</b> across all 3 scenarios — only new entries differ.
              Click a scenario card or tab to view its full execution plan.
            </span>
          </div>

          {/* 3 scenario cards */}
          <div style={{fontFamily:"Syne",fontSize:11,fontWeight:700,color:A.amber,
            letterSpacing:"0.08em",textTransform:"uppercase",marginBottom:2}}>
            Three-Scenario Comparison
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:12}}>
            {SCENARIOS.map(s => (
              <div key={s.id} onClick={()=>setActiveScen(s.id)}
                style={{background:CARD,border:`1px solid ${s.color}35`,borderRadius:8,
                  overflow:"hidden",cursor:"pointer",transition:"border-color 0.15s"}}>
                {/* Card header */}
                <div style={{background:`${s.color}18`,borderBottom:`1px solid ${s.color}35`,
                  padding:"11px 14px",display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                  <span style={{fontFamily:"Syne",fontSize:11,fontWeight:700,color:s.color,
                    letterSpacing:"0.04em"}}>{s.name}</span>
                  <span style={{fontFamily:"JetBrains Mono",fontSize:9,color:s.color,opacity:0.7}}>→ Full plan</span>
                </div>
                <div style={{padding:"12px 14px"}}>
                  <p style={{fontFamily:"DM Sans",fontSize:10,color:"#5a7090",fontStyle:"italic",
                    marginBottom:10,lineHeight:1.55}}>{s.philosophy}</p>
                  <p style={{fontFamily:"DM Sans",fontSize:9,color:"#5a7090",marginBottom:10}}>
                    <span style={{color:"#c8d8f0",fontWeight:600}}>Pool: </span>{s.candidatePool}
                  </p>
                  {/* Count grid */}
                  <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:5,marginBottom:12}}>
                    {[["New Entries",s.newCount,A.green],["Mand. Exits",s.mandCount,A.red],
                      ["Rot. Exits",s.rotCount,A.orange],["Holds",s.holdCount,"#5a7090"]].map(([l,v,c])=>(
                      <div key={l} style={{display:"flex",justifyContent:"space-between",padding:"5px 8px",
                        background:CARD2,borderRadius:5,border:`1px solid ${c}20`}}>
                        <span style={{fontFamily:"DM Sans",fontSize:9,color:"#5a7090"}}>{l}</span>
                        <span style={{fontFamily:"JetBrains Mono",fontSize:11,fontWeight:700,color:c}}>{v}</span>
                      </div>
                    ))}
                  </div>
                  {/* Entries list */}
                  <div style={{marginBottom:10}}>
                    <div style={{fontFamily:"DM Sans",fontSize:8,color:"#5a7090",marginBottom:5,
                      textTransform:"uppercase",letterSpacing:"0.07em"}}>New Entries</div>
                    {s.entries.map(e=>(
                      <div key={e.sy} style={{display:"flex",justifyContent:"space-between",
                        alignItems:"center",padding:"5px 0",borderBottom:`1px solid ${BDR}`}}>
                        <span style={{fontFamily:"JetBrains Mono",fontSize:10,color:A.cyan,fontWeight:600}}>{e.sy}</span>
                        <span style={{fontFamily:"DM Sans",fontSize:9,color:"#5a7090"}}>{e.sc}</span>
                        <span style={{fontFamily:"JetBrains Mono",fontSize:10,color:A.green,fontWeight:700}}>+{e.ms.toFixed(2)}</span>
                      </div>
                    ))}
                  </div>
                  {/* Capital footer */}
                  <div style={{display:"flex",justifyContent:"space-between",padding:"7px 10px",
                    background:CARD2,borderRadius:5,border:`1px solid ${BDR}`}}>
                    <span style={{fontFamily:"JetBrains Mono",fontSize:10,color:"#5a7090"}}>
                      Capital: <span style={{color:"#c8d8f0"}}>€{s.entryRequired.toLocaleString()}</span>
                    </span>
                    <span style={{fontFamily:"JetBrains Mono",fontSize:10,
                      color:s.cashAfter<6?A.red:A.green,fontWeight:700}}>Cash: {s.cashAfter}%</span>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Side-by-side comparison table */}
          <DCard title="Side-by-Side Metrics" c={A.amber}>
            <div style={{overflowX:"auto"}}>
              <table style={{width:"100%",borderCollapse:"collapse"}}>
                <thead><tr>
                  <th style={{...th(A.amber,"left"),width:"26%"}}>Metric</th>
                  {SCENARIOS.map(s=><th key={s.id} style={th(s.color)}>{s.name.split("—")[1]?.trim()}</th>)}
                </tr></thead>
                <tbody>
                  {[
                    ["Concentration cap","None","≤4/exch · ≤3/sec","≤5/exch · ≤4/sec"],
                    ["New positions","3 entries","3 entries","3 entries"],
                    ["Capital deployed","€21,163","€22,533","€21,163"],
                    ["Cash remaining","€3,600 · 7.2%","€2,950 · 5.9%","€3,600 · 7.2%"],
                    ["Tech sector wt","41.8% ⚠","28.4% ✓","35.2% ⚠"],
                    ["Best for","Max momentum","Max diversification","Balanced approach"],
                  ].map(([m,...vals],i) => (
                    <tr key={m}>
                      <td style={{...tdR("left",i%2===0),color:"#5a7090",fontWeight:500}}>{m}</td>
                      {vals.map((v,si)=>(
                        <td key={si} style={{...tdR("center",i%2===0),
                          color:v.includes("⚠")?A.red:v.includes("✓")?A.green:SCENARIOS[si].color,
                          fontWeight:600}}>{v}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DCard>

          {/* Shared sections */}
          <DCard c={A.green}>
            <CBTable/>
          </DCard>
          <DCard c={A.red}>
            <SharedExits/>
          </DCard>
          <DCard c={"#5a7090"}>
            <HoldsTable/>
          </DCard>

          {/* Footer */}
          <div style={{display:"flex",alignItems:"center",justifyContent:"center",gap:10,padding:"14px",
            background:`${A.red}10`,border:`1px solid ${A.red}40`,borderRadius:8}}>
            <span style={{fontFamily:"Syne",fontSize:13,fontWeight:800,color:A.red,letterSpacing:"0.04em"}}>
              ⚠  SELECT A SCENARIO TAB — THEN REVIEW ITS FULL EXECUTION PLAN
            </span>
          </div>
        </div>
      )}

      {/* ══════════════════════════════════════════
          INDIVIDUAL SCENARIO VIEW
      ══════════════════════════════════════════ */}
      {activeScen > 0 && scen && (
        <div style={{display:"flex",flexDirection:"column",gap:12}}>

          {/* Scenario hero header */}
          <div style={{background:CARD,border:`1px solid ${scen.color}40`,borderRadius:8,overflow:"hidden"}}>
            <div style={{background:`${scen.color}18`,borderBottom:`1px solid ${scen.color}35`,padding:"14px 18px"}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:5}}>
                <span style={{fontFamily:"Syne",fontSize:18,fontWeight:800,color:scen.color,
                  letterSpacing:"0.04em"}}>{scen.name}</span>
                <Tag c={A.green}>✓ READY FOR REVIEW</Tag>
              </div>
              <p style={{fontFamily:"DM Sans",fontSize:11,color:"#5a7090",fontStyle:"italic",
                marginBottom:4,lineHeight:1.5}}>{scen.philosophy}</p>
              <p style={{fontFamily:"DM Sans",fontSize:10,color:"#5a7090"}}>
                <span style={{color:"#c8d8f0"}}>Pool:</span> {scen.candidatePool}
              </p>
            </div>
            <div style={{padding:"12px 18px"}}>
              <div style={{display:"grid",gridTemplateColumns:"repeat(6,1fr)",gap:8}}>
                {[["Period","2026-03",A.amber],["Execution","04-Mar-2026","#c8d8f0"],
                  ["Equity","€50,000",A.green],["Max Pos","14","#c8d8f0"],
                  ["Generated","2026-03-01 07:31","#5a7090"],["Architecture","v3.2",A.cyan]
                ].map(([l,v,c])=>(
                  <div key={l} style={{padding:"7px 10px",background:CARD2,borderRadius:5,
                    border:`1px solid ${BDR2}`}}>
                    <div style={{fontFamily:"DM Sans",fontSize:8,color:"#5a7090",
                      textTransform:"uppercase",letterSpacing:"0.06em",marginBottom:2}}>{l}</div>
                    <div style={{fontFamily:"JetBrains Mono",fontSize:11,fontWeight:600,color:c}}>{v}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Action tiles */}
          <div style={{display:"flex",gap:10}}>
            <ActionTile label="MANDATORY EXITS" count={scen.mandCount} c={A.red}/>
            <ActionTile label="ROTATION EXITS"  count={scen.rotCount}  c={A.orange}/>
            <ActionTile label="NEW ENTRIES"      count={scen.newCount}  c={scen.color}/>
            <ActionTile label="HOLDS"            count={scen.holdCount} c={"#5a7090"}/>
          </div>

          <p style={{fontFamily:"DM Sans",fontSize:10,fontStyle:"italic",color:"#5a7090"}}>
            System-generated recommendations. ALL require human review and approval before execution.
          </p>

          {/* Circuit breakers */}
          <DCard c={A.green}><CBTable/></DCard>

          {/* Shared exits */}
          <DCard c={A.red}><SharedExits/></DCard>

          {/* New Entries */}
          <DCard c={scen.color}>
            <SecHead c={scen.color}>
              New Entries ({scen.newCount}) — Limit Order: Close + 0.5% &nbsp;[{scen.name}]
            </SecHead>
            <p style={{fontFamily:"DM Sans",fontSize:9,color:"#5a7090",marginBottom:10,fontStyle:"italic",lineHeight:1.5}}>
              Place limit buy orders at Limit Price on 04-Mar-2026.
              Verify: SMA50 &gt; SMA200, Close &gt; SMA50, ADX ≥ 20. Set initial stop immediately after fill.
            </p>
            <div style={{overflowX:"auto"}}>
              <table style={{width:"100%",borderCollapse:"collapse",minWidth:900}}>
                <thead><tr>
                  {["Rank","Symbol","Name","Exchange","Sector","Shares","Limit Px","Pos (€)","Pos%","Init Stop","Stop Dst%","ADX","ATR%","Score"]
                    .map((h,i)=><th key={h} style={th(scen.color,i<5?"left":"right")}>{h}</th>)}
                </tr></thead>
                <tbody>{scen.entries.map((e,i) => (
                  <tr key={e.sy}>
                    <td style={tdR("center",i%2===0)}>{e.rk}</td>
                    <td style={{...tdR("left",i%2===0),color:A.cyan,fontWeight:700}}>{e.sy}</td>
                    <td style={{...tdR("left",i%2===0),color:"#c8d8f0",fontSize:8}}>{e.nm}</td>
                    <td style={{...tdR("center",i%2===0),color:"#5a7090"}}>{e.ex}</td>
                    <td style={{...tdR("left",i%2===0),color:"#5a7090",fontSize:8}}>{e.sc}</td>
                    <td style={tdR("right",i%2===0)}>{e.sh}</td>
                    <td style={{...tdR("right",i%2===0),color:"#c8d8f0",fontWeight:700}}>€{fN(e.lp,2)}</td>
                    <td style={tdR("right",i%2===0)}>€{fN(e.pv,0)}</td>
                    <td style={{...tdR("right",i%2===0),color:scen.color}}>+{e.pp.toFixed(1)}%</td>
                    <td style={{...tdR("right",i%2===0),color:A.red}}>€{fN(e.st,2)}</td>
                    <td style={{...tdR("right",i%2===0),color:A.red}}>–{e.sd.toFixed(1)}%</td>
                    <td style={{...tdR("right",i%2===0),color:e.adx>30?A.green:A.amber}}>{e.adx.toFixed(1)}</td>
                    <td style={tdR("right",i%2===0)}>+{e.atr.toFixed(1)}%</td>
                    <td style={{...tdR("right",i%2===0),color:A.green,fontWeight:700}}>+{e.ms.toFixed(4)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </DCard>

          {/* Holds */}
          <DCard c={"#5a7090"}><HoldsTable/></DCard>

          {/* Capital summary – two columns */}
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:12}}>
            <DCard title="Capital Summary" c={A.blue}>
              <table style={{width:"100%",borderCollapse:"collapse"}}>
                <tbody>
                  {kvRow("Account Equity","€50,000.00",A.green)}
                  {kvRow("Holds Value","€42,800.00","#c8d8f0")}
                  {kvRow("Capital Freed by Exits",`€${scen.exitProceeds.toLocaleString()}`,A.amber)}
                  {kvRow(`Capital Required (${scen.entries.length} entries)`,`€${scen.entryRequired.toLocaleString()}`,A.orange)}
                  {kvRow("Total Deployed (after)",`€${(50000-scen.cashEur).toLocaleString()}  (+${(100-scen.cashAfter).toFixed(1)}%)`,"#c8d8f0")}
                  {kvRow("Cash Remaining",`€${scen.cashEur.toLocaleString()}  (${scen.cashAfter}%)`,scen.cashAfter<6?A.red:A.green)}
                </tbody>
              </table>
            </DCard>
            <DCard title="Asset Class Breakdown" c={A.blue}>
              <table style={{width:"100%",borderCollapse:"collapse"}}>
                <thead><tr>
                  {["Asset Class","Value","% Equity","Pos"].map((h,i)=><th key={h} style={th(A.blue,i===0?"left":"right")}>{h}</th>)}
                </tr></thead>
                <tbody>{scen.assetBreakdown.map(([ac,v,p,n],i)=>(
                  <tr key={ac}>
                    <td style={{...tdR("left",i%2===0),color:"#c8d8f0",fontWeight:600}}>{ac}</td>
                    <td style={{...tdR("right",i%2===0),color:A.cyan}}>{v}</td>
                    <td style={{...tdR("right",i%2===0),color:A.amber}}>{p}</td>
                    <td style={{...tdR("center",i%2===0),color:"#5a7090"}}>{n}</td>
                  </tr>
                ))}</tbody>
              </table>
            </DCard>
          </div>

          {/* Execution Checklist */}
          <DCard title="Execution Checklist" c={A.amber}>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:0}}>
              {[
                "Verify all prices are from 2026-03-01 close",
                "Confirm INTC.US stop was breached at €31.50",
                "Execute INTC.US market SELL at 2026-03-04 open",
                "Execute AMZN.US market SELL at 2026-03-04 close",
                `Place ${scen.entries[0].sy} limit buy @ €${fN(scen.entries[0].lp,2)}`,
                `Place ${scen.entries[1].sy} limit buy @ €${fN(scen.entries[1].lp,2)}`,
                `Place ${scen.entries[2].sy} limit buy @ €${fN(scen.entries[2].lp,2)}`,
                "Update trailing stops for HOLD positions Fri 2026-03-07",
                "Confirm post-trade cash ≥ 5%",
                "Archive report with execution timestamp",
              ].map((item,i) => (
                <div key={i} style={{display:"flex",alignItems:"flex-start",gap:9,padding:"7px 10px",
                  borderBottom:`1px solid ${BDR}`,background:i%2===0?CARD:CARD2}}>
                  <span style={{fontFamily:"JetBrains Mono",fontSize:11,color:A.amber,
                    flexShrink:0,marginTop:1}}>☐</span>
                  <span style={{fontFamily:"DM Sans",fontSize:10,color:"#c8d8f0",lineHeight:1.45}}>
                    {i+1}. {item}
                  </span>
                </div>
              ))}
            </div>
          </DCard>

          {/* Warnings */}
          {scen.warnings.map((w,i) => (
            <div key={i} style={{display:"flex",gap:12,alignItems:"flex-start",padding:"11px 16px",
              background:`${A.amber}0d`,border:`1px solid ${A.amber}35`,borderRadius:8}}>
              <span style={{color:A.amber,fontSize:16,flexShrink:0,lineHeight:1}}>⚠</span>
              <span style={{fontFamily:"DM Sans",fontSize:11,color:"#c8d8f0",lineHeight:1.55}}>{w}</span>
            </div>
          ))}

          {/* Approval footer */}
          <div style={{display:"flex",alignItems:"center",justifyContent:"center",gap:10,padding:"14px",
            background:`${A.red}10`,border:`1px solid ${A.red}40`,borderRadius:8}}>
            <span style={{fontFamily:"Syne",fontSize:13,fontWeight:800,color:A.red,letterSpacing:"0.04em"}}>
              ⚠  HUMAN APPROVAL REQUIRED BEFORE PLACING ANY ORDERS
            </span>
          </div>

          <p style={{fontFamily:"JetBrains Mono",fontSize:9,color:"#5a7090",textAlign:"center"}}>
            Execution: 04-Mar-2026  ·  Generated: 2026-03-01 07:31:44  ·  Architecture: v3.2  ·  {scen.name}
          </p>
        </div>
      )}
    </div>
  );
};


// ═══════════════════════════════════════════════════════════════════════════════
// TAB: TECH CHARTS  (Script 15 — with asset/exchange/portfolio/rec filters + search)
// ═══════════════════════════════════════════════════════════════════════════════
const ChartsTab = () => {
  const [sel,   setSel]   = useState(SIGNALS[0].symbol);
  const [lb,    setLb]    = useState(120);
  const [search,setSearch]= useState("");
  const [fAsset,setFAsset]= useState("ALL");  // asset class filter
  const [fExch, setFExch] = useState("ALL");  // exchange filter
  const [fPort, setFPort] = useState(false);  // portfolio-only filter
  const [fRec,  setFRec]  = useState("ALL");  // recommendation scenario filter

  // Unique filter values
  const assetClasses = ["ALL", ...new Set(SIGNALS.map(s => s.assetClass))];
  const exchanges    = ["ALL", ...new Set(SIGNALS.map(s => s.exch))];

  // Rec symbols for each scenario
  const recMap = {"ALL":null, "S1":REC_SYMBOLS.s1, "S2":REC_SYMBOLS.s2, "S3":REC_SYMBOLS.s3, "ANY":ALL_REC_SYMBOLS};

  // Apply all filters
  const filtered = useMemo(() => {
    return SIGNALS.filter(s => {
      if (search) {
        const q = search.toLowerCase();
        if (!s.symbol.toLowerCase().includes(q) && !s.name.toLowerCase().includes(q)) return false;
      }
      if (fAsset !== "ALL" && s.assetClass !== fAsset) return false;
      if (fExch  !== "ALL" && s.exch  !== fExch)       return false;
      if (fPort  && !PORTFOLIO.some(p => p.symbol === s.symbol)) return false;
      if (fRec   !== "ALL") {
        const allowed = recMap[fRec];
        if (allowed && !allowed.includes(s.symbol)) return false;
      }
      return true;
    });
  }, [search, fAsset, fExch, fPort, fRec]);

  // Auto-select first visible if current selection is filtered out
  useEffect(() => {
    if (!filtered.some(s => s.symbol === sel) && filtered.length > 0) setSel(filtered[0].symbol);
  }, [filtered]);

  const sig = SIGNALS.find(s => s.symbol === sel) || SIGNALS[0];
  const pos = PORTFOLIO.find(p => p.symbol === sel);
  const idx = SIGNALS.findIndex(s => s.symbol === sel);
  const series = useMemo(() => genSeries(sel, sig.sma200*0.78, 1+idx*0.04, lb+50).slice(-lb), [sel, lb]);
  const qualPass = sig.sma50>sig.sma200 && sig.close>sig.sma50 && sig.adx>20;
  const badge = (pass) => ({display:"inline-flex",alignItems:"center",gap:5,padding:"4px 10px",borderRadius:4,background:pass?"#052e16":"#2d0808",border:`1px solid ${pass?"#166534":"#7f1d1d"}`,fontSize:10,color:pass?"#4ade80":"#f87171"});

  const FilterBtn = ({active, onClick, children, c}) => (
    <button onClick={onClick} style={{fontFamily:"JetBrains Mono",fontSize:9,padding:"3px 8px",borderRadius:3,cursor:"pointer",
      background:active?`${c||"#F59E0B"}28`:"transparent",border:`1px solid ${active?(c||"#F59E0B"):"#1e293b"}`,
      color:active?(c||"#F59E0B"):"#64748b",whiteSpace:"nowrap"}}>
      {children}
    </button>
  );

  const activeFiltersCount = (fAsset!=="ALL"?1:0)+(fExch!=="ALL"?1:0)+(fPort?1:0)+(fRec!=="ALL"?1:0)+(search?1:0);

  return (
    <div className="anim" style={{display:"flex",gap:0,background:CH.header,borderRadius:8,overflow:"hidden",border:`1px solid ${A.border}`,height:"calc(100vh - 160px)",minHeight:600}}>

      {/* ── Sidebar ── */}
      <div style={{width:232,background:CH.sidebar,flexShrink:0,display:"flex",flexDirection:"column",borderRight:"1px solid #0f172a"}}>

        {/* Sidebar header: title + lookback */}
        <div style={{background:CH.header,padding:"10px 12px",borderBottom:"1px solid #1e293b",flexShrink:0}}>
          <div style={{fontFamily:"DM Sans",fontSize:10,fontWeight:600,color:"#94a3b8",letterSpacing:"0.08em",textTransform:"uppercase",marginBottom:7}}>Technical Charts · Script 15</div>
          <div style={{display:"flex",gap:4}}>
            {[60,120,200].map(n => (
              <button key={n} onClick={()=>setLb(n)} style={{flex:1,fontFamily:"JetBrains Mono",fontSize:9,padding:"3px 0",borderRadius:3,cursor:"pointer",background:lb===n?"#334155":"transparent",border:`1px solid ${lb===n?"#475569":"#1e293b"}`,color:lb===n?"#e2e8f0":"#64748b"}}>{n}d</button>
            ))}
          </div>
        </div>

        {/* ── Filter panel ── */}
        <div style={{background:"#0c1526",borderBottom:"1px solid #1e293b",padding:"9px 12px",flexShrink:0}}>

          {/* Search bar */}
          <div style={{position:"relative",marginBottom:7}}>
            <span style={{position:"absolute",left:7,top:"50%",transform:"translateY(-50%)",color:"#475569",fontSize:11,pointerEvents:"none"}}>🔍</span>
            <input
              value={search}
              onChange={e=>setSearch(e.target.value)}
              placeholder="Symbol or company…"
              style={{width:"100%",fontFamily:"JetBrains Mono",fontSize:10,padding:"5px 8px 5px 24px",background:"#0f172a",border:`1px solid ${search?"#F59E0B":"#1e293b"}`,borderRadius:4,color:"#e2e8f0",outline:"none"}}
            />
            {search && (
              <button onClick={()=>setSearch("")} style={{position:"absolute",right:6,top:"50%",transform:"translateY(-50%)",background:"none",border:"none",color:"#64748b",cursor:"pointer",fontSize:12,lineHeight:1}}>✕</button>
            )}
          </div>

          {/* Asset class filter */}
          <div style={{marginBottom:6}}>
            <div style={{fontFamily:"DM Sans",fontSize:8,color:"#475569",textTransform:"uppercase",letterSpacing:"0.08em",marginBottom:4}}>Asset Class</div>
            <div style={{display:"flex",flexWrap:"wrap",gap:3}}>
              {assetClasses.map(a => (
                <FilterBtn key={a} active={fAsset===a} onClick={()=>setFAsset(a)} c="#4FC3F7">
                  {a==="ALL"?"All":a==="Cryptocurrency"?"Crypto":a.replace(" Stock","").replace(" ","·")}
                </FilterBtn>
              ))}
            </div>
          </div>

          {/* Exchange filter */}
          <div style={{marginBottom:6}}>
            <div style={{fontFamily:"DM Sans",fontSize:8,color:"#475569",textTransform:"uppercase",letterSpacing:"0.08em",marginBottom:4}}>Exchange</div>
            <div style={{display:"flex",flexWrap:"wrap",gap:3}}>
              {exchanges.map(e => (
                <FilterBtn key={e} active={fExch===e} onClick={()=>setFExch(e)} c="#F59E0B">
                  {e}
                </FilterBtn>
              ))}
            </div>
          </div>

          {/* Portfolio filter */}
          <div style={{marginBottom:6}}>
            <div style={{fontFamily:"DM Sans",fontSize:8,color:"#475569",textTransform:"uppercase",letterSpacing:"0.08em",marginBottom:4}}>Portfolio</div>
            <div style={{display:"flex",gap:3}}>
              <FilterBtn active={!fPort} onClick={()=>setFPort(false)} c="#69F0AE">All</FilterBtn>
              <FilterBtn active={fPort}  onClick={()=>setFPort(true)}  c="#69F0AE">Held positions only</FilterBtn>
            </div>
          </div>

          {/* Rec scenario filter */}
          <div style={{marginBottom:4}}>
            <div style={{fontFamily:"DM Sans",fontSize:8,color:"#475569",textTransform:"uppercase",letterSpacing:"0.08em",marginBottom:4}}>Recommendations</div>
            <div style={{display:"flex",flexWrap:"wrap",gap:3}}>
              {["ALL","S1","S2","S3","ANY"].map(r => (
                <FilterBtn key={r} active={fRec===r} onClick={()=>setFRec(r)} c="#CE93D8">
                  {r==="ALL"?"All":r==="ANY"?"Any rec":r}
                </FilterBtn>
              ))}
            </div>
          </div>

          {/* Active filter summary */}
          {activeFiltersCount > 0 && (
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginTop:5,paddingTop:5,borderTop:"1px solid #1e293b"}}>
              <span style={{fontFamily:"JetBrains Mono",fontSize:9,color:"#94a3b8"}}>{filtered.length} of {SIGNALS.length} symbols</span>
              <button onClick={()=>{setSearch("");setFAsset("ALL");setFExch("ALL");setFPort(false);setFRec("ALL");}} style={{fontFamily:"JetBrains Mono",fontSize:8,padding:"2px 7px",borderRadius:3,cursor:"pointer",background:"#7f1d1d",border:"1px solid #991b1b",color:"#fca5a5"}}>Clear all</button>
            </div>
          )}
        </div>

        {/* Symbol list */}
        <div style={{overflowY:"auto",flex:1}}>
          {filtered.length === 0 && (
            <div style={{padding:"20px 14px",textAlign:"center",color:"#475569",fontFamily:"DM Sans",fontSize:10}}>No symbols match filters</div>
          )}
          {filtered.map(s => {
            const held    = PORTFOLIO.some(p => p.symbol === s.symbol);
            const inRecS1 = REC_SYMBOLS.s1.includes(s.symbol);
            const inRecS2 = REC_SYMBOLS.s2.includes(s.symbol);
            const inRecS3 = REC_SYMBOLS.s3.includes(s.symbol);
            const anyRec  = inRecS1 || inRecS2 || inRecS3;
            const active  = s.symbol === sel;
            return (
              <div key={s.symbol} onClick={()=>setSel(s.symbol)} style={{padding:"8px 12px",cursor:"pointer",background:active?"#334155":"transparent",borderBottom:"1px solid #1e293b",borderLeft:active?"3px solid #F59E0B":"3px solid transparent"}}>
                <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:2}}>
                  <code style={{fontFamily:"JetBrains Mono",fontSize:11,color:active?"#F59E0B":"#94a3b8"}}>{s.symbol}</code>
                  <span style={{fontFamily:"JetBrains Mono",fontSize:9,color:s.score>25?"#4ade80":s.score>15?"#fbbf24":"#94a3b8"}}>+{s.score.toFixed(1)}</span>
                </div>
                <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:3}}>
                  <span style={{fontFamily:"DM Sans",fontSize:9,color:"#64748b"}}>{s.name.length>20?s.name.slice(0,19)+"…":s.name}</span>
                  <span style={{fontFamily:"JetBrains Mono",fontSize:8,color:s.adx>25?"#a3e635":"#fbbf24"}}>{s.adx.toFixed(0)} ADX</span>
                </div>
                {/* Badges row */}
                <div style={{display:"flex",gap:3,flexWrap:"wrap"}}>
                  <span style={{fontFamily:"JetBrains Mono",fontSize:7,padding:"1px 4px",borderRadius:2,background:"#1e293b",border:"1px solid #334155",color:"#64748b"}}>{s.exch}</span>
                  {held && <span style={{fontFamily:"JetBrains Mono",fontSize:7,padding:"1px 4px",borderRadius:2,background:"#052e16",border:"1px solid #166534",color:"#4ade80"}}>HELD</span>}
                  {inRecS1 && <span style={{fontFamily:"JetBrains Mono",fontSize:7,padding:"1px 4px",borderRadius:2,background:"#1e3a8a",border:"1px solid #2563EB",color:"#93c5fd"}}>S1</span>}
                  {inRecS2 && <span style={{fontFamily:"JetBrains Mono",fontSize:7,padding:"1px 4px",borderRadius:2,background:"#064e3b",border:"1px solid #059669",color:"#6ee7b7"}}>S2</span>}
                  {inRecS3 && <span style={{fontFamily:"JetBrains Mono",fontSize:7,padding:"1px 4px",borderRadius:2,background:"#4c1d95",border:"1px solid #7C3AED",color:"#c4b5fd"}}>S3</span>}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Main chart area ── */}
      <div style={{flex:1,background:"#0b1120",display:"flex",flexDirection:"column",minWidth:0,overflow:"hidden"}}>
        {/* Chart header */}
        <div style={{background:CH.header,padding:"12px 18px",borderBottom:"1px solid #1e293b",flexShrink:0}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",flexWrap:"wrap",gap:8}}>
            <div>
              <span style={{fontFamily:"JetBrains Mono",fontSize:19,fontWeight:700,color:"#e2e8f0"}}>{sig.symbol}</span>
              <span style={{fontFamily:"DM Sans",fontSize:11,color:"#64748b",marginLeft:10}}>{sig.name} · {sig.exch} · {sig.assetClass}</span>
            </div>
            <div style={{display:"flex",gap:5,flexWrap:"wrap"}}>
              {[["Close",fN(sig.close,2),"#e2e8f0"],["SMA50",fN(sig.sma50,0),"#F59E0B"],["SMA200",fN(sig.sma200,0),"#EF4444"],["ADX",sig.adx.toFixed(1),sig.adx>25?"#4ade80":"#fbbf24"],["ATR%",sig.atrP.toFixed(1)+"%","#F97316"],["Score","+"+sig.score.toFixed(1),"#a78bfa"]].map(([l,v,c]) => (
                <div key={l} style={{display:"flex",flexDirection:"column",alignItems:"center",padding:"3px 8px",background:"#1e293b",borderRadius:4,border:"1px solid #334155"}}>
                  <span style={{fontFamily:"DM Sans",fontSize:7,color:"#64748b",textTransform:"uppercase",marginBottom:1}}>{l}</span>
                  <span style={{fontFamily:"JetBrains Mono",fontSize:11,fontWeight:600,color:c}}>{v}</span>
                </div>
              ))}
            </div>
          </div>
          <div style={{display:"flex",gap:7,marginTop:8,flexWrap:"wrap"}}>
            {[["SMA50 > SMA200",sig.sma50>sig.sma200,"Golden Cross"],["Close > SMA50",sig.close>sig.sma50,"Price in trend"],["ADX > 20",sig.adx>20,"Trend strength"]].map(([rule,pass,lbl]) => (
              <div key={rule} style={badge(pass)}><span>{pass?"✓":"✗"}</span><span><b style={{marginRight:4}}>{rule}</b><span style={{opacity:0.7,fontSize:9}}>{lbl}</span></span></div>
            ))}
            <div style={{...badge(qualPass),marginLeft:4}}>
              <b>{qualPass?"TREND QUALIFIED":"DISQUALIFIED"}</b>
              <span style={{opacity:0.7,fontSize:9,marginLeft:4}}>Rank #{sig.rank} · Score +{sig.score.toFixed(1)}</span>
            </div>
          </div>
        </div>

        {/* Charts */}
        <div style={{flex:1,padding:"10px 14px",display:"flex",flexDirection:"column",gap:8,overflowY:"auto",background:"#0b1120"}}>
          {/* Price + SMAs */}
          <div style={{background:"#0d1117",border:"1px solid #1e293b",borderRadius:5,padding:"9px 12px 7px"}}>
            <div style={{display:"flex",justifyContent:"space-between",marginBottom:5}}>
              <span style={{fontFamily:"DM Sans",fontSize:10,fontWeight:600,color:"#94a3b8"}}>PRICE · SMA50 · SMA200{pos?` · STOP €${fN(pos.stop,2)}`:""}</span>
              <div style={{display:"flex",gap:12}}>
                {[["── Price","#2563EB"],["── SMA50","#F59E0B"],["── SMA200","#EF4444"],...(pos?[["── Stop","#DC2626"]]:[])]
                  .map(([l,c]) => <span key={l} style={{fontFamily:"DM Sans",fontSize:9,color:c}}>{l}</span>)}
              </div>
            </div>
            <ResponsiveContainer width="100%" height={195}>
              <ComposedChart data={series} margin={{top:4,right:10,left:6,bottom:0}}>
                <CartesianGrid stroke="#1e293b" vertical={false} opacity={0.8}/>
                <XAxis dataKey="date" tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:"#475569"}} interval={Math.floor(lb/8)}/>
                <YAxis domain={["auto","auto"]} tick={{fontFamily:"JetBrains Mono",fontSize:8,fill:"#475569"}} tickFormatter={v=>v>=1000?`${(v/1000).toFixed(1)}K`:v.toFixed(0)} width={46}/>
                <Tooltip content={<ChartTip/>}/>
                <defs><linearGradient id="gPR" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#2563EB" stopOpacity={0.18}/><stop offset="95%" stopColor="#2563EB" stopOpacity={0}/></linearGradient></defs>
                <Area type="monotone" dataKey="close" name="Price" stroke="#2563EB" fill="url(#gPR)" strokeWidth={2} dot={false}/>
                <Line type="monotone" dataKey="sma50"  name="SMA50"  stroke="#F59E0B" strokeWidth={1.5} dot={false}/>
                <Line type="monotone" dataKey="sma200" name="SMA200" stroke="#EF4444" strokeWidth={1.5} dot={false} strokeDasharray="4 2"/>
                {pos && <ReferenceLine y={pos.stop}  stroke="#DC2626" strokeDasharray="3 3" strokeWidth={1.5} label={{value:`Stop €${fN(pos.stop,2)}`, position:"insideTopRight",  fill:"#DC2626", fontSize:9, fontFamily:"JetBrains Mono"}}/>}
                {pos && <ReferenceLine y={pos.entry} stroke="#64748b" strokeDasharray="2 4" strokeWidth={1}   label={{value:`Entry €${fN(pos.entry,2)}`,position:"insideBottomRight",fill:"#64748b", fontSize:8, fontFamily:"JetBrains Mono"}}/>}
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          {/* Volume */}
          <div style={{background:"#0d1117",border:"1px solid #1e293b",borderRadius:5,padding:"7px 12px 5px"}}>
            <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}>
              <span style={{fontFamily:"DM Sans",fontSize:9,fontWeight:600,color:"#94a3b8",letterSpacing:"0.05em"}}>VOLUME</span>
              <span style={{fontFamily:"DM Sans",fontSize:9,color:"#64748b"}}>— 50-day avg</span>
            </div>
            <ResponsiveContainer width="100%" height={72}>
              <ComposedChart data={series} margin={{top:2,right:10,left:6,bottom:0}}>
                <CartesianGrid stroke="#1e293b" vertical={false} opacity={0.5}/>
                <XAxis dataKey="date" tick={{fontFamily:"JetBrains Mono",fontSize:6,fill:"#475569"}} interval={Math.floor(lb/8)}/>
                <YAxis tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:"#475569"}} tickFormatter={v=>`${(v/1e6).toFixed(1)}M`} width={36}/>
                <Tooltip content={<ChartTip/>}/>
                <Bar dataKey="vol" name="Volume" fill="#94A3B8" opacity={0.55} radius={[1,1,0,0]}/>
                <Line type="monotone" dataKey="volAvg" name="50d Avg" stroke="#F59E0B" strokeWidth={1.2} dot={false}/>
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          {/* ADX + ATR */}
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
            <div style={{background:"#0d1117",border:"1px solid #1e293b",borderRadius:5,padding:"7px 12px 5px"}}>
              <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}>
                <span style={{fontFamily:"DM Sans",fontSize:9,fontWeight:600,color:"#94a3b8"}}>ADX (14) — TREND STRENGTH</span>
                <span style={{fontFamily:"JetBrains Mono",fontSize:9,color:sig.adx>25?"#4ade80":"#fbbf24"}}>{sig.adx.toFixed(1)}</span>
              </div>
              <ResponsiveContainer width="100%" height={85}>
                <AreaChart data={series} margin={{top:3,right:8,left:4,bottom:0}}>
                  <defs><linearGradient id="gAD" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#8B5CF6" stopOpacity={0.35}/><stop offset="95%" stopColor="#8B5CF6" stopOpacity={0}/></linearGradient></defs>
                  <CartesianGrid stroke="#1e293b" vertical={false} opacity={0.6}/>
                  <XAxis dataKey="date" tick={{fontFamily:"JetBrains Mono",fontSize:6,fill:"#475569"}} interval={Math.floor(lb/8)}/>
                  <YAxis domain={[0,60]} tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:"#475569"}} width={24}/>
                  <Tooltip content={<ChartTip/>}/>
                  <ReferenceLine y={20} stroke="#F59E0B" strokeDasharray="3 3" label={{value:"20",position:"insideTopRight",fill:"#F59E0B",fontSize:8,fontFamily:"JetBrains Mono"}}/>
                  <Area type="monotone" dataKey="adx" name="ADX(14)" stroke="#8B5CF6" fill="url(#gAD)" strokeWidth={1.8} dot={false}/>
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div style={{background:"#0d1117",border:"1px solid #1e293b",borderRadius:5,padding:"7px 12px 5px"}}>
              <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}>
                <span style={{fontFamily:"DM Sans",fontSize:9,fontWeight:600,color:"#94a3b8"}}>ATR% (20) — VOLATILITY</span>
                <span style={{fontFamily:"JetBrains Mono",fontSize:9,color:"#F97316"}}>{sig.atrP.toFixed(1)}%</span>
              </div>
              <ResponsiveContainer width="100%" height={85}>
                <AreaChart data={series} margin={{top:3,right:8,left:4,bottom:0}}>
                  <defs><linearGradient id="gAT" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#F97316" stopOpacity={0.35}/><stop offset="95%" stopColor="#F97316" stopOpacity={0}/></linearGradient></defs>
                  <CartesianGrid stroke="#1e293b" vertical={false} opacity={0.6}/>
                  <XAxis dataKey="date" tick={{fontFamily:"JetBrains Mono",fontSize:6,fill:"#475569"}} interval={Math.floor(lb/8)}/>
                  <YAxis domain={[0,"auto"]} tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:"#475569"}} tickFormatter={v=>`${v.toFixed(1)}%`} width={30}/>
                  <Tooltip content={<ChartTip pct/>}/>
                  <Area type="monotone" dataKey="atr" name="ATR%(20)" stroke="#F97316" fill="url(#gAT)" strokeWidth={1.8} dot={false}/>
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// TAB: PORTFOLIO
// ═══════════════════════════════════════════════════════════════════════════════
const PortfolioTab = () => {
  const totalV = PORTFOLIO.reduce((a,p) => a + p.curr*p.shares, 0);
  const sectorAlloc = PORTFOLIO.reduce((acc, p) => {
    const v = p.curr * p.shares;
    acc[p.sector] = (acc[p.sector]||0) + v;
    return acc;
  }, {});
  return (
    <div className="anim" style={{display:"flex",flexDirection:"column",gap:14}}>
      <div style={{display:"flex",gap:10,flexWrap:"wrap"}}>
        <KPI l="Open Positions" v="12" c={A.amber}/>
        <KPI l="Total Value" v={`€${Math.round(totalV).toLocaleString()}`} c={A.green}/>
        <KPI l="Unrealised P&L" v={`+€${PORTFOLIO.reduce((a,p)=>(a+(p.curr-p.entry)*p.shares),0).toLocaleString(undefined,{maximumFractionDigits:0})}`} c={A.green}/>
        <KPI l="Trailing Stops" v={PORTFOLIO.filter(p=>p.stopType==="Trailing").length} s="of 12 positions" c={A.amber}/>
        <KPI l="Crypto Weight" v={`${(PORTFOLIO.filter(p=>p.assetClass==="Cryptocurrency").reduce((a,p)=>a+p.curr*p.shares,0)/totalV*100).toFixed(1)}%`} s="limit 20%" c={A.cyan}/>
      </div>
      <div style={{background:A.card,border:`1px solid ${A.border}`,borderRadius:8,padding:14,overflowX:"auto"}}>
        <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:12}}><SectionBar/><span style={{fontFamily:"Syne",fontSize:12,fontWeight:700,color:A.text,letterSpacing:"0.05em",textTransform:"uppercase"}}>Open Positions</span></div>
        <table style={{width:"100%",borderCollapse:"collapse",fontFamily:"JetBrains Mono",fontSize:10}}>
          <thead><tr style={{borderBottom:`1px solid ${A.border2}`}}>{["SYMBOL","NAME","CLASS","SECTOR","SHARES","ENTRY €","PRICE €","VALUE €","WT%","P&L €","P&L%","STOP €","STOP TYPE","DAYS"].map(h=><th key={h} style={{padding:"5px 8px",color:A.dim,fontWeight:500,textAlign:["NAME","CLASS","SECTOR"].includes(h)?"left":"right"}}>{h}</th>)}</tr></thead>
          <tbody>{PORTFOLIO.map((p,i) => {
            const pE=(p.curr-p.entry)*p.shares, pP=(p.curr/p.entry-1)*100;
            const wt=p.curr*p.shares/totalV*100;
            return (
              <tr key={p.symbol} className="hr" style={{borderBottom:`1px solid ${A.border}`}}>
                <td style={{padding:"5px 8px",color:A.cyan,textAlign:"right"}}>{p.symbol}</td>
                <td style={{padding:"5px 8px",color:A.text,textAlign:"left",fontSize:9}}>{p.name}</td>
                <td style={{padding:"5px 8px",color:A.dim,textAlign:"left",fontSize:9}}>{p.assetClass}</td>
                <td style={{padding:"5px 8px",color:sectorColor[p.sector]||A.dim,textAlign:"left",fontSize:9}}>{p.sector}</td>
                <td style={{padding:"5px 8px",color:A.text,textAlign:"right"}}>{p.shares}</td>
                <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>€{fN(p.entry,2)}</td>
                <td style={{padding:"5px 8px",color:A.text,textAlign:"right"}}>€{fN(p.curr,2)}</td>
                <td style={{padding:"5px 8px",color:A.text,textAlign:"right"}}>€{Math.round(p.curr*p.shares).toLocaleString()}</td>
                <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>{wt.toFixed(1)}%</td>
                <td style={{padding:"5px 8px",color:pE>=0?A.green:A.red,fontWeight:700,textAlign:"right"}}>{pE>=0?"+":"-"}€{Math.abs(Math.round(pE)).toLocaleString()}</td>
                <td style={{padding:"5px 8px",color:pP>=0?A.green:A.red,fontWeight:700,textAlign:"right"}}>{pP>=0?"+":"-"}{Math.abs(pP).toFixed(1)}%</td>
                <td style={{padding:"5px 8px",color:A.red,textAlign:"right"}}>€{fN(p.stop,2)}</td>
                <td style={{padding:"5px 8px",textAlign:"right"}}><Tag c={p.stopType==="Trailing"?A.green:A.dim} sm>{p.stopType}</Tag></td>
                <td style={{padding:"5px 8px",color:A.dim,textAlign:"right"}}>{p.days}d</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:14}}>
        <div style={{background:A.card,border:`1px solid ${A.border}`,borderRadius:8,padding:14}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}><SectionBar c={A.blue}/><span style={{fontFamily:"Syne",fontSize:11,fontWeight:700,color:A.text,letterSpacing:"0.05em",textTransform:"uppercase"}}>Sector Allocation</span></div>
          {Object.entries(sectorAlloc).sort((a,b)=>b[1]-a[1]).map(([sec,val]) => {
            const pct = val/totalV*100;
            return <div key={sec} style={{marginBottom:8}}>
              <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}>
                <span style={{fontFamily:"DM Sans",fontSize:11,color:sectorColor[sec]||A.text}}>{sec}</span>
                <span style={{fontFamily:"JetBrains Mono",fontSize:11,color:pct>30?A.red:A.text}}>{pct.toFixed(1)}%</span>
              </div>
              <div style={{height:5,background:A.border,borderRadius:3}}>
                <div style={{width:`${Math.min(pct,100)}%`,height:"100%",background:pct>30?A.red:sectorColor[sec]||A.amber,borderRadius:3}}/>
              </div>
            </div>;
          })}
        </div>
        <div style={{background:A.card,border:`1px solid ${A.border}`,borderRadius:8,padding:14}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}><SectionBar c={A.purple}/><span style={{fontFamily:"Syne",fontSize:11,fontWeight:700,color:A.text,letterSpacing:"0.05em",textTransform:"uppercase"}}>Constraint Check</span></div>
          {[["Cash ≥ 5%","7.2%",true],["Crypto ≤ 20%","15.1%",true],["Any sector ≤ 30%","Tech 38.4%",false],["Max position ≤ 8%","NVDA 21.0%",false],["Top-3 ≤ 30%","27.1%",true],["Correlation","0.62 < 0.85",true],["Data staleness","0 days",true]].map(([k,v,ok]) => (
            <div key={k} style={{display:"flex",justifyContent:"space-between",padding:"5px 0",borderBottom:`1px solid ${A.border}`}}>
              <span style={{fontFamily:"DM Sans",fontSize:11,color:A.text}}>{k}</span>
              <div style={{display:"flex",gap:6,alignItems:"center"}}>
                <span style={{fontFamily:"JetBrains Mono",fontSize:10,color:ok?A.green:A.red}}>{v}</span>
                <Tag c={ok?A.green:A.red} sm>{ok?"✓ OK":"⚠ WARN"}</Tag>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// TAB: ANALYTICS  (Script 24)
// ═══════════════════════════════════════════════════════════════════════════════
const AnalyticsTab = () => {
  const pSt = {background:D.panelBg, border:`1px solid ${D.grid}`, borderRadius:8, padding:"14px 16px"};
  const MC = ({label, val, sub}) => (
    <div style={{background:D.plotBg,border:`1px solid ${D.grid}`,borderRadius:8,padding:"16px 20px",flex:"1 1 118px",textAlign:"center",minWidth:118}}>
      <div style={{color:D.sub,fontSize:10,textTransform:"uppercase",letterSpacing:"1px",marginBottom:6,fontFamily:"DM Sans"}}>{label}</div>
      <div style={{fontSize:22,fontWeight:600,fontFamily:"DM Sans"}}>{val}</div>
      {sub && <div style={{color:D.sub,fontSize:10,marginTop:4,fontFamily:"DM Sans"}}>{sub}</div>}
    </div>
  );
  const totalV = PORTFOLIO.reduce((a,p) => a+p.curr*p.shares, 0);
  return (
    <div className="anim" style={{display:"flex",flexDirection:"column",gap:14}}>
      <div style={{display:"flex",gap:10,flexWrap:"wrap"}}>
        <MC label="Total Return" val={<span style={{color:D.profit}}>+189.2%</span>} sub="since 2020-01-02"/>
        <MC label="Ann. Return"  val={<span style={{color:D.profit}}>+18.4%</span>}  sub="CAGR"/>
        <MC label="Sharpe Ratio" val={<span style={{color:D.profit}}>1.47</span>}     sub="252-day"/>
        <MC label="Max Drawdown" val={<span style={{color:D.loss}}>-19.8%</span>}     sub="peak-to-trough"/>
        <MC label="Win Rate"     val={<span style={{color:D.text}}>54.2%</span>}      sub="312 trades"/>
        <MC label="Profit Factor"val={<span style={{color:D.profit}}>2.18×</span>}   sub="gross P / gross L"/>
        <MC label="Portfolio Val" val={<span style={{color:D.text}}>{`€${Math.round(totalV).toLocaleString()}`}</span>} sub="current estimate"/>
        <MC label="Open Positions"val={<span style={{color:D.text}}>12</span>}        sub="active"/>
      </div>
      <div style={{...pSt,display:"flex",gap:16,flexWrap:"wrap",alignItems:"center"}}>
        <span style={{fontFamily:"DM Sans",fontSize:10,fontWeight:700,color:D.sub,textTransform:"uppercase",letterSpacing:"0.07em",flexShrink:0}}>Script 23 · Risk Analytics</span>
        {[["VaR 95% 1-day","€880",D.loss],["CVaR 95% 1-day","€1,240",D.loss],["VaR 99% 1-day","€1,420",D.loss],["Beta (SPY)","0.74",D.text],["Track. Error","8.2% ann",D.text],["Info Ratio","0.78",D.profit],["HHI","0.094",D.text],["Effective N","10.6",D.text]].map(([l,v,c]) => (
          <div key={l} style={{textAlign:"center"}}>
            <div style={{fontFamily:"DM Sans",fontSize:9,color:D.sub,marginBottom:2}}>{l}</div>
            <div style={{fontFamily:"JetBrains Mono",fontSize:13,fontWeight:600,color:c}}>{v}</div>
          </div>
        ))}
      </div>
      <div style={pSt}>
        <div style={{fontFamily:"DM Sans",fontSize:12,fontWeight:600,color:D.text,marginBottom:10}}>📈 Equity Curve</div>
        <ResponsiveContainer width="100%" height={205}>
          <AreaChart data={EQUITY_CURVE} margin={{top:4,right:12,left:10,bottom:0}}>
            <defs><linearGradient id="gEQ" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor={D.equity} stopOpacity={0.22}/><stop offset="95%" stopColor={D.equity} stopOpacity={0}/></linearGradient></defs>
            <CartesianGrid stroke={D.grid} vertical={false}/>
            <XAxis dataKey="label" tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:D.sub}} interval={6}/>
            <YAxis tickFormatter={v=>`€${(v/1000).toFixed(0)}K`} tick={{fontFamily:"JetBrains Mono",fontSize:8,fill:D.sub}}/>
            <Tooltip content={<ChartTip/>}/>
            <Legend wrapperStyle={{fontFamily:"DM Sans",fontSize:10,color:D.sub}}/>
            <Area type="monotone" dataKey="strategy"  name="Strategy NAV" stroke={D.equity} fill="url(#gEQ)" strokeWidth={2} dot={false}/>
            <Line type="monotone" dataKey="benchmark" name="SPY B&H"      stroke={D.bm} strokeWidth={1.5} dot={false} strokeDasharray="5 3"/>
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div style={pSt}>
        <div style={{fontFamily:"DM Sans",fontSize:12,fontWeight:600,color:D.text,marginBottom:10}}>🗓️ Monthly Return Heatmap</div>
        <div style={{overflowX:"auto"}}>
          <table style={{borderCollapse:"collapse",width:"100%",fontFamily:"JetBrains Mono",fontSize:11}}>
            <thead><tr>
              <th style={{padding:"5px 8px",color:D.sub,fontWeight:400,fontSize:10,textAlign:"left"}}></th>
              {MONTHS.map(m => <th key={m} style={{padding:"5px 6px",color:D.sub,fontWeight:500,fontSize:10,textAlign:"center"}}>{m}</th>)}
              <th style={{padding:"5px 8px",color:D.sub,fontWeight:500,fontSize:10,textAlign:"right"}}>Full Year</th>
            </tr></thead>
            <tbody>{Object.entries(HEATMAP).map(([yr,rets]) => {
              const ann = rets.filter(v=>v!==null).reduce((a,v)=>a*(1+v/100),1)-1;
              return (
                <tr key={yr}>
                  <td style={{padding:"4px 8px",color:D.sub,fontWeight:600,fontSize:10,whiteSpace:"nowrap"}}>{yr}</td>
                  {rets.map((v,i) => {
                    const bg = v===null?D.panelBg:v>4?"#1B5E20":v>2?"#2E7D32":v>0?"#388E3C":v>-2?"#C62828":v>-4?"#B71C1C":"#7f0000";
                    return <td key={i} style={{padding:"5px 4px",background:bg,color:v===null?D.sub:D.text,textAlign:"center",fontSize:10,fontWeight:v&&Math.abs(v)>3?700:400,borderRadius:2,minWidth:42}}>{v===null?"":v>0?`+${v.toFixed(1)}%`:`${v.toFixed(1)}%`}</td>;
                  })}
                  <td style={{padding:"5px 8px",color:ann>=0?D.profit:D.loss,fontWeight:700,textAlign:"right"}}>{ann>=0?"+":""}{(ann*100).toFixed(1)}%</td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      </div>
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:14}}>
        <div style={pSt}>
          <div style={{fontFamily:"DM Sans",fontSize:12,fontWeight:600,color:D.text,marginBottom:8}}>📉 Drawdown</div>
          <ResponsiveContainer width="100%" height={155}>
            <AreaChart data={EQUITY_CURVE} margin={{top:4,right:8,left:8,bottom:0}}>
              <defs><linearGradient id="gDD" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor={D.dd} stopOpacity={0.38}/><stop offset="95%" stopColor={D.dd} stopOpacity={0.03}/></linearGradient></defs>
              <CartesianGrid stroke={D.grid} vertical={false}/>
              <XAxis dataKey="label" tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:D.sub}} interval={7}/>
              <YAxis tickFormatter={v=>`${v}%`} tick={{fontFamily:"JetBrains Mono",fontSize:8,fill:D.sub}}/>
              <Tooltip content={<ChartTip pct/>}/>
              <ReferenceLine y={-19.8} stroke={D.loss} strokeDasharray="3 3" label={{value:"Max DD −19.8%",fill:D.loss,fontSize:8,fontFamily:"JetBrains Mono"}}/>
              <Area type="monotone" dataKey="dd" name="Drawdown" stroke={D.dd} fill="url(#gDD)" strokeWidth={1.5} dot={false}/>
            </AreaChart>
          </ResponsiveContainer>
        </div>
        <div style={pSt}>
          <div style={{fontFamily:"DM Sans",fontSize:12,fontWeight:600,color:D.text,marginBottom:8}}>📊 Rolling Sharpe Ratio (252-day)</div>
          <ResponsiveContainer width="100%" height={155}>
            <LineChart data={ROLLING_SHARPE} margin={{top:4,right:8,left:8,bottom:0}}>
              <CartesianGrid stroke={D.grid} vertical={false}/>
              <XAxis dataKey="label" tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:D.sub}} interval={7}/>
              <YAxis tick={{fontFamily:"JetBrains Mono",fontSize:8,fill:D.sub}} domain={["auto","auto"]}/>
              <Tooltip content={<ChartTip/>}/>
              <ReferenceLine y={1.0} stroke={D.sub} strokeDasharray="3 3" label={{value:"Good (1.0)",fill:D.sub,fontSize:8,fontFamily:"JetBrains Mono"}}/>
              <ReferenceLine y={0}   stroke={D.sub} strokeDasharray="2 4" opacity={0.5}/>
              <Line type="monotone" dataKey="sharpe" name="Rolling Sharpe" stroke={D.sharpe} strokeWidth={2} dot={false}/>
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div style={{display:"grid",gridTemplateColumns:"260px 1fr",gap:14}}>
        <div style={pSt}>
          <div style={{fontFamily:"DM Sans",fontSize:12,fontWeight:600,color:D.text,marginBottom:8}}>🥧 Asset Allocation</div>
          <ResponsiveContainer width="100%" height={195}>
            <PieChart>
              <Pie data={ALLOC_PIE} dataKey="value" cx="50%" cy="50%" innerRadius={52} outerRadius={82} paddingAngle={2}
                label={({name,percent})=>`${name} ${(percent*100).toFixed(0)}%`} labelLine={false}>
                {ALLOC_PIE.map(e => <Cell key={e.name} fill={e.color} stroke={D.panelBg} strokeWidth={2}/>)}
              </Pie>
              <Tooltip formatter={v=>`€${v.toLocaleString()}`} contentStyle={{background:D.panelBg,border:`1px solid ${D.grid}`}}/>
            </PieChart>
          </ResponsiveContainer>
        </div>
        <div style={pSt}>
          <div style={{fontFamily:"DM Sans",fontSize:12,fontWeight:600,color:D.text,marginBottom:8}}>📋 Position P&L</div>
          <div style={{overflowX:"auto",maxHeight:210,overflowY:"auto"}}>
            <table style={{width:"100%",borderCollapse:"collapse"}}>
              <thead><tr style={{background:"#0D0D1A"}}>
                {["Symbol","Name","Class","Shares","Entry €","Price €","Value €","Wt%","P&L €","P&L%","Stop €","Days"].map((h,i)=>(
                  <th key={h} style={{background:"#0D0D1A",color:D.sub,fontSize:10,padding:"7px 9px",textAlign:i<3?"left":"right",borderBottom:`1px solid ${D.grid}`,whiteSpace:"nowrap"}}>{h}</th>
                ))}
              </tr></thead>
              <tbody>{PORTFOLIO.map((p,i) => {
                const pE=(p.curr-p.entry)*p.shares, pP=(p.curr/p.entry-1)*100;
                const wt=p.curr*p.shares/PORTFOLIO.reduce((a,q)=>a+q.curr*q.shares,0)*100;
                const bg = i%2===0?"#111128":"#0D0D1A";
                const cs = {fontSize:11,padding:"6px 9px",borderBottom:`1px solid #1A1A2E`,background:bg};
                return (
                  <tr key={p.symbol}>
                    <td style={{...cs,textAlign:"left"}}><code style={{color:D.equity,fontSize:11}}>{p.symbol}</code></td>
                    <td style={{...cs,textAlign:"left",color:D.text,fontSize:10}}>{p.name.length>16?p.name.slice(0,15)+"…":p.name}</td>
                    <td style={{...cs,textAlign:"left",color:D.sub,fontSize:9}}>{p.assetClass}</td>
                    <td style={{...cs,textAlign:"right",color:D.text}}>{p.shares}</td>
                    <td style={{...cs,textAlign:"right",color:D.sub}}>€{fN(p.entry,2)}</td>
                    <td style={{...cs,textAlign:"right",color:D.text}}>€{fN(p.curr,2)}</td>
                    <td style={{...cs,textAlign:"right",color:D.text}}>€{Math.round(p.curr*p.shares).toLocaleString()}</td>
                    <td style={{...cs,textAlign:"right",color:D.sub}}>{wt.toFixed(1)}%</td>
                    <td style={{...cs,textAlign:"right",color:pE>=0?D.profit:D.loss,fontWeight:600}}>{pE>=0?"+":"-"}€{Math.abs(Math.round(pE)).toLocaleString()}</td>
                    <td style={{...cs,textAlign:"right",color:pP>=0?D.profit:D.loss,fontWeight:600}}>{pP>=0?"+":"-"}{Math.abs(pP).toFixed(1)}%</td>
                    <td style={{...cs,textAlign:"right",color:D.loss,fontSize:10}}>€{fN(p.stop,2)}</td>
                    <td style={{...cs,textAlign:"right",color:D.sub}}>{p.days}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// TAB: BACKTEST  (Script 19 validation framework)
// ═══════════════════════════════════════════════════════════════════════════════
const BacktestTab = () => {
  const [aiR, setAiR] = useState("");
  const [aiL, setAiL] = useState(false);

  const TESTS = [
    {id:"T01",desc:"Total Return > 30% over backtest period",     val:"+189.2%",threshold:"> 30%",    pass:true},
    {id:"T02",desc:"Strategy Sharpe > Benchmark Sharpe × 1.25",  val:"1.47",   threshold:"> 0.81",   pass:true},
    {id:"T03",desc:"Max Drawdown ≥ −30% (absolute limit)",       val:"−19.8%", threshold:"≥ −30%",   pass:true},
    {id:"T04",desc:"≥ 100 completed round-trip trades",          val:"312",    threshold:"≥ 100",    pass:true},
    {id:"T05",desc:"35% ≤ Win Rate ≤ 65%",                      val:"54.2%",  threshold:"35–65%",   pass:true},
    {id:"T06",desc:"Profit Factor ≥ 1.5",                       val:"2.18",   threshold:"≥ 1.5",    pass:true},
    {id:"T07",desc:"Avg Win ≥ 2.0 × Avg Loss",                  val:"2.58×",  threshold:"≥ 2.0×",   pass:true},
    {id:"T08",desc:"Return drop < 50% when costs × 2",          val:"−18.1%", threshold:"< 50% Δ",  pass:true},
    {id:"T09",desc:"Avg drawdown recovery ≤ 12 months",         val:"7.4 mo", threshold:"≤ 12 mo",  pass:true},
    {id:"T10",desc:"≥ 70% of calendar years positive",          val:"83.3%",  threshold:"≥ 70%",    pass:true},
  ];
  const SCORE = [
    {m:"CAGR %",           v:18.4, pts:3, max:3, t:"≥8%→1 · ≥12%→2 · ≥18%→3"},
    {m:"Sharpe Ratio",     v:1.47, pts:3, max:3, t:"≥0.8→1 · ≥1.2→2 · ≥2.0→3"},
    {m:"Sortino Ratio",    v:2.11, pts:3, max:3, t:"≥1.0→1 · ≥1.5→2 · ≥2.5→3"},
    {m:"Calmar Ratio",     v:0.93, pts:2, max:3, t:"≥0.5→1 · ≥1.0→2 · ≥2.0→3"},
    {m:"Max Drawdown %",   v:-19.8,pts:2, max:3, t:"≥-30%→1 · ≥-20%→2 · ≥-15%→3"},
    {m:"Win Rate %",       v:54.2, pts:3, max:3, t:"≥35%→1 · ≥45%→2 · ≥55%→3"},
    {m:"Profit Factor",    v:2.18, pts:3, max:3, t:"≥1.5→1 · ≥2.0→2 · ≥2.5→3"},
    {m:"Win/Loss Ratio",   v:2.58, pts:3, max:3, t:"≥2.0→1 · ≥2.5→2 · ≥3.5→3"},
    {m:"Positive Years %", v:83.3, pts:2, max:3, t:"≥70%→1 · ≥75%→2 · ≥85%→3"},
    {m:"Avg Recovery mo",  v:7.4,  pts:3, max:3, t:"≤12→1 · ≤9→2 · ≤6→3"},
  ];
  const tot = SCORE.reduce((a,s) => a+s.pts, 0);
  const RF = [
    {id:"RF01",n:"Curve-Fitted Equity Curve",    ind:">75% positive months",              fired:false},
    {id:"RF02",n:"Single Trade Dominance",        ind:">50% return from one trade",        fired:false},
    {id:"RF03",n:"Excessive Win Rate",            ind:"Win rate >70% in trend strategy",   fired:false},
    {id:"RF04",n:"Extreme Parameter Selection",   ind:"Optimal params at grid edge",       fired:false},
    {id:"RF05",n:"IS/OOS Collapse",               ind:"OOS Sharpe < 50% of IS Sharpe",     fired:false},
    {id:"RF06",n:"Zero Losing Years",             ind:"No down year over 5+ year period",  fired:false},
    {id:"RF07",n:"Unrealistic Trade Count",       ind:"<100 or >2,000 trades / 5 years",   fired:false},
  ];
  const BM = [{n:"SPY buy-and-hold",cagr:11.0,sh:0.65,dd:-34.0},{n:"60/40 Portfolio",cagr:8.5,sh:0.55,dd:-22.0},{n:"Naive Trend (SMA on SPY)",cagr:9.0,sh:0.50,dd:-20.0}];
  const MCd = Array.from({length:25},(_,i)=>({m:i*2.5,p5:Math.round(100000*(1+0.042)**(i*0.5)*(0.74-i*0.0025)),p25:Math.round(100000*(1+0.068)**(i*0.5)*(0.9-i*0.0005)),p50:Math.round(100000*(1+0.092)**(i*0.5)),p75:Math.round(100000*(1+0.118)**(i*0.5)*(1+i*0.001)),p95:Math.round(100000*(1+0.148)**(i*0.5)*(1.08+i*0.002))}));

  const runAI = async () => {
    setAiL(true); setAiR("");
    try {
      const r = await fetch("https://api.anthropic.com/v1/messages",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:"claude-sonnet-4-20250514",max_tokens:900,messages:[{role:"user",content:"You are a senior systematic trend-following portfolio manager. Review these backtest results in 3 focused paragraphs: (1) result quality & robustness, (2) key regime risks, (3) one concrete improvement. Results (2019–2024 IS): CAGR 18.4% vs SPY 12.1% | Sharpe 1.47 (OOS 1.31, 89% IS retention) | Sortino 2.11 | Max DD -19.8% | Calmar 0.93 | Win Rate 54.2% | Profit Factor 2.18 | 312 trades | Avg hold 48d | All 10 validation tests PASS | Score 27/30 GOOD | Zero red flags | WFO 6/6 windows OOS positive. Strategy: SMA50>SMA200, Close>SMA50, ADX>20 qualify; Score=(Close-SMA200)/SMA200×100; 2% risk, inverse-ATR sizing 0.5–8%; initial stop Entry-3×ATR, trailing after +15% at Friday_Close-4×ATR."}]})});
      const d = await r.json();
      setAiR(d.content?.find(b=>b.type==="text")?.text || "No response.");
    } catch { setAiR("Analysis unavailable."); }
    setAiL(false);
  };

  const thSt = {fontFamily:"JetBrains Mono",fontSize:10,padding:"6px 9px",color:A.dim,fontWeight:500,borderBottom:`1px solid ${A.border2}`};
  const tdSt = {fontFamily:"JetBrains Mono",fontSize:10,padding:"5px 9px",borderBottom:`1px solid ${A.border}`};
  const box = (title, bg, children) => (
    <div style={{background:A.card,border:`1px solid ${A.border}`,borderRadius:8,overflow:"hidden"}}>
      <div style={{background:bg||"#1B2A47",color:"white",fontFamily:"Syne",fontSize:11,fontWeight:700,padding:"7px 14px",letterSpacing:"0.04em"}}>{title}</div>
      <div style={{overflowX:"auto"}}>{children}</div>
    </div>
  );

  return (
    <div className="anim" style={{display:"flex",flexDirection:"column",gap:14}}>
      {/* Rating banner */}
      <div style={{display:"flex",gap:12,flexWrap:"wrap",alignItems:"center",background:A.card,border:`1px solid ${A.border}`,borderRadius:8,padding:"14px 18px"}}>
        <div style={{background:`${A.green}20`,border:`2px solid ${A.green}`,borderRadius:8,padding:"10px 24px",textAlign:"center",flexShrink:0}}>
          <div style={{fontFamily:"JetBrains Mono",fontSize:9,color:A.dim,marginBottom:4}}>SCRIPT 19 · OVERALL RATING</div>
          <div style={{fontFamily:"Syne",fontSize:28,fontWeight:800,color:A.green}}>GOOD</div>
          <div style={{fontFamily:"JetBrains Mono",fontSize:11,color:A.green,marginTop:3}}>{tot}/30 points</div>
        </div>
        <div style={{flex:1,display:"flex",gap:10,flexWrap:"wrap"}}>
          {[["Score",`${tot}/30 pts`,A.green],["Tests Passed","10/10",A.green],["Red Flags","0 triggered",A.green],["WFO Windows","6/6 OOS +ve",A.green],["Deployment","✓ APPROVED",A.green],["Rating","Deploy / std monitoring",A.amber]].map(([l,v,c]) => (
            <div key={l} style={{padding:"8px 14px",background:A.card2,border:`1px solid ${A.border2}`,borderRadius:5}}>
              <div style={{fontFamily:"DM Sans",fontSize:9,color:A.dim,textTransform:"uppercase",letterSpacing:"0.04em",marginBottom:3}}>{l}</div>
              <div style={{fontFamily:"JetBrains Mono",fontSize:13,fontWeight:700,color:c}}>{v}</div>
            </div>
          ))}
        </div>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:14}}>
        {/* 10 Tests */}
        {box("10 PRIMARY VALIDATION TESTS — Script 19", undefined,
          <table style={{width:"100%",borderCollapse:"collapse"}}>
            <thead><tr>{["ID","Description","Actual","Threshold","Result"].map(h=><th key={h} style={{...thSt,textAlign:h==="Description"?"left":"right"}}>{h}</th>)}</tr></thead>
            <tbody>{TESTS.map(t=>(
              <tr key={t.id} className="hr" style={{background:t.pass?`${A.green}06`:`${A.red}06`}}>
                <td style={{...tdSt,textAlign:"right"}}><Tag c={A.dim} sm>{t.id}</Tag></td>
                <td style={{...tdSt,textAlign:"left",color:A.text}}>{t.desc}</td>
                <td style={{...tdSt,textAlign:"right",color:t.pass?A.green:A.red,fontWeight:700}}>{t.val}</td>
                <td style={{...tdSt,textAlign:"right",color:A.dim}}>{t.threshold}</td>
                <td style={{...tdSt,textAlign:"right"}}><Tag c={t.pass?A.green:A.red} sm>{t.pass?"✓ PASS":"✗ FAIL"}</Tag></td>
              </tr>
            ))}</tbody>
          </table>
        )}
        {/* 30-pt scoring */}
        {box("30-POINT SCORING SYSTEM — Script 19", undefined,
          <>
            <table style={{width:"100%",borderCollapse:"collapse"}}>
              <thead><tr>{["Metric","Value","Score","Thresholds"].map(h=><th key={h} style={{...thSt,textAlign:h==="Metric"||h==="Thresholds"?"left":"right"}}>{h}</th>)}</tr></thead>
              <tbody>
                {SCORE.map(s=>(
                  <tr key={s.m} className="hr">
                    <td style={{...tdSt,textAlign:"left",color:A.text}}>{s.m}</td>
                    <td style={{...tdSt,textAlign:"right",fontWeight:600,color:A.text}}>{s.v}</td>
                    <td style={{...tdSt,textAlign:"right"}}>
                      <div style={{display:"flex",gap:2,justifyContent:"flex-end",alignItems:"center"}}>
                        {Array.from({length:s.max}).map((_,i)=><div key={i} style={{width:9,height:9,borderRadius:2,background:i<s.pts?A.green:A.border2}}/>)}
                        <span style={{fontFamily:"JetBrains Mono",fontSize:10,color:A.green,marginLeft:5,fontWeight:700}}>{s.pts}/{s.max}</span>
                      </div>
                    </td>
                    <td style={{...tdSt,textAlign:"left",color:A.dim,fontSize:9}}>{s.t}</td>
                  </tr>
                ))}
                <tr style={{background:`${A.green}10`}}>
                  <td style={{...tdSt,textAlign:"left",fontWeight:700,color:A.text,borderTop:`1px solid ${A.green}30`}}>TOTAL</td>
                  <td style={tdSt}></td>
                  <td style={{...tdSt,textAlign:"right",fontWeight:800,color:A.green,fontSize:16,borderTop:`1px solid ${A.green}30`}}>{tot}/30</td>
                  <td style={{...tdSt,color:A.green,fontWeight:700,borderTop:`1px solid ${A.green}30`}}>→ GOOD</td>
                </tr>
              </tbody>
            </table>
            <div style={{padding:"8px 14px",borderTop:`1px solid ${A.border}`}}>
              {[[27,"EXCELLENT","Deploy immediately"],[23,"GOOD","Deploy with standard monitoring"],[19,"ACCEPTABLE","Deploy with enhanced monitoring"],[15,"MARGINAL","Paper trade first"],[0,"FAIL","Do not deploy"]].map(([t,l,d])=>{
                const active = tot>=(t) && tot<({27:31,23:27,19:23,15:19,0:15}[t]||15);
                return <div key={l} style={{display:"flex",gap:8,alignItems:"center",padding:"3px 0",opacity:active?1:0.3}}>
                  <span style={{fontFamily:"JetBrains Mono",fontSize:10,color:A.dim,width:24}}>{t}+</span>
                  <span style={{fontFamily:"Syne",fontSize:11,fontWeight:700,color:["EXCELLENT","GOOD"].includes(l)?A.green:l==="ACCEPTABLE"?A.amber:l==="MARGINAL"?A.orange:A.red,width:82}}>{l}</span>
                  <span style={{fontFamily:"DM Sans",fontSize:10,color:A.dim}}>{d}</span>
                  {active && <Tag c={A.green} sm>← YOU ARE HERE</Tag>}
                </div>;
              })}
            </div>
          </>
        )}
      </div>

      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:14}}>
        {box("7 CRITICAL RED FLAGS — Script 19", undefined,
          <table style={{width:"100%",borderCollapse:"collapse"}}>
            <thead><tr>{["ID","Flag Name","Indicator","Status"].map(h=><th key={h} style={{...thSt,textAlign:h==="Flag Name"||h==="Indicator"?"left":"right"}}>{h}</th>)}</tr></thead>
            <tbody>{RF.map(f=>(
              <tr key={f.id} className="hr" style={{background:f.fired?`${A.red}10`:`${A.green}04`}}>
                <td style={{...tdSt,textAlign:"right"}}><Tag c={f.fired?A.red:A.dim} sm>{f.id}</Tag></td>
                <td style={{...tdSt,textAlign:"left",fontWeight:600,color:A.text}}>{f.n}</td>
                <td style={{...tdSt,textAlign:"left",color:A.dim,fontSize:9}}>{f.ind}</td>
                <td style={{...tdSt,textAlign:"right"}}><Tag c={f.fired?A.red:A.green} sm>{f.fired?"⚠ TRIGGERED":"✓ CLEAR"}</Tag></td>
              </tr>
            ))}</tbody>
          </table>
        )}
        {box("BENCHMARK & WALK-FORWARD — Scripts 17+19", undefined,
          <>
            <table style={{width:"100%",borderCollapse:"collapse"}}>
              <thead><tr>{["Benchmark","CAGR","Sharpe","Max DD","vs Strategy"].map(h=><th key={h} style={{...thSt,textAlign:h==="Benchmark"?"left":"right"}}>{h}</th>)}</tr></thead>
              <tbody>
                <tr style={{background:`${A.amber}10`}}>
                  <td style={{...tdSt,textAlign:"left",color:A.amber,fontWeight:700}}>THIS STRATEGY</td>
                  <td style={{...tdSt,textAlign:"right",color:A.green,fontWeight:700}}>+18.4%</td>
                  <td style={{...tdSt,textAlign:"right",color:A.green,fontWeight:700}}>1.47</td>
                  <td style={{...tdSt,textAlign:"right",color:A.red,fontWeight:700}}>-19.8%</td>
                  <td style={tdSt}>—</td>
                </tr>
                {BM.map(b=>(
                  <tr key={b.n} className="hr">
                    <td style={{...tdSt,textAlign:"left",color:A.text}}>{b.n}</td>
                    <td style={{...tdSt,textAlign:"right",color:A.dim}}>+{b.cagr.toFixed(1)}%</td>
                    <td style={{...tdSt,textAlign:"right",color:A.dim}}>{b.sh.toFixed(2)}</td>
                    <td style={{...tdSt,textAlign:"right",color:A.dim}}>{b.dd.toFixed(1)}%</td>
                    <td style={{...tdSt,textAlign:"right",color:A.green,fontSize:9}}>+{(18.4-b.cagr).toFixed(1)}% CAGR</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{padding:"10px 14px",borderTop:`1px solid ${A.border}`}}>
              <div style={{fontFamily:"DM Sans",fontSize:10,fontWeight:700,color:A.text,marginBottom:6}}>Walk-Forward IS vs OOS Sharpe (Script 17)</div>
              <ResponsiveContainer width="100%" height={110}>
                <BarChart data={[{w:"2019",is:1.82,oos:1.58},{w:"2020",is:2.14,oos:1.89},{w:"2021",is:1.65,oos:1.42},{w:"2022",is:0.94,oos:0.71},{w:"2023",is:1.71,oos:1.55},{w:"2024",is:1.88,oos:1.72}]} margin={{top:4,right:4,left:0,bottom:0}}>
                  <CartesianGrid stroke={A.border} vertical={false}/>
                  <XAxis dataKey="w" tick={{fontFamily:"JetBrains Mono",fontSize:8,fill:A.dim}}/>
                  <YAxis tick={{fontFamily:"JetBrains Mono",fontSize:7,fill:A.dim}} domain={[0,2.5]}/>
                  <Tooltip content={<ChartTip/>}/>
                  <ReferenceLine y={1.0} stroke={A.amber} strokeDasharray="3 3"/>
                  <Bar dataKey="is"  name="IS Sharpe"  fill={A.amber} radius={[2,2,0,0]}/>
                  <Bar dataKey="oos" name="OOS Sharpe" fill={A.blue}  radius={[2,2,0,0]}/>
                  <Legend wrapperStyle={{fontFamily:"DM Sans",fontSize:9}}/>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </div>

      {/* Monte Carlo */}
      <div style={{background:A.card,border:`1px solid ${A.border}`,borderRadius:8,overflow:"hidden"}}>
        <div style={{background:"#1B2A47",color:"white",fontFamily:"Syne",fontSize:11,fontWeight:700,padding:"7px 14px",letterSpacing:"0.04em"}}>MONTE CARLO SIMULATION — Script 18  (1,000 paths · 60-month horizon from $100K)</div>
        <div style={{padding:16}}>
          <div style={{display:"flex",gap:10,flexWrap:"wrap",marginBottom:14}}>
            {[["P5 Final","$126K",A.red],["P25 Final","$162K",A.orange],["P50 Final","$198K",A.amber],["P75 Final","$248K",A.green],["P95 Final","$410K",A.green],["P5 Max DD","−38.2%",A.red],["Ruin Prob","2.1%",A.green]].map(([l,v,c]) => (
              <div key={l} style={{padding:"8px 14px",background:A.card2,border:`1px solid ${A.border2}`,borderRadius:5}}>
                <div style={{fontFamily:"DM Sans",fontSize:9,color:A.dim,marginBottom:2}}>{l}</div>
                <div style={{fontFamily:"JetBrains Mono",fontSize:14,fontWeight:700,color:c}}>{v}</div>
              </div>
            ))}
          </div>
          <ResponsiveContainer width="100%" height={175}>
            <AreaChart data={MCd} margin={{top:4,right:12,left:10,bottom:0}}>
              <CartesianGrid stroke={A.border} vertical={false}/>
              <XAxis dataKey="m" tick={{fontFamily:"JetBrains Mono",fontSize:8,fill:A.dim}} tickFormatter={v=>`M${v}`}/>
              <YAxis tickFormatter={v=>`$${(v/1000).toFixed(0)}K`} tick={{fontFamily:"JetBrains Mono",fontSize:8,fill:A.dim}}/>
              <Tooltip content={<ChartTip/>}/>
              <Area type="monotone" dataKey="p95" name="P95" stroke={A.green} fill={`${A.green}06`} strokeDasharray="4 2" dot={false}/>
              <Area type="monotone" dataKey="p75" name="P75" stroke={A.green} fill={`${A.green}10`} dot={false}/>
              <Area type="monotone" dataKey="p50" name="P50" stroke={A.amber} fill={`${A.amber}14`} strokeWidth={2} dot={false}/>
              <Area type="monotone" dataKey="p25" name="P25" stroke={A.red}   fill={`${A.red}08`} dot={false}/>
              <Area type="monotone" dataKey="p5"  name="P5"  stroke={A.red}   fill={`${A.red}04`} strokeDasharray="4 2" dot={false}/>
              <Legend wrapperStyle={{fontFamily:"DM Sans",fontSize:10}}/>
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* AI Analyst */}
      <div style={{background:A.card,border:`1px solid ${A.border}`,borderRadius:8,padding:16}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:12}}>
          <div style={{display:"flex",alignItems:"center",gap:8}}>
            <SectionBar/><span style={{fontFamily:"Syne",fontSize:13,fontWeight:700,color:A.text,letterSpacing:"0.05em",textTransform:"uppercase"}}>AI Institutional Analysis — Claude (claude-sonnet-4-20250514)</span>
          </div>
          <button onClick={runAI} disabled={aiL} style={{fontFamily:"Syne",fontSize:11,fontWeight:800,padding:"7px 20px",background:aiL?`${A.amber}20`:A.amber,color:aiL?A.amber:"#04060e",border:"none",borderRadius:5,cursor:aiL?"not-allowed":"pointer",letterSpacing:"0.06em"}}>
            {aiL ? "ANALYZING..." : "▶ RUN ANALYSIS"}
          </button>
        </div>
        {aiL && <div style={{fontFamily:"JetBrains Mono",fontSize:11,color:A.amber}}>Reviewing all 10 tests, red flags, and WFO results<span className="blink">...</span></div>}
        {aiR && <div style={{fontFamily:"DM Sans",fontSize:13,color:A.text,lineHeight:1.85,whiteSpace:"pre-wrap"}}>{aiR}</div>}
        {!aiL && !aiR && <div style={{fontFamily:"DM Sans",fontSize:11,color:A.dim,fontStyle:"italic"}}>Click to receive an institutional-grade assessment of the validation test results, scoring, red flag analysis, and regime risk evaluation from Claude.</div>}
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN APP
// ═══════════════════════════════════════════════════════════════════════════════
export default function App() {
  const [tab,  setTab]  = useState("overview");
  const [time, setTime] = useState(new Date());
  useEffect(() => { const t = setInterval(() => setTime(new Date()), 1000); return () => clearInterval(t); }, []);

  const TABS = [
    {id:"overview",         icon:"◈", label:"OVERVIEW"},
    {id:"pipeline",         icon:"⟁", label:"PIPELINE"},
    {id:"signals",          icon:"◎", label:"SIGNALS"},
    {id:"recommendations",  icon:"📋",label:"RECOMMENDATIONS"},
    {id:"charts",           icon:"◫", label:"TECH CHARTS"},
    {id:"portfolio",        icon:"◩", label:"PORTFOLIO"},
    {id:"analytics",        icon:"◌", label:"ANALYTICS"},
    {id:"backtest",         icon:"◆", label:"BACKTEST"},
  ];
  const C = {
    overview:        <OverviewTab/>,
    pipeline:        <PipelineTab/>,
    signals:         <SignalsTab/>,
    recommendations: <RecTab/>,
    charts:          <ChartsTab/>,
    portfolio:       <PortfolioTab/>,
    analytics:       <AnalyticsTab/>,
    backtest:        <BacktestTab/>,
  };

  return (
    <div style={{minHeight:"100vh", background:A.bg, color:A.text}}>
      <Fonts/>
      {/* ── Header ── */}
      <div style={{background:A.card,borderBottom:`1px solid ${A.border2}`,padding:"0 22px",display:"flex",alignItems:"center",justifyContent:"space-between",height:52,position:"sticky",top:0,zIndex:100}}>
        <div style={{display:"flex",alignItems:"center",gap:12}}>
          <div style={{display:"flex",gap:3,alignItems:"center"}}>
            <div style={{width:7,height:32,background:A.amber,borderRadius:2}}/>
            <div style={{width:3,height:26,background:`${A.amber}50`,borderRadius:2}}/>
            <div style={{width:2,height:18,background:`${A.amber}25`,borderRadius:2}}/>
          </div>
          <div>
            <div style={{fontFamily:"Syne",fontSize:15,fontWeight:800,color:A.amber,letterSpacing:"0.08em"}}>TREND FOLLOWING OS</div>
            <div style={{fontFamily:"JetBrains Mono",fontSize:8,color:A.dim,letterSpacing:"0.04em"}}>Architecture v3.2 · Systematic · 24-Script Pipeline · Multi-Asset</div>
          </div>
        </div>
        <div style={{display:"flex",gap:14,alignItems:"center"}}>
          <div style={{textAlign:"right"}}>
            <div style={{fontFamily:"JetBrains Mono",fontSize:10,color:A.text}}>{time.toLocaleDateString("en-US",{weekday:"short",month:"short",day:"numeric",year:"numeric"})}</div>
            <div style={{fontFamily:"JetBrains Mono",fontSize:9,color:A.dim}}>{time.toLocaleTimeString()} · <span style={{color:A.red}}>CLOSED</span></div>
          </div>
          <div style={{display:"flex",gap:6}}>
            {[["● LIVE",A.green],["12 POS",A.amber],["✓ GO",A.green]].map(([l,c]) => (
              <div key={l} style={{padding:"3px 10px",background:`${c}15`,border:`1px solid ${c}40`,borderRadius:4,fontFamily:"JetBrains Mono",fontSize:10,color:c}}>{l}</div>
            ))}
          </div>
        </div>
      </div>
      {/* ── Tab bar ── */}
      <div style={{background:A.card,borderBottom:`1px solid ${A.border}`,padding:"0 22px",display:"flex",overflowX:"auto"}}>
        {TABS.map(t => (
          <button key={t.id} onClick={()=>setTab(t.id)} style={{fontFamily:"Syne",fontSize:10,fontWeight:700,letterSpacing:"0.07em",padding:"13px 15px",background:"transparent",border:"none",borderBottom:tab===t.id?`2px solid ${A.amber}`:"2px solid transparent",color:tab===t.id?A.amber:A.dim,cursor:"pointer",display:"flex",alignItems:"center",gap:5,whiteSpace:"nowrap"}}>
            <span style={{fontSize:11}}>{t.icon}</span>{t.label}
          </button>
        ))}
      </div>
      {/* ── Content ── */}
      <div style={{padding:"18px 22px",maxWidth:1480,margin:"0 auto"}}>{C[tab]}</div>
      {/* ── Footer ── */}
      <div style={{borderTop:`1px solid ${A.border}`,padding:"8px 22px",display:"flex",justifyContent:"space-between",alignItems:"center",background:A.card,marginTop:20,flexWrap:"wrap",gap:8}}>
        <div style={{fontFamily:"JetBrains Mono",fontSize:9,color:A.dim}}>Trend Following OS · v3.2.0 · NOT FINANCIAL ADVICE</div>
        <div style={{display:"flex",gap:14,flexWrap:"wrap"}}>
          {[["Universe","892"],["Qualified","15"],["Portfolio","12 pos"],["Last Rebal","2026-03-01"],["Next Exec","2026-03-04"]].map(([k,v]) => (
            <div key={k} style={{fontFamily:"JetBrains Mono",fontSize:9,color:A.dim}}>{k}: <span style={{color:A.text}}>{v}</span></div>
          ))}
        </div>
      </div>
    </div>
  );
}
