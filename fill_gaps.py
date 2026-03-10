#!/usr/bin/env python3
"""
Fill Missing Dates - Gap Filler
================================
Downloads only the missing trading days identified by identify_gaps.py

Usage:
    python fill_gaps.py                    # Fill all exchanges
    python fill_gaps.py --exchange NYSE    # Fill specific exchange
    python fill_gaps.py --dry-run          # Show what would be downloaded
    python fill_gaps.py --max-gaps 5       # Limit number of gaps to fill
"""

import os
import sys
import argparse
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Set
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Project paths
PROJECT_ROOT = Path(__file__).parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
RAW_BULK_DIR = DATA_CACHE_DIR / "raw_bulk"
CONFIG_DIR = PROJECT_ROOT / "config"
LOG_DIR = PROJECT_ROOT / "logs"

# Setup logging
LOG_DIR.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
log_file = LOG_DIR / f'fill_gaps_{timestamp}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# EODHD API configuration
RATE_LIMIT_CALLS_PER_SECOND = 5
RATE_LIMIT_PERIOD = 1.0

def load_env() -> str:
    """Load API key from .env file"""
    env_file = PROJECT_ROOT / '.env'
    
    if not env_file.exists():
        raise FileNotFoundError(f".env file not found at {env_file}")
    
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if key.strip() == 'EODHD_API_KEY':
                    return value.strip()
    
    raise ValueError("EODHD_API_KEY not found in .env file")

def is_crypto_exchange(exchange_code: str) -> bool:
    """Return True if this is a 24/7 crypto exchange"""
    return exchange_code.upper() in ('CC', 'CRYPTO')

def generate_expected_days(start_date: datetime, end_date: datetime, include_weekends: bool = False) -> Set[str]:
    """Generate set of expected trading days"""
    expected = set()
    current = start_date
    
    while current <= end_date:
        if include_weekends or current.weekday() < 5:
            expected.add(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)
    
    return expected

def get_existing_files(exchange_dir: Path) -> Set[str]:
    """Get set of dates that have data files"""
    if not exchange_dir.exists():
        return set()
    
    return {f.stem for f in exchange_dir.glob("*.parquet")}

def find_gaps(exchange_code: str, lookback_days: int = 400, from_date: str = '2023-01-01') -> List[str]:
    """Find all missing dates for an exchange"""
    exchange_dir = RAW_BULK_DIR / exchange_code
    
    if not exchange_dir.exists():
        logger.warning(f"{exchange_code}: No data directory - run initial mode first")
        return []
    
    existing_dates = get_existing_files(exchange_dir)
    
    if len(existing_dates) == 0:
        logger.warning(f"{exchange_code}: No files - run initial mode first")
        return []
    
    # Calculate date range
    existing_date_objs = [datetime.strptime(d, '%Y-%m-%d') for d in existing_dates]
    earliest = min(existing_date_objs)
    latest = max(existing_date_objs)
    
    # Limit to lookback period
    end_date = datetime.now()
    # Calculate start date: either --from date or lookback
    if from_date:
        # User specified --from date, use it directly
        start_date = datetime.strptime(from_date, '%Y-%m-%d')
    else:
        # Fallback to lookback
        start_date = end_date - timedelta(days=lookback_days)
    analysis_start = max(earliest, start_date)
    # CRITICAL: Check to TODAY, not latest file (detects gaps at the end)
    analysis_end = end_date
    
    # Generate expected dates
    is_crypto = is_crypto_exchange(exchange_code)
    expected_dates = generate_expected_days(analysis_start, analysis_end, include_weekends=is_crypto)
    
    # Find and sort missing dates
    missing_dates = sorted(expected_dates - existing_dates)
    
    return missing_dates

