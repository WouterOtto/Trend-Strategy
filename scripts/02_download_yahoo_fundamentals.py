#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script 2: Yahoo Finance Fundamentals Downloader (Exchange-Aware)
==================================================================
Production-grade company metadata acquisition using Yahoo Finance API

CRITICAL: This script is EXCHANGE-AWARE. European symbols are stored WITHOUT
suffix in EODHD bulk data (e.g. "BMW" in XETRA/file.parquet) but Yahoo Finance
requires the suffix (e.g. "BMW.DE"). The script preserves exchange context
throughout the entire fetch pipeline.

Purpose:
    Fetch company fundamentals (name, sector, industry, market cap, etc.)
    for all symbols discovered by the EODHD bulk downloader.

Key fields captured:
    instrument_type  : 'stock', 'etf', 'mutual_fund', 'index', etc.
    is_equity        : True only for real stocks (EQUITY quoteType)
    sector/industry  : Empty for ETFs by design
    exchange_code    : EODHD exchange (NASDAQ, XETRA, etc.)

Inputs:
    - data_cache/raw_bulk/{exchange}/*.parquet
    - data_cache/fundamentals/company_info.json

Outputs:
    - data_cache/fundamentals/company_info.json
    - data_cache/fundamentals/fetch_failures.jsonl
    - data_cache/metadata/fundamentals_log.json

Modes:
    full        : Fetch all symbols (respects cache)
    incremental : Fetch only new symbols
    refresh     : Re-fetch all symbols (ignore cache)
    patch       : Backfill instrument_type + exchange_code into old cache

Architecture: v3.2 (Feb 2026)
"""

import sys
import json
import logging
import argparse
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, NamedTuple
from collections import defaultdict
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

PROJECT_ROOT     = Path(__file__).parent.parent
DATA_CACHE_DIR   = PROJECT_ROOT / "data_cache"
RAW_BULK_DIR     = DATA_CACHE_DIR / "raw_bulk"
FUNDAMENTALS_DIR = DATA_CACHE_DIR / "fundamentals"
METADATA_DIR     = DATA_CACHE_DIR / "metadata"
LOG_DIR          = PROJECT_ROOT / "logs"

RATE_LIMIT_REQUESTS_PER_SECOND = 2
RATE_LIMIT_PERIOD              = 1.0
MAX_RETRIES                    = 3
RETRY_DELAY                    = 5
CHECKPOINT_EVERY               = 100

# CRITICAL: Exchange suffix mapping for Yahoo Finance
# EODHD stores "BMW" in XETRA directory → Yahoo needs "BMW.DE"
EXCHANGE_SUFFIX_MAP = {
    'XETRA': '.DE',   # Frankfurt/XETRA
    'LSE':   '.L',    # London Stock Exchange
    'PA':    '.PA',   # Euronext Paris
    'AS':    '.AS',   # Euronext Amsterdam
    'NYSE':  '',      # US exchanges: no suffix for Yahoo
    'NASDAQ':'',
    'NCM':   '',
    'NGM':   '',
    'NMS':   '',
}

# EODHD format uses these suffixes (matches their API)
# Note: Crypto (CC) symbols are stored WITH -USD already (e.g. 'BTC-USD')
#       so we use empty suffix to avoid BTC-USD.CC format
EODHD_SUFFIX_MAP = {
    'XETRA': '.DE',
    'LSE':   '.L',
    'PA':    '.PA',
    'AS':    '.AS',
    'CC':    '',      # Crypto already has -USD in symbol field
    'NYSE':  '.US',   # EODHD uses .US for all US exchanges
    'NASDAQ':'.US',
    'NCM':   '.US',
    'NGM':   '.US',
    'NMS':   '.US',
}

# Yahoo Finance quoteType mapping
QUOTE_TYPE_MAP = {
    'EQUITY':         'stock',
    'ETF':            'etf',
    'MUTUALFUND':     'mutual_fund',
    'INDEX':          'index',
    'FUTURE':         'future',
    'CURRENCY':       'currency',
    'CRYPTOCURRENCY': 'crypto',
    'NONE':           'unknown',  # Yahoo returns 'NONE' for delisted/invalid symbols
    '':               'unknown',
}

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'download_yahoo_fundamentals_{ts}.log'

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Log file: {log_file}")
    return logger

logger = setup_logging()

# ============================================================================
# DATA STRUCTURES
# ============================================================================

class SymbolContext(NamedTuple):
    """Symbol with its exchange context preserved"""
    symbol: str        # Raw symbol from EODHD (e.g. "BMW", "00XL")
    exchange: str      # EODHD exchange code (e.g. "XETRA", "NASDAQ")

# ============================================================================
# SYMBOL EXTRACTION (EXCHANGE-AWARE)
# ============================================================================

def extract_all_symbols() -> List[SymbolContext]:
    """
    Extract symbols from raw_bulk cache, PRESERVING exchange context.

    Returns:
        List of SymbolContext(symbol='BMW', exchange='XETRA')
    """
    logger.info("\n" + "="*60)
    logger.info("EXTRACTING SYMBOLS (EXCHANGE-AWARE)")
    logger.info("="*60)

    if not RAW_BULK_DIR.exists():
        logger.error(f"Raw bulk cache not found: {RAW_BULK_DIR}")
        sys.exit(1)

    exchanges = [d for d in RAW_BULK_DIR.iterdir() if d.is_dir()]
    if not exchanges:
        logger.error("No exchange directories found")
        sys.exit(1)

    logger.info(f"Exchanges: {', '.join(e.name for e in exchanges)}")

    result: List[SymbolContext] = []
    stats_by_exchange: Dict[str, int] = {}

    for exchange_dir in exchanges:
        exchange_code = exchange_dir.name
        parquet_files = list(exchange_dir.glob("*.parquet"))

        if not parquet_files:
            logger.warning(f"{exchange_code}: No files")
            continue

        latest = max(parquet_files, key=lambda p: p.stem)

        try:
            df = pd.read_parquet(latest)
            col = 'code' if 'code' in df.columns else 'symbol' if 'symbol' in df.columns else None

            if col is None:
                logger.warning(f"{exchange_code}: No symbol column")
                continue

            symbols = df[col].dropna().unique()
            
            for sym in symbols:
                result.append(SymbolContext(symbol=str(sym), exchange=exchange_code))

            stats_by_exchange[exchange_code] = len(symbols)
            logger.info(f"  {exchange_code:10s}: {len(symbols):,} symbols")

        except Exception as e:
            logger.error(f"{exchange_code}: {e}")

    logger.info(f"\nTotal: {len(result):,} symbols across {len(stats_by_exchange)} exchanges")
    return result

# ============================================================================
# SYMBOL CONVERSION (EXCHANGE-AWARE)
# ============================================================================

def convert_to_yahoo(ctx: SymbolContext) -> str:
    """
    Convert EODHD symbol to Yahoo Finance format using exchange context.

    Examples:
      SymbolContext('BMW', 'XETRA')      → 'BMW.DE'
      SymbolContext('00XL', 'XETRA')     → '00XL.DE'
      SymbolContext('AAPL', 'NASDAQ')    → 'AAPL'
      SymbolContext('BTC-USD', 'CC')     → 'BTC-USD' (no change!)
      SymbolContext('ETH-USD', 'CC')     → 'ETH-USD' (no change!)
    
    Note: EODHD stores crypto with -USD suffix already in the symbol field,
          so we don't add it again!
    """
    # Crypto: EODHD already has -USD in the symbol (e.g. 'BTC-USD')
    # Yahoo also expects 'BTC-USD', so return as-is
    if ctx.exchange == 'CC':
        return ctx.symbol  # Already has -USD suffix from EODHD

    # Add exchange suffix for European markets
    suffix = EXCHANGE_SUFFIX_MAP.get(ctx.exchange, '')
    return ctx.symbol + suffix


def to_eodhd_symbol(ctx: SymbolContext) -> str:
    """
    Convert to EODHD standard format (used for cache keys and architecture).
    
    EODHD API format:
      - US stocks:   AAPL.US, MSFT.US
      - German:      BMW.DE, SAP.DE
      - UK:          VOD.L, BP.L
      - French:      AIR.PA
      - Dutch:       ASML.AS
      - Crypto:      BTC.CC, ETH.CC
    
    This is the canonical format used throughout the pipeline.
    
    Examples:
      SymbolContext('AAPL', 'NASDAQ') → 'AAPL.US'
      SymbolContext('BMW', 'XETRA')   → 'BMW.DE'
      SymbolContext('BTC', 'CC')      → 'BTC.CC'
    """
    suffix = EODHD_SUFFIX_MAP.get(ctx.exchange, '')
    return ctx.symbol + suffix

# ============================================================================
# JUNK FILTER (EXCHANGE-AWARE)
# ============================================================================

def is_junk_symbol(ctx: SymbolContext) -> bool:
    """
    Return True if symbol should be skipped.

    KEY: European symbols CAN start with digits (e.g. 00XL.DE is valid).
    Only US symbols are filtered by digit prefix.

    US exchange filters (NASDAQ, NYSE, etc.):
      - Starts with digit      (3APE = structured product)
      - Starts with '0P'       (0P0001O70F = Morningstar ID)
      - Length > 5             (AAIDX, AACBWR)
      - Ends 'WS', 'W', 'U', 'Q', 'R'  (warrants, units, rights, bankrupt)

    European exchange filters (XETRA, LSE, PA, AS):
      - Only filter extremely long symbols (> 10 chars)
      - European naming conventions are different, be permissive
    """
    # European/crypto markets: minimal filtering
    if ctx.exchange in ('XETRA', 'LSE', 'PA', 'AS', 'CC'):
        # Only filter extremely abnormal symbols
        if len(ctx.symbol) > 10:
            return True
        return False

    # From here: US market filters only
    symbol = ctx.symbol

    # Structured products starting with digit
    if symbol[0].isdigit():
        return True

    # Morningstar mutual fund IDs (but NOT for PA exchange where they're valid!)
    if symbol.startswith('0P') and ctx.exchange != 'PA':
        return True

    # Very long tickers are unusual
    if len(symbol) > 5:
        return True

    # Warrant variants
    if symbol.endswith('WS'):
        return True

    # Warrants, units, bankrupt (len > 3 to preserve 'T', 'F', etc.)
    if len(symbol) > 3 and symbol[-1] in ('W', 'U', 'Q'):
        return True

    # Rights (len > 4 to preserve 4-char tickers like AADR)
    if len(symbol) > 4 and symbol[-1] == 'R':
        return True

    return False

# ============================================================================
# CACHE I/O
# ============================================================================

def load_cache() -> Dict[str, Dict]:
    cache_file = FUNDAMENTALS_DIR / 'company_info.json'
    if not cache_file.exists():
        return {}
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"[OK] Loaded cache: {len(data):,} entries")
        return data
    except Exception as e:
        logger.error(f"Cache load failed: {e}")
        return {}


def save_cache(cache: Dict[str, Dict]):
    FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)
    with open(FUNDAMENTALS_DIR / 'company_info.json', 'w', encoding='utf-8') as f:
        json.dump(dict(sorted(cache.items())), f, indent=2, ensure_ascii=False)


def log_failure(symbol: str, exchange: str, reason: str):
    FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)
    with open(FUNDAMENTALS_DIR / 'fetch_failures.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps({
            'symbol': symbol,
            'exchange': exchange,
            'reason': reason,
            'timestamp': datetime.now().isoformat()
        }) + '\n')

# ============================================================================
# YAHOO FINANCE CLIENT
# ============================================================================

class YahooFinanceClient:
    """Rate-limited Yahoo Finance client"""

    def __init__(self):
        self.last_request = 0.0
        self.interval     = RATE_LIMIT_PERIOD / RATE_LIMIT_REQUESTS_PER_SECOND
        self.fetch_count  = 0

    def _rate_limit(self):
        elapsed = time.time() - self.last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_request = time.time()

    def fetch(self, ctx: SymbolContext) -> Optional[Dict]:
        """
        Fetch company info for SymbolContext.
        Returns None on failure.
        """
        self._rate_limit()
        self.fetch_count += 1

        yahoo_symbol = convert_to_yahoo(ctx)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                info = yf.Ticker(yahoo_symbol).info

                if not info:
                    return None

                if not any(k in info for k in ('symbol', 'shortName', 'longName')):
                    return None

                quote_type = info.get('quoteType', '').upper()
                inst_type  = QUOTE_TYPE_MAP.get(quote_type, quote_type.lower() or 'unknown')

                return {
                    'symbol':          ctx.symbol,
                    'exchange_code':   ctx.exchange,
                    'yahoo_symbol':    yahoo_symbol,
                    'instrument_type': inst_type,
                    'is_equity':       inst_type == 'stock',
                    'name':            info.get('longName') or info.get('shortName', ''),
                    'sector':          info.get('sector', ''),
                    'industry':        info.get('industry', ''),
                    'market_cap':      info.get('marketCap') or 0,
                    'currency':        info.get('currency', ''),
                    'exchange':        info.get('exchange', ''),
                    'country':         info.get('country', ''),
                    'employees':       info.get('fullTimeEmployees') or 0,
                    'website':         info.get('website', ''),
                    'business_summary':info.get('longBusinessSummary', '')[:500],
                    'fetched_at':      datetime.now().isoformat(),
                    'source':          'yahoo',
                }

            except Exception as e:
                if attempt < MAX_RETRIES:
                    logger.debug(f"  {yahoo_symbol}: retry {attempt}/{MAX_RETRIES} ({e})")
                    time.sleep(RETRY_DELAY)
                else:
                    logger.debug(f"  {yahoo_symbol}: failed after {MAX_RETRIES} attempts")
                    return None

        return None

# ============================================================================
# FETCH LOOP
# ============================================================================

def _fetch_loop(
    client: YahooFinanceClient,
    contexts: List[SymbolContext],
    cache: Dict[str, Dict],
    label: str
) -> Tuple[int, int, int]:
    """
    Core fetch loop.
    Returns (successful, failed, skipped)
    """
    succ = fail = skip = 0
    total = len(contexts)

    for idx, ctx in enumerate(contexts, 1):
        pct = idx / total * 100

        if is_junk_symbol(ctx):
            skip += 1
            logger.debug(f"[{pct:5.1f}%] Skip: {ctx.exchange}:{ctx.symbol}")
            continue

        logger.info(f"[{pct:5.1f}%] {label} {ctx.exchange}:{ctx.symbol}")
        result = client.fetch(ctx)

        if result:
            # Cache key: {symbol}.{exchange} (matches EODHD bulk extraction format)
            # Examples: "AAPL.NASDAQ", "BMW.XETRA", "VOD.LSE", "BTC.CC"
            # This matches Script 1 output and Script 3 input format exactly
            cache_key = f"{ctx.symbol}.{ctx.exchange}"
            cache[cache_key] = result
            succ += 1
            itype = result['instrument_type']
            logger.info(f"  [OK] {result['name'] or '?'} [{itype}]")
        else:
            fail += 1
            log_failure(ctx.symbol, ctx.exchange, "No data from Yahoo")
            logger.warning(f"  [FAIL]")

        if idx % CHECKPOINT_EVERY == 0:
            save_cache(cache)

    return succ, fail, skip


def _log_summary(
    title: str,
    total: int,
    succ: int,
    fail: int,
    skip: int,
    cache: Dict[str, Dict]
):
    """Print mode summary"""
    fetched = succ + fail
    logger.info(f"\n{'='*60}")
    logger.info(title)
    logger.info(f"{'='*60}")
    logger.info(f"Input:        {total:,}")
    logger.info(f"Skipped:      {skip:,}")
    logger.info(f"Fetched:      {fetched:,}")
    logger.info(f"  Success:    {succ:,}")
    logger.info(f"  Failed:     {fail:,}")
    if fetched:
        logger.info(f"Success rate: {succ/fetched*100:.1f}%")

    # Breakdown by instrument type
    type_counts: Dict[str, int] = {}
    for info in cache.values():
        t = info.get('instrument_type', 'unknown')
        type_counts[t] = type_counts.get(t, 0) + 1

    logger.info(f"\nCache: {len(cache):,} entries")
    logger.info("By instrument type:")
    for t, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
        tag = " <- trend universe" if t == 'stock' else ""
        logger.info(f"  {t:20s}: {cnt:,}{tag}")

# ============================================================================
# EXECUTION MODES
# ============================================================================

def full_mode(
    client: YahooFinanceClient,
    max_symbols: Optional[int] = None
) -> Tuple[Dict[str, Dict], int, int]:
    """MODE 1: Fetch all symbols (skip cached)"""
    logger.info("\n" + "="*70)
    logger.info("MODE 1: FULL")
    logger.info("="*70)

    all_contexts = extract_all_symbols()
    if max_symbols:
        logger.info(f"[TEST] Limit: {max_symbols}")
        all_contexts = all_contexts[:max_symbols]

    cache = load_cache()
    
    # Filter out already-cached symbols
    # Cache keys use format: {symbol}.{exchange} (matches EODHD bulk format)
    to_fetch = []
    for ctx in all_contexts:
        cache_key = f"{ctx.symbol}.{ctx.exchange}"
        if cache_key not in cache:
            to_fetch.append(ctx)

    logger.info(f"\nTotal: {len(all_contexts):,} | Cached: {len(cache):,} | To fetch: {len(to_fetch):,}")

    if not to_fetch:
        logger.info("[OK] All cached")
        return cache, 0, 0

    s, f, sk = _fetch_loop(client, to_fetch, cache, "Fetch")
    save_cache(cache)
    _log_summary("FULL MODE SUMMARY", len(to_fetch), s, f, sk, cache)
    return cache, s, f


def incremental_mode(
    client: YahooFinanceClient
) -> Tuple[Dict[str, Dict], int, int]:
    """MODE 2: Fetch only new symbols"""
    logger.info("\n" + "="*70)
    logger.info("MODE 2: INCREMENTAL")
    logger.info("="*70)

    all_contexts = extract_all_symbols()
    cache = load_cache()

    if not cache:
        logger.warning("[WARN] Cache empty. Use --mode full")
        return full_mode(client)

    new_contexts = []
    for ctx in all_contexts:
        cache_key = f"{ctx.symbol}.{ctx.exchange}"
        if cache_key not in cache:
            new_contexts.append(ctx)

    logger.info(f"\nTotal: {len(all_contexts):,} | Cached: {len(cache):,} | New: {len(new_contexts):,}")

    if not new_contexts:
        logger.info("[OK] No new symbols")
        return cache, 0, 0

    s, f, sk = _fetch_loop(client, new_contexts, cache, "Fetch")
    save_cache(cache)
    _log_summary("INCREMENTAL SUMMARY", len(new_contexts), s, f, sk, cache)
    return cache, s, f


def refresh_mode(
    client: YahooFinanceClient,
    max_symbols: Optional[int] = None
) -> Tuple[Dict[str, Dict], int, int]:
    """MODE 3: Re-fetch all symbols (ignore cache)"""
    logger.info("\n" + "="*70)
    logger.info("MODE 3: REFRESH")
    logger.info("="*70)

    all_contexts = extract_all_symbols()
    if max_symbols:
        logger.info(f"[TEST] Limit: {max_symbols}")
        all_contexts = all_contexts[:max_symbols]

    cache = {}
    s, f, sk = _fetch_loop(client, all_contexts, cache, "Refresh")
    save_cache(cache)
    _log_summary("REFRESH SUMMARY", len(all_contexts), s, f, sk, cache)
    return cache, s, f


def patch_mode(
    client: YahooFinanceClient
) -> Tuple[Dict[str, Dict], int, int]:
    """
    MODE 4: Backfill instrument_type + exchange_code into old cache.

    Old cache may use various key formats:
      - Bare symbol: {"AAPL": {...}}
      - Old @-format: {"BMW@XETRA": {...}}

    New cache uses Yahoo symbol as key (architecture standard):
      - {"AAPL": {...}}         (US stocks, no suffix)
      - {"BMW.DE": {...}}       (German stocks)
      - {"BTC-USD": {...}}      (Crypto)

    This mode:
      1. Loads old cache
      2. Extracts current symbol universe
      3. Re-fetches entries missing instrument_type/exchange_code
      4. Migrates all keys to yahoo_symbol format
    """
    logger.info("\n" + "="*70)
    logger.info("MODE 4: PATCH - Migrate to yahoo_symbol cache keys")
    logger.info("="*70)

    old_cache = load_cache()
    if not old_cache:
        logger.warning("[WARN] Cache is empty. Run --mode full first.")
        return old_cache, 0, 0

    # Extract current symbol universe with exchange context
    all_contexts = extract_all_symbols()
    
    # Build mapping: (symbol, exchange) → SymbolContext
    # This handles both formats: bare "BMW" and "@-format" "BMW@XETRA"
    symbol_lookup: Dict[Tuple[str, str], SymbolContext] = {}
    for ctx in all_contexts:
        symbol_lookup[(ctx.symbol, ctx.exchange)] = ctx

    # Identify entries needing patch and build new cache
    new_cache: Dict[str, Dict] = {}
    to_patch: List[SymbolContext] = []
    migrated = 0

    for old_key, old_info in old_cache.items():
        # Parse old key format
        if '@' in old_key:
            # Old format: "BMW@XETRA"
            symbol, exchange = old_key.split('@', 1)
        else:
            # Bare symbol: "AAPL" or "BMW.DE"
            # Try to infer exchange from the entry
            symbol = old_key
            exchange = old_info.get('exchange_code', '')
            
            if not exchange:
                # No exchange code stored - skip or guess?
                # For now, try to find it in symbol_lookup
                found = False
                for (sym, exc), ctx in symbol_lookup.items():
                    if sym == symbol or convert_to_yahoo(ctx) == symbol:
                        symbol = sym
                        exchange = exc
                        found = True
                        break
                if not found:
                    # Can't determine exchange - keep as-is for now
                    logger.debug(f"Cannot determine exchange for {old_key}, keeping")
                    new_cache[old_key] = old_info
                    migrated += 1
                    continue

        # Look up the context
        ctx = symbol_lookup.get((symbol, exchange))
        
        if not ctx:
            # Symbol no longer in current universe, keep old entry
            logger.debug(f"Symbol {symbol}@{exchange} not in current universe, keeping")
            new_cache[old_key] = old_info
            migrated += 1
            continue

        # Generate correct yahoo symbol
        yahoo_sym = convert_to_yahoo(ctx)

        # Check if needs patching
        needs_patch = (
            'instrument_type' not in old_info or
            'exchange_code' not in old_info or
            old_key != yahoo_sym  # Key format needs migration
        )

        if needs_patch:
            to_patch.append(ctx)
        else:
            # Already correct, just copy with correct key
            new_cache[yahoo_sym] = old_info
            migrated += 1

    logger.info(f"\nOld cache entries:       {len(old_cache):,}")
    logger.info(f"Need patching:           {len(to_patch):,}")
    logger.info(f"Already correct:         {migrated:,}")

    if not to_patch:
        logger.info("[OK] All entries already correct")
        return new_cache, 0, 0

    # Fetch missing/outdated entries
    succ = fail = skip = 0
    for idx, ctx in enumerate(to_patch, 1):
        pct = idx / len(to_patch) * 100

        yahoo_sym = convert_to_yahoo(ctx)

        if is_junk_symbol(ctx):
            # Mark as unknown
            new_cache[yahoo_sym] = {
                'symbol': ctx.symbol,
                'exchange_code': ctx.exchange,
                'yahoo_symbol': yahoo_sym,
                'instrument_type': 'unknown',
                'is_equity': False,
            }
            skip += 1
            logger.debug(f"[{pct:5.1f}%] Junk: {ctx.exchange}:{ctx.symbol}")
            continue

        logger.info(f"[{pct:5.1f}%] Patch {ctx.exchange}:{ctx.symbol}")
        result = client.fetch(ctx)

        if result:
            new_cache[yahoo_sym] = result
            succ += 1
            logger.info(f"  [OK] {result['name']} [{result['instrument_type']}]")
        else:
            # Try to preserve old data if exists
            old_key_variants = [
                f"{ctx.symbol}@{ctx.exchange}",
                ctx.symbol,
                yahoo_sym
            ]
            old_data = None
            for variant in old_key_variants:
                if variant in old_cache:
                    old_data = old_cache[variant].copy()
                    break
            
            if old_data:
                old_data.update({
                    'symbol': ctx.symbol,
                    'exchange_code': ctx.exchange,
                    'yahoo_symbol': yahoo_sym,
                    'instrument_type': old_data.get('instrument_type', 'unknown'),
                    'is_equity': old_data.get('is_equity', False),
                })
                new_cache[yahoo_sym] = old_data
            else:
                new_cache[yahoo_sym] = {
                    'symbol': ctx.symbol,
                    'exchange_code': ctx.exchange,
                    'yahoo_symbol': yahoo_sym,
                    'instrument_type': 'unknown',
                    'is_equity': False,
                }
            fail += 1
            logger.warning(f"  [FAIL]")

        if idx % CHECKPOINT_EVERY == 0:
            save_cache(new_cache)

    save_cache(new_cache)
    _log_summary("PATCH SUMMARY", len(to_patch), succ, fail, skip, new_cache)
    return new_cache, succ, fail

# ============================================================================
# AUDIT LOG
# ============================================================================

def save_audit(
    mode: str,
    succ: int,
    fail: int,
    total_cached: int,
    start: datetime,
    end: datetime,
    fetch_count: int
):
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = METADATA_DIR / 'fundamentals_log.json'
    logs = []

    if log_file.exists():
        try:
            with open(log_file) as f:
                logs = json.load(f)
        except Exception:
            pass

    logs.append({
        'session_id': datetime.now().strftime('%Y%m%d_%H%M%S'),
        'mode': mode,
        'api_calls': fetch_count,
        'successful': succ,
        'failed': fail,
        'success_rate_pct': round(succ / fetch_count * 100, 1) if fetch_count else 0,
        'total_cached': total_cached,
        'duration_seconds': (end - start).total_seconds(),
        'script_version': '2.1.0',
        'architecture': '3.2',
    })

    with open(log_file, 'w') as f:
        json.dump(logs, f, indent=2)

    logger.info(f"[OK] Audit: {log_file}")

# ============================================================================
# CLI
# ============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Yahoo Finance Fundamentals Downloader v2.1 (Exchange-Aware)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/02_download_yahoo_fundamentals.py --mode full
  python scripts/02_download_yahoo_fundamentals.py --mode incremental
  python scripts/02_download_yahoo_fundamentals.py --mode patch
  python scripts/02_download_yahoo_fundamentals.py --mode full --max-symbols 50
        """
    )
    p.add_argument('--mode', required=True,
                   choices=['full', 'incremental', 'refresh', 'patch'])
    p.add_argument('--max-symbols', type=int)
    return p.parse_args()


def main() -> int:
    start = datetime.now()

    logger.info("="*70)
    logger.info("YAHOO FINANCE FUNDAMENTALS DOWNLOADER v2.1 (Exchange-Aware)")
    logger.info("="*70)

    args = parse_args()
    client = YahooFinanceClient()

    logger.info(f"Mode: {args.mode.upper()}")
    if args.max_symbols:
        logger.info(f"Limit: {args.max_symbols} symbols")

    try:
        if args.mode == 'full':
            cache, s, f = full_mode(client, args.max_symbols)
        elif args.mode == 'incremental':
            cache, s, f = incremental_mode(client)
        elif args.mode == 'refresh':
            cache, s, f = refresh_mode(client, args.max_symbols)
        elif args.mode == 'patch':
            cache, s, f = patch_mode(client)
        else:
            logger.error(f"Unknown mode: {args.mode}")
            return 1

    except Exception as e:
        logger.error(f"Fatal: {e}", exc_info=True)
        return 1

    end = datetime.now()
    save_audit(args.mode, s, f, len(cache), start, end, client.fetch_count)

    logger.info(f"\n{'='*70}")
    logger.info("COMPLETE")
    logger.info(f"{'='*70}")
    logger.info(f"Duration:  {end - start}")
    logger.info(f"API calls: {client.fetch_count:,}")
    logger.info(f"Success:   {s:,}")
    logger.info(f"Failed:    {f:,}")
    logger.info(f"Cached:    {len(cache):,}")
    logger.info(f"\nCache: {FUNDAMENTALS_DIR / 'company_info.json'}")
    logger.info("="*70)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\nInterrupted")
        sys.exit(1)