class EODHDClient:
    """Simple EODHD API client with rate limiting"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://eodhistoricaldata.com/api"
        self.last_call_time = 0
        
        # Setup session with retries
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def _rate_limit(self):
        """Enforce rate limiting"""
        elapsed = time.time() - self.last_call_time
        if elapsed < RATE_LIMIT_PERIOD:
            time.sleep(RATE_LIMIT_PERIOD - elapsed)
        self.last_call_time = time.time()
    
    def get_bulk_eod(self, exchange: str, date: str) -> List[Dict]:
        """Fetch bulk EOD data for an exchange on a specific date"""
        self._rate_limit()
        
        url = f"{self.base_url}/eod-bulk-last-day/{exchange}"
        params = {
            'api_token': self.api_key,
            'date': date,
            'fmt': 'json'
        }
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.debug(f"No data for {exchange} on {date} (404)")
                return []
            elif response.status_code == 402:
                logger.error(f"Payment required for {exchange} (not in your plan)")
                return []
            else:
                logger.warning(f"HTTP {response.status_code} for {exchange} {date}")
                return []
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error for {exchange} {date}: {e}")
            return []

def fill_exchange_gaps(
    client: EODHDClient,
    exchange_code: str,
    exchange_info: Dict,
    missing_dates: List[str],
    dry_run: bool = False
) -> int:
    """Fill gaps for one exchange"""
    
    if not missing_dates:
        logger.info(f"{exchange_code}: No gaps to fill ✅")
        return 0
    
    logger.info(f"\n{'='*60}")
    logger.info(f"Exchange: {exchange_info.get('name', exchange_code)} ({exchange_code})")
    logger.info(f"{'='*60}")
    logger.info(f"Missing dates: {len(missing_dates)}")
    logger.info(f"Date range: {missing_dates[0]} → {missing_dates[-1]}")
    
    if dry_run:
        logger.info("DRY RUN - would download these dates:")
        for date in missing_dates[:10]:
            logger.info(f"  {date}")
        if len(missing_dates) > 10:
            logger.info(f"  ... and {len(missing_dates) - 10} more")
        return 0
    
    # Get whitelist if defined
    whitelist = set(exchange_info.get('symbols_whitelist', []) or [])
    if whitelist:
        logger.info(f"Whitelist active: {', '.join(sorted(whitelist))}")
    
    # Create exchange directory
    exchange_dir = RAW_BULK_DIR / exchange_code
    exchange_dir.mkdir(parents=True, exist_ok=True)
    
    # Download each missing date
    records_saved = 0
    successful = 0
    
    for idx, date_str in enumerate(missing_dates, 1):
        progress = (idx / len(missing_dates)) * 100
        logger.info(f"[{progress:5.1f}%] Fetching {exchange_code} {date_str}...")
        
        # Skip if file already exists (shouldn't happen, but defensive)
        output_file = exchange_dir / f"{date_str}.parquet"
        if output_file.exists():
            logger.debug(f"  File exists, skipping")
            continue
        
        # Fetch data
        data = client.get_bulk_eod(exchange_code, date_str)
        
        if data is None or len(data) == 0:
            logger.debug(f"  No data returned (holiday/weekend)")
            continue
        
        # Convert to DataFrame
        df = pd.DataFrame(data)
        
        # Apply whitelist if defined
        if whitelist:
            symbol_col = 'code' if 'code' in df.columns else 'symbol'
            before = len(df)
            df = df[df[symbol_col].isin(whitelist)]
            logger.debug(f"  Whitelist: {before} → {len(df)} symbols")
            
            if len(df) == 0:
                logger.debug(f"  No whitelisted symbols")
                continue
        
        # Add metadata
        df['exchange'] = exchange_code
        df['date'] = date_str
        df['download_timestamp'] = datetime.now().isoformat()
        
        # Save
        df.to_parquet(output_file, index=False, compression='snappy')
        
        records_saved += len(df)
        successful += 1
        logger.info(f"  ✓ Saved {len(df):,} symbols")
    
    logger.info(f"\n✓ {exchange_code}: Filled {successful}/{len(missing_dates)} dates, {records_saved:,} records")
    return records_saved

def main():
    parser = argparse.ArgumentParser(
        description='Fill missing trading days (gaps) in downloaded data'
    )
    parser.add_argument(
        '--exchange',
        help='Fill gaps for specific exchange only (e.g., NYSE)'
    )
    parser.add_argument(
        '--lookback',
        type=int,
        default=400,
        help='Days to look back (default: from 2023-01-01, or specify custom days)'
    )
    parser.add_argument(
        '--from',
        dest='from_date',
        default='2023-01-01',
        help='Start date for analysis (default: 2023-01-01, format: YYYY-MM-DD)'
    )
    parser.add_argument(
        '--max-gaps',
        type=int,
        help='Maximum number of missing dates to fill per exchange'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be downloaded without actually downloading'
    )
    
    args = parser.parse_args()
    
    start_time = datetime.now()
    
    logger.info("="*70)
    logger.info("GAP FILLER - Download Missing Trading Days")
    logger.info("="*70)
    logger.info(f"Log file: {log_file}")
    logger.info(f"Analysis start date: {args.from_date}")
    if args.max_gaps:
        logger.info(f"Max gaps per exchange: {args.max_gaps}")
    if args.dry_run:
        logger.info("DRY RUN MODE - no downloads will occur")
    logger.info("")
    
    # Load configuration
    try:
        api_key = load_env()
        
        config_file = CONFIG_DIR / "exchanges.json"
        if not config_file.exists():
            logger.error(f"Config file not found: {config_file}")
            sys.exit(1)
        
        import json
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        exchanges = config.get('exchanges', {})
        
    except Exception as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    
    # Filter to specific exchange if requested
    if args.exchange:
        if args.exchange.upper() not in exchanges:
            logger.error(f"Exchange {args.exchange} not found in config")
            sys.exit(1)
        exchanges = {args.exchange.upper(): exchanges[args.exchange.upper()]}
    
    # Create API client
    client = EODHDClient(api_key)
    logger.info("✓ EODHD client initialized")
    
    # Find and fill gaps for each exchange
    total_records = 0
    total_gaps_filled = 0
    
    for exchange_code, exchange_info in exchanges.items():
        # Find missing dates
        missing_dates = find_gaps(exchange_code, args.lookback, args.from_date)
        
        if not missing_dates:
            logger.info(f"{exchange_code}: No gaps found ✅")
            continue
        
        # Limit if requested
        if args.max_gaps and len(missing_dates) > args.max_gaps:
            logger.info(f"{exchange_code}: Limiting to first {args.max_gaps} gaps (of {len(missing_dates)} total)")
            missing_dates = missing_dates[:args.max_gaps]
        
        # Fill the gaps
        records = fill_exchange_gaps(
            client,
            exchange_code,
            exchange_info,
            missing_dates,
            dry_run=args.dry_run
        )
        
        total_records += records
        if records > 0:
            total_gaps_filled += len(missing_dates)
    
    # Summary
    end_time = datetime.now()
    duration = end_time - start_time
    
    logger.info(f"\n{'='*70}")
    logger.info("GAP FILL COMPLETE")
    logger.info(f"{'='*70}")
    logger.info(f"Duration: {duration}")
    logger.info(f"Total gaps filled: {total_gaps_filled}")
    logger.info(f"Total records: {total_records:,}")
    logger.info(f"Log file: {log_file}")
    logger.info("="*70)
    
    if not args.dry_run and total_records > 0:
        logger.info("\nℹ️  Note: last_update.json NOT modified (gaps filled, not forward progress)")
        logger.info("   Run incremental mode to update to latest date")

if __name__ == '__main__':
    main()
