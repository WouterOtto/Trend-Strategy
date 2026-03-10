#!/usr/bin/env python3
"""
Script 1: EODHD Bulk Downloader
================================
Production-grade bulk data acquisition using EODHD API

Purpose:
    Download OHLCV data and corporate actions for all exchanges
    using EODHD's bulk endpoints for maximum efficiency

Inputs:
    - .env file with EODHD_API_KEY
    - config/exchanges.json (exchange definitions)
    - data_cache/metadata/last_update.json (for incremental mode)

Outputs:
    - data_cache/raw_bulk/{exchange}/{date}.parquet (OHLCV)
    - data_cache/metadata/last_update.json (last date per exchange)
    - data_cache/metadata/bulk_download_log.json (audit trail)
    - data_cache/metadata/new_symbols.jsonl (newly discovered symbols)
    - data_cache/metadata/delisted_symbols.jsonl (delisted/suspended symbols)
    - data_cache/corporate_actions/{exchange}_splits.parquet
    - data_cache/corporate_actions/{exchange}_dividends.parquet

Execution Modes (3 total):
    # MODE 1: INITIAL - Complete rebuild (run once at setup)
    python scripts/01_download_eodhd_bulk.py --mode initial
    Ã¢â€ â€™ Downloads 400 days for ALL symbols on all 6 exchanges
    Ã¢â€ â€™ Creates baseline for future incremental updates
    Ã¢â€ â€™ API calls: 2,412 | Time: 10-15 minutes

    # MODE 2: INCREMENTAL - Smart delta load (production workhorse)
    python scripts/01_download_eodhd_bulk.py --mode incremental
    Ã¢â€ â€™ Auto-detects gap (last_date Ã¢â€ â€™ today)
    Ã¢â€ â€™ Downloads only missing dates
    Ã¢â€ â€™ Discovers NEW symbols (IPOs, listings) automatically
    Ã¢â€ â€™ Flags DELISTED symbols automatically
    Ã¢â€ â€™ Self-healing (catches missed days automatically)
    Ã¢â€ â€™ API calls: 6-144 (depending on gap) | Time: 30s - 4min

    # MODE 3: CUSTOM - Specific date range (manual backfill)
    python scripts/01_download_eodhd_bulk.py --from 2024-01-01 --to 2024-12-31
    Ã¢â€ â€™ Downloads exact date range specified
    Ã¢â€ â€™ Useful for gap filling or testing
    Ã¢â€ â€™ API calls: varies | Time: varies

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import logging
import argparse
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================================
# CONFIGURATION
# ============================================================================

# Project paths - script is in scripts/ subdirectory
PROJECT_ROOT = Path(__file__).parent.parent  # Go up TWO levels: scripts/ -> project/
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
RAW_BULK_DIR = DATA_CACHE_DIR / "raw_bulk"
CORPORATE_ACTIONS_DIR = DATA_CACHE_DIR / "corporate_actions"
METADATA_DIR = DATA_CACHE_DIR / "metadata"
LOG_DIR = PROJECT_ROOT / "logs"

# EODHD API configuration
EODHD_BASE_URL = "https://eodhistoricaldata.com/api"
RATE_LIMIT_CALLS_PER_SECOND = 5
RATE_LIMIT_PERIOD = 1.0  # seconds
MAX_RETRIES = 3
BACKOFF_MULTIPLIER = 2

# Default history length for full mode
DEFAULT_HISTORY_DAYS = 400

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure logging with file and console output"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'download_eodhd_bulk_{timestamp}.log'
    
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
# ENVIRONMENT & CONFIG LOADING
# ============================================================================

def load_env() -> str:
    """Load EODHD API key from .env file"""
    env_file = PROJECT_ROOT / '.env'
    
    if not env_file.exists():
        raise FileNotFoundError(f".env file not found at {env_file}")
    
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()
    
    api_key = os.getenv('EODHD_API_KEY')
    if not api_key:
        raise ValueError("EODHD_API_KEY not found in .env file")
    
    logger.info("Ã¢Å“â€œ EODHD API key loaded")
    return api_key

def load_exchange_config() -> Dict:
    """Load exchange definitions from config/exchanges.json"""
    config_file = CONFIG_DIR / 'exchanges.json'
    
    if not config_file.exists():
        # Create default configuration
        logger.warning(f"Config file not found at {config_file}, creating default")
        create_default_exchange_config()
    
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    exchanges = config.get('exchanges', {})
    logger.info(f"Ã¢Å“â€œ Loaded {len(exchanges)} exchange definitions")
    
    return exchanges

def create_default_exchange_config():
    """Create default exchanges.json configuration"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    default_config = {
        "exchanges": {
            "NYSE": {
                "name": "New York Stock Exchange",
                "code": "NYSE",
                "country": "US",
                "currency": "USD",
                "trading_hours": "09:30-16:00 EST"
            },
            "NASDAQ": {
                "name": "NASDAQ Stock Market",
                "code": "NASDAQ",
                "country": "US",
                "currency": "USD",
                "trading_hours": "09:30-16:00 EST"
            },
            "XETRA": {
                "name": "Deutsche BÃƒÂ¶rse XETRA",
                "code": "XETRA",
                "country": "DE",
                "currency": "EUR",
                "trading_hours": "09:00-17:30 CET"
            },
            "LSE": {
                "name": "London Stock Exchange",
                "code": "LSE",
                "country": "GB",
                "currency": "GBP",
                "trading_hours": "08:00-16:30 GMT"
            },
            "PA": {
                "name": "Euronext Paris",
                "code": "PA",
                "country": "FR",
                "currency": "EUR",
                "trading_hours": "09:00-17:30 CET"
            },
            "AS": {
                "name": "Euronext Amsterdam",
                "code": "AS",
                "country": "NL",
                "currency": "EUR",
                "trading_hours": "09:00-17:30 CET"
            }
        }
    }
    
    config_file = CONFIG_DIR / 'exchanges.json'
    with open(config_file, 'w') as f:
        json.dump(default_config, f, indent=2)
    
    logger.info(f"Ã¢Å“â€œ Created default exchange config at {config_file}")

# ============================================================================
# API CLIENT WITH RATE LIMITING
# ============================================================================

class EODHDClient:
    """
    EODHD API client with automatic rate limiting and retry logic
    
    Rate limits:
        - 5 requests per second for bulk API
        - Automatic retry on 429 (rate limit exceeded)
        - Exponential backoff on failures
    """
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = EODHD_BASE_URL
        
        # Rate limiting
        self.last_request_time = 0
        self.min_request_interval = RATE_LIMIT_PERIOD / RATE_LIMIT_CALLS_PER_SECOND
        
        # Setup session with retry logic
        self.session = self._create_session()
    
    def _create_session(self) -> requests.Session:
        """Create requests session with retry logic"""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=MAX_RETRIES,
            status_forcelist=[429, 500, 502, 503, 504],
            backoff_factor=BACKOFF_MULTIPLIER,
            allowed_methods=["GET"]
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def _rate_limit(self):
        """Enforce rate limiting"""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            sleep_time = self.min_request_interval - elapsed
            time.sleep(sleep_time)
        
        self.last_request_time = time.time()
    
    def get_bulk_eod(self, exchange: str, date: str) -> Optional[List[Dict]]:
        """
        Fetch bulk end-of-day data for an exchange on a specific date
        
        Args:
            exchange: Exchange code (NYSE, NASDAQ, etc.)
            date: Date in YYYY-MM-DD format
        
        Returns:
            List of OHLCV records or None on error
        """
        self._rate_limit()
        
        url = f"{self.base_url}/eod-bulk-last-day/{exchange}"
        params = {
            'api_token': self.api_key,
            'date': date,
            'fmt': 'json'
        }
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            if isinstance(data, dict) and 'error' in data:
                logger.warning(f"API error for {exchange} on {date}: {data['error']}")
                return None
            
            return data
        
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.debug(f"No data for {exchange} on {date} (weekend/holiday)")
                return None
            else:
                logger.error(f"HTTP error fetching {exchange} on {date}: {e}")
                return None
        
        except Exception as e:
            logger.error(f"Error fetching {exchange} on {date}: {e}")
            return None
    
    def get_corporate_actions(
        self, 
        exchange: str, 
        action_type: str,
        from_date: str,
        to_date: str
    ) -> Optional[List[Dict]]:
        """
        Fetch corporate actions (splits or dividends) for an exchange
        
        Uses the EOD bulk endpoint with type parameter:
        https://eodhd.com/api/eod-bulk-last-day/{EXCHANGE}?type=splits
        
        Args:
            exchange: Exchange code (NYSE, NASDAQ, etc.)
            action_type: 'splits' or 'dividends'
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)
        
        Returns:
            List of corporate action records or None on error
        """
        self._rate_limit()
        
        if action_type not in ['splits', 'dividends']:
            raise ValueError("action_type must be 'splits' or 'dividends'")
        
        # Map exchange codes to bulk API codes
        # US exchanges use 'US' for bulk endpoint
        exchange_map = {
            'NYSE': 'US',
            'NASDAQ': 'US',
            'XETRA': 'XETRA',
            'LSE': 'LSE',
            'PA': 'PA',
            'AS': 'AS'
        }
        
        bulk_exchange = exchange_map.get(exchange, exchange)
        
        # Use EOD bulk endpoint with type parameter
        url = f"{self.base_url}/eod-bulk-last-day/{bulk_exchange}"
        params = {
            'api_token': self.api_key,
            'type': action_type,
            'from': from_date,
            'to': to_date,
            'fmt': 'json'
        }
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            # Check for API errors
            if isinstance(data, dict) and 'error' in data:
                logger.debug(f"API error for {exchange} {action_type}: {data['error']}")
                return None
            
            # Filter empty responses
            if not data or len(data) == 0:
                logger.debug(f"No {action_type} data for {exchange}")
                return None
            
            return data
        
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.debug(f"No data for {exchange} {action_type} (404)")
                return None
            else:
                logger.debug(f"HTTP error fetching {exchange} {action_type}: {e}")
                return None
        
        except Exception as e:
            logger.debug(f"Error fetching {exchange} {action_type}: {e}")
            return None

# ============================================================================
# METADATA MANAGEMENT
# ============================================================================

def load_last_updates() -> Dict[str, str]:
    """
    Load last update dates per exchange from metadata
    
    Returns:
        Dictionary mapping exchange code to last download date (YYYY-MM-DD)
        Example: {'NYSE': '2026-02-07', 'NASDAQ': '2026-02-07', ...}
    """
    metadata_file = METADATA_DIR / 'last_update.json'
    
    if not metadata_file.exists():
        logger.debug("No last_update.json found, starting fresh")
        return {}
    
    with open(metadata_file, 'r') as f:
        last_updates = json.load(f)
    
    logger.info(f"Ã¢Å“â€œ Loaded last update dates for {len(last_updates)} exchanges")
    return last_updates

def save_last_updates(last_updates: Dict[str, str]):
    """Save last update dates per exchange"""
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    metadata_file = METADATA_DIR / 'last_update.json'
    
    with open(metadata_file, 'w') as f:
        json.dump(last_updates, f, indent=2)
    
    logger.debug(f"Ã¢Å“â€œ Saved last_update.json")

def update_last_update(exchange: str, date: str):
    """Update last download date for a single exchange"""
    last_updates = load_last_updates()
    last_updates[exchange] = date
    save_last_updates(last_updates)

def initialize_last_updates(date: str, exchanges: Dict):
    """Initialize last_update.json for initial mode"""
    last_updates = {code: date for code in exchanges.keys()}
    save_last_updates(last_updates)
    logger.info(f"Ã¢Å“â€œ Initialized last_update.json with date: {date}")

def log_new_symbols(exchange: str, symbols: set, date: str):
    """
    Log newly discovered symbols to new_symbols.jsonl
    
    Args:
        exchange: Exchange code
        symbols: Set of new symbol codes
        date: Date they were first seen
    """
    if len(symbols) == 0:
        return
    
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = METADATA_DIR / 'new_symbols.jsonl'
    
    with open(log_file, 'a') as f:
        for symbol in symbols:
            entry = {
                'exchange': exchange,
                'symbol': symbol,
                'first_seen_date': date,
                'reason': 'new_listing',
                'timestamp': datetime.now().isoformat()
            }
            f.write(json.dumps(entry) + '\n')
    
    logger.info(f"  NEW SYMBOLS: {', '.join(sorted(list(symbols))[:10])}" + 
                (f" ... +{len(symbols)-10} more" if len(symbols) > 10 else ""))

def log_delisted_symbols(exchange: str, symbols: set, last_date: str):
    """
    Log potentially delisted symbols to delisted_symbols.jsonl
    
    Args:
        exchange: Exchange code
        symbols: Set of symbols that disappeared
        last_date: Last date they were seen
    """
    if len(symbols) == 0:
        return
    
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = METADATA_DIR / 'delisted_symbols.jsonl'
    
    with open(log_file, 'a') as f:
        for symbol in symbols:
            entry = {
                'exchange': exchange,
                'symbol': symbol,
                'last_seen_date': last_date,
                'reason': 'delisted_or_suspended',
                'timestamp': datetime.now().isoformat()
            }
            f.write(json.dumps(entry) + '\n')
    
    logger.warning(f"  POTENTIALLY DELISTED: {', '.join(sorted(list(symbols))[:10])}" + 
                   (f" ... +{len(symbols)-10} more" if len(symbols) > 10 else ""))

def get_symbols_from_cache(exchange: str, date: str) -> set:
    """
    Extract unique symbols from a cached parquet file
    
    Args:
        exchange: Exchange code
        date: Date in YYYY-MM-DD format
    
    Returns:
        Set of symbol codes present in that file
    """
    parquet_file = RAW_BULK_DIR / exchange / f"{date}.parquet"
    
    if not parquet_file.exists():
        return set()
    
    try:
        df = pd.read_parquet(parquet_file)
        if 'code' in df.columns:
            return set(df['code'].unique())
        elif 'symbol' in df.columns:
            return set(df['symbol'].unique())
        else:
            logger.warning(f"No 'code' or 'symbol' column in {parquet_file}")
            return set()
    except Exception as e:
        logger.error(f"Error reading {parquet_file}: {e}")
        return set()

def validate_no_gaps(exchanges: Dict, lookback_days: int = 300) -> bool:
    """
    Validate no gaps in last N days of cache
    
    Returns:
        True if no gaps found, False otherwise
    """
    logger.info(f"\nValidating cache (last {lookback_days} days)...")
    
    today = datetime.now().date()
    start_date = today - timedelta(days=lookback_days)
    trading_days = generate_trading_days(
        datetime.combine(start_date, datetime.min.time()),
        datetime.combine(today, datetime.min.time())
    )
    
    has_gaps = False
    
    for exchange_code in exchanges.keys():
        exchange_dir = RAW_BULK_DIR / exchange_code
        
        if not exchange_dir.exists():
            logger.warning(f"  {exchange_code}: Cache directory missing!")
            has_gaps = True
            continue
        
        missing = []
        for date_str in trading_days:
            cache_file = exchange_dir / f"{date_str}.parquet"
            if not cache_file.exists():
                missing.append(date_str)
        
        if len(missing) > 0:
            logger.warning(f"  {exchange_code}: {len(missing)} missing days (e.g., {missing[0]})")
            has_gaps = True
        else:
            logger.info(f"  {exchange_code}: Ã¢Å“â€œ No gaps")
    
    return not has_gaps

# ============================================================================
# DATA DOWNLOAD FUNCTIONS
# ============================================================================

def generate_trading_days(
    start_date: datetime,
    end_date: datetime,
    include_weekends: bool = False
) -> List[str]:
    """
    Generate list of trading days between dates

    Args:
        start_date: Start date
        end_date: End date
        include_weekends: If True, include Sat/Sun (for crypto 24/7 markets)

    Note: Does not filter for actual market holidays, just weekends.
    Market holiday filtering happens implicitly when API returns no data.
    """
    trading_days = []
    current_date = start_date

    while current_date <= end_date:
        if include_weekends or current_date.weekday() < 5:
            trading_days.append(current_date.strftime('%Y-%m-%d'))
        current_date += timedelta(days=1)

    return trading_days


def is_crypto_exchange(exchange_code: str) -> bool:
    """Return True if this is a 24/7 crypto exchange (no weekends to skip)"""
    return exchange_code.upper() in ('CC', 'CRYPTO')

def download_ohlcv_data(
    client: EODHDClient,
    exchanges: Dict,
    start_date: str,
    end_date: str
) -> Dict[str, int]:
    """
    Download OHLCV data for all exchanges in date range
    
    Returns:
        Dictionary with download statistics per exchange
    """
    stats = {}
    
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    trading_days = generate_trading_days(start_dt, end_dt)
    
    logger.info(f"Downloading OHLCV data for {len(trading_days)} trading days")
    logger.info(f"Date range: {start_date} to {end_date}")
    logger.info(f"Exchanges: {', '.join(exchanges.keys())}")
    
    total_downloads = len(exchanges) * len(trading_days)
    completed = 0
    
    for exchange_code, exchange_info in exchanges.items():
        logger.info(f"\n{'='*60}")
        logger.info(f"Exchange: {exchange_info['name']} ({exchange_code})")
        logger.info(f"{'='*60}")
        
        exchange_dir = RAW_BULK_DIR / exchange_code
        exchange_dir.mkdir(parents=True, exist_ok=True)
        
        records_saved = 0
        
        # Crypto trades 24/7 - include weekends; stocks skip weekends
        crypto = is_crypto_exchange(exchange_code)
        exchange_trading_days = generate_trading_days(
            datetime.strptime(start_date, '%Y-%m-%d'),
            datetime.strptime(end_date, '%Y-%m-%d'),
            include_weekends=crypto
        )

        # Symbols whitelist: if defined in exchange config, only keep those
        # e.g. crypto: ["BTC-USD", "ETH-USD"] to avoid downloading all 500+ pairs
        whitelist = set(exchange_info.get('symbols_whitelist', []) or [])
        if whitelist:
            logger.info(f"  Whitelist active: {', '.join(sorted(whitelist))}")

        for date_str in exchange_trading_days:
            completed += 1
            progress = (completed / total_downloads) * 100

            # Check if file already exists
            output_file = exchange_dir / f"{date_str}.parquet"
            if output_file.exists():
                logger.debug(f"[{progress:5.1f}%] Skipping {exchange_code} {date_str} (already exists)")
                continue

            logger.info(f"[{progress:5.1f}%] Fetching {exchange_code} {date_str}...")

            data = client.get_bulk_eod(exchange_code, date_str)

            if data is None or len(data) == 0:
                logger.debug(f"No data returned for {exchange_code} on {date_str}")
                continue

            # Convert to DataFrame
            df = pd.DataFrame(data)

            # Apply whitelist filter (critical for crypto: keeps only BTC-USD, ETH-USD)
            if whitelist:
                symbol_col = 'code' if 'code' in df.columns else 'symbol'
                before = len(df)
                df = df[df[symbol_col].isin(whitelist)]
                logger.debug(f"  Whitelist: {before} -> {len(df)} symbols")
                if len(df) == 0:
                    continue

            # Add metadata columns
            df['exchange'] = exchange_code
            df['date'] = date_str
            df['download_timestamp'] = datetime.now().isoformat()

            # Save to parquet
            df.to_parquet(output_file, index=False, compression='snappy')

            records_saved += len(df)
            logger.info(f"  ✓ Saved {len(df):,} symbols to {output_file.name}")
        
        stats[exchange_code] = records_saved
        logger.info(f"Ã¢Å“â€œ {exchange_code}: {records_saved:,} total records")
    
    return stats

def initial_mode(client: EODHDClient, exchanges: Dict) -> Dict[str, int]:
    """
    MODE 1: INITIAL - Complete rebuild from scratch
    
    Process:
        1. Delete existing cache (with confirmation)
        2. Calculate 400 days back from today
        3. Download all dates for all exchanges
        4. Download corporate actions (5 years)
        5. Initialize metadata
    
    Returns:
        Download statistics per exchange
    """
    logger.info(f"\n{'='*70}")
    logger.info("MODE 1: INITIAL - Complete Rebuild")
    logger.info(f"{'='*70}")
    
    # Step 1: Confirm deletion of existing cache
    if RAW_BULK_DIR.exists():
        logger.warning(f"\nÃ¢Å¡Â Ã¯Â¸Â  Existing cache found at: {RAW_BULK_DIR}")
        logger.warning("This will DELETE all cached data and download fresh.")
        
        # For automation, check for --force flag; otherwise require input
        import sys
        if '--force' in sys.argv:
            confirm = 'yes'
        else:
            confirm = input("Delete existing cache? (yes/no): ").strip().lower()
        
        if confirm == 'yes':
            import shutil
            logger.info("Deleting existing cache...")
            shutil.rmtree(RAW_BULK_DIR)
            logger.info("Ã¢Å“â€œ Cache deleted")
        else:
            logger.error("Aborted by user")
            sys.exit(1)
    
    # Step 2: Calculate 400 days back
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=400)
    
    logger.info(f"\nDownload range: {start_date} to {end_date} (400 days)")
    logger.info(f"Exchanges: {len(exchanges)}")
    
    # Step 3: Download all dates
    stats = download_ohlcv_data(
        client,
        exchanges,
        start_date.strftime('%Y-%m-%d'),
        end_date.strftime('%Y-%m-%d')
    )
    
    # Step 4: Initialize metadata
    initialize_last_updates(end_date.strftime('%Y-%m-%d'), exchanges)
    
    return stats

def incremental_mode(client: EODHDClient, exchanges: Dict) -> Dict[str, int]:
    """
    MODE 2: INCREMENTAL - Smart delta load with auto-discovery
    
    Process:
        1. Load last update date per exchange
        2. Calculate date range (last_date + 1 Ã¢â€ â€™ today)
        3. Download only trading days in range
        4. Detect NEW symbols (IPOs, new listings)
        5. Detect DELISTED symbols (on last date only)
        6. Update corporate actions for delta period
        7. Update last update metadata
        8. Validate no gaps in last 300 days
    
    Returns:
        Download statistics per exchange
    """
    logger.info(f"\n{'='*70}")
    logger.info("MODE 2: INCREMENTAL - Smart Delta Load")
    logger.info(f"{'='*70}")
    
    # Step 1: Load last update dates
    last_updates = load_last_updates()
    
    if len(last_updates) == 0:
        logger.error("No baseline found. Run --mode initial first.")
        sys.exit(1)
    
    today = datetime.now().date()
    stats = {}
    
    for exchange_code, exchange_info in exchanges.items():
        last_date_str = last_updates.get(exchange_code)
        
        # FIX: Don't skip exchanges with no baseline - try to download them
        if last_date_str is None:
            logger.warning(f"{exchange_code}: No baseline found. Attempting first-time download...")
            logger.info(f"  Will download same date range as working exchanges")
            
            # Use most recent last_date from SIMILAR exchanges (crypto vs stocks)
            # Problem: if CC (crypto) has 2026-02-25 and stocks have 2026-02-11,
            # a new stock exchange (LSE) should use 2026-02-11, not 2026-02-25!
            if len(last_updates) > 0:
                # Determine if this exchange is crypto or stock
                is_crypto_new = is_crypto_exchange(exchange_code)
                
                # Find dates from similar exchanges (crypto to crypto, stock to stock)
                similar_dates = []
                for other_code, other_date in last_updates.items():
                    if is_crypto_exchange(other_code) == is_crypto_new:
                        similar_dates.append(datetime.strptime(other_date, '%Y-%m-%d').date())
                
                if similar_dates:
                    reference_date = max(similar_dates)
                    logger.info(f"  Using reference from similar exchanges: {reference_date}")
                else:
                    # No similar exchanges, use any available date
                    reference_date = max(datetime.strptime(d, '%Y-%m-%d').date() 
                                       for d in last_updates.values())
                    logger.info(f"  Using reference from any exchange: {reference_date}")
                
                last_date = reference_date
                last_date_str = reference_date.strftime('%Y-%m-%d')
                last_date_str = last_date.strftime('%Y-%m-%d')
                logger.info(f"  Using yesterday as reference: {last_date_str}")
        else:
            last_date = datetime.strptime(last_date_str, '%Y-%m-%d').date()
        
        # Step 2: Calculate date range (last_date + 1 Ã¢â€ â€™ today)
        start_date = last_date + timedelta(days=1)
        end_date = today
        
        # Step 3: Get trading days (crypto includes weekends)
        trading_days = generate_trading_days(
            datetime.combine(start_date, datetime.min.time()),
            datetime.combine(end_date, datetime.min.time()),
            include_weekends=is_crypto_exchange(exchange_code)
        )
        
        if len(trading_days) == 0:
            logger.info(f"{exchange_code}: Already up to date (last: {last_date_str})")
            stats[exchange_code] = 0
            continue
        
        logger.info(f"\n{'='*60}")
        logger.info(f"Exchange: {exchange_info['name']} ({exchange_code})")
        logger.info(f"{'='*60}")
        logger.info(f"Last update: {last_date_str}")
        logger.info(f"Downloading: {len(trading_days)} days ({start_date} Ã¢â€ â€™ {end_date})")
        
        # Step 4: Track symbols for new/delisted detection
        previous_symbols = get_symbols_from_cache(exchange_code, last_date_str)
        
        exchange_dir = RAW_BULK_DIR / exchange_code
        exchange_dir.mkdir(parents=True, exist_ok=True)
        
        records_saved = 0
        
        for idx, date_str in enumerate(trading_days, 1):
            progress = (idx / len(trading_days)) * 100
            logger.info(f"[{progress:5.1f}%] Fetching {exchange_code} {date_str}...")
            
            # Bulk API call (returns ALL symbols on exchange for this date)
            data = client.get_bulk_eod(exchange_code, date_str)
            
            if data is None or len(data) == 0:
                logger.debug(f"  No data returned (weekend/holiday)")
                continue
            
            # Convert to DataFrame
            df = pd.DataFrame(data)
            
            # Add metadata
            df['exchange'] = exchange_code
            df['date'] = date_str
            df['download_timestamp'] = datetime.now().isoformat()
            
            # Get current symbols (use 'code' or 'symbol' column)
            symbol_col = 'code' if 'code' in df.columns else 'symbol'
            current_symbols = set(df[symbol_col].unique())
            
            # Step 5: Detect NEW symbols (IPOs, new listings)
            if len(previous_symbols) > 0:
                new_symbols = current_symbols - previous_symbols
                if len(new_symbols) > 0:
                    logger.info(f"  Ã°Å¸â€ â€¢ {len(new_symbols)} new symbols detected")
                    log_new_symbols(exchange_code, new_symbols, date_str)
            
            # Step 6: Detect DELISTED symbols (on last date only)
            if date_str == trading_days[-1] and len(previous_symbols) > 0:
                potentially_delisted = previous_symbols - current_symbols
                if len(potentially_delisted) > 0:
                    logger.warning(f"  Ã¢Å¡Â Ã¯Â¸Â  {len(potentially_delisted)} symbols missing")
                    log_delisted_symbols(exchange_code, potentially_delisted, last_date_str)
            
            # Save data
            output_file = exchange_dir / f"{date_str}.parquet"
            df.to_parquet(output_file, index=False, compression='snappy')
            
            records_saved += len(df)
            logger.info(f"  Ã¢Å“â€œ Saved {len(df):,} symbols")
            
            previous_symbols = current_symbols
        
        # Step 7: Update last update metadata
        if len(trading_days) > 0:
            update_last_update(exchange_code, trading_days[-1])
        
        stats[exchange_code] = records_saved
        logger.info(f"Ã¢Å“â€œ {exchange_code}: {records_saved:,} total records")
    

    # Step 7: Update corporate actions for the delta period
    # Calculate overall date range (earliest start to today)
    if len(stats) > 0 and any(v > 0 for v in stats.values()):
        # Find the earliest date we downloaded
        earliest_date = today
        for exchange_code in exchanges.keys():
            last_date_str = last_updates.get(exchange_code)
            if last_date_str:
                last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                if last_date < earliest_date:
                    earliest_date = last_date

        # Update corporate actions for (earliest_date â†’ today)
        corp_start = (earliest_date + timedelta(days=1)).strftime("%Y-%m-%d")
        corp_end = today.strftime("%Y-%m-%d")

        logger.info("\n" + "=" * 70)
        logger.info(f"Step 7: Updating corporate actions for {corp_start} â†’ {corp_end}")
        logger.info("=" * 70)

        corp_stats = update_corporate_actions_incremental(
            client,
            exchanges,
            corp_start,
            corp_end
        )
    else:
        logger.info("\nNo OHLCV data downloaded, skipping corporate actions update")
        corp_stats = {}

    # Step 9: Validate no gaps in last 300 days
    validate_no_gaps(exchanges, lookback_days=300)
    
    return stats

def custom_mode(
    client: EODHDClient,
    exchanges: Dict,
    from_date: str,
    to_date: str
) -> Dict[str, int]:
    """
    MODE 3: CUSTOM - Download specific date range
    
    Process:
        1. Download exact date range specified
        2. Does NOT update metadata (to avoid interfering with incremental mode)
    
    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
    
    Returns:
        Download statistics per exchange
    """
    logger.info(f"\n{'='*70}")
    logger.info("MODE 3: CUSTOM - Specific Date Range")
    logger.info(f"{'='*70}")
    
    start_date = datetime.strptime(from_date, '%Y-%m-%d').date()
    end_date = datetime.strptime(to_date, '%Y-%m-%d').date()
    
    trading_days = generate_trading_days(
        datetime.combine(start_date, datetime.min.time()),
        datetime.combine(end_date, datetime.min.time())
    )
    
    logger.info(f"Custom download: {len(trading_days)} trading days ({start_date} Ã¢â€ â€™ {end_date})")
    logger.info("Note: Does NOT update metadata (manual backfill mode)")
    
    stats = download_ohlcv_data(client, exchanges, from_date, to_date)

    # Update last_update metadata for any exchange that received data.
    # This is critical when using --exchanges CC to backfill a single exchange:
    # without this, the next incremental run would re-download from the wrong date.
    updated = []
    for exchange_code, record_count in stats.items():
        if record_count > 0:
            update_last_update(exchange_code, to_date)
            updated.append(exchange_code)
    if updated:
        logger.info(f"\nMetadata updated for: {', '.join(updated)}")
        logger.info(f"  last_update set to: {to_date}")
    else:
        logger.info("\nNo records downloaded - metadata NOT updated")

    return stats

def download_corporate_actions(
    client: EODHDClient,
    exchanges: Dict,
    start_date: str,
    end_date: str
) -> Dict[str, Dict[str, int]]:
    """
    Download corporate actions (splits and dividends) for all exchanges
    
    Uses EOD bulk endpoint with type parameter:
      /api/eod-bulk-last-day/{EXCHANGE}?type=splits&from=...&to=...
    
    This approach works better than separate /splits-bulk/ endpoints.
    
    Returns:
        Nested dictionary with stats per exchange and action type
    """
    stats = {}
    
    logger.info(f"\n{'='*60}")
    logger.info("DOWNLOADING CORPORATE ACTIONS")
    logger.info(f"{'='*60}")
    logger.info(f"Date range: {start_date} to {end_date}")
    logger.info("Using: EOD bulk endpoint with type parameter")
    
    CORPORATE_ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Track which exchanges have data
    exchanges_with_data = []
    exchanges_without_data = []
    
    for exchange_code, exchange_info in exchanges.items():
        logger.info(f"\n{exchange_info['name']} ({exchange_code}):")
        stats[exchange_code] = {}
        
        has_data = False
        
        for action_type in ['splits', 'dividends']:
            logger.info(f"  Fetching {action_type}...")
            
            output_file = CORPORATE_ACTIONS_DIR / f"{exchange_code}_{action_type}.parquet"
            
            data = client.get_corporate_actions(
                exchange_code,
                action_type,
                start_date,
                end_date
            )
            
            if data is None or len(data) == 0:
                logger.info(f"    No {action_type} data available")
                stats[exchange_code][action_type] = 0
                continue
            
            # Convert to DataFrame
            df = pd.DataFrame(data)
            
            # Add metadata
            df['exchange'] = exchange_code
            df['action_type'] = action_type
            df['download_timestamp'] = datetime.now().isoformat()
            
            # Save to parquet
            df.to_parquet(output_file, index=False, compression='snappy')
            
            stats[exchange_code][action_type] = len(df)
            logger.info(f"    Ã¢Å“â€œ Saved {len(df):,} {action_type}")
            
            has_data = True
        
        # Track exchanges with data
        if has_data:
            exchanges_with_data.append(exchange_code)
        else:
            exchanges_without_data.append(exchange_code)
    
    # Summary
    if len(exchanges_with_data) > 0:
        logger.info(f"\nÃ¢Å“â€œ Exchanges with corporate actions: {', '.join(exchanges_with_data)}")
    
    if len(exchanges_without_data) > 0:
        logger.info(f"\nÃ¢â€žÂ¹Ã¯Â¸Â  Exchanges without corporate actions: {', '.join(exchanges_without_data)}")
        logger.info("   (No data in date range or not available for exchange)")
    
    return stats

def update_corporate_actions_incremental(
    client: EODHDClient,
    exchanges: Dict,
    start_date: str,
    end_date: str
) -> Dict[str, Dict[str, int]]:
    """
    Update corporate actions for incremental date range
    
    Appends new splits/dividends to existing files or creates new ones
    
    Args:
        client: EODHD API client
        exchanges: Dictionary of exchange configurations
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    
    Returns:
        Nested dictionary with stats per exchange and action type
    """
    stats = {}
    
    logger.info(f"\n{'='*60}")
    logger.info("UPDATING CORPORATE ACTIONS (INCREMENTAL)")
    logger.info(f"{'='*60}")
    logger.info(f"Date range: {start_date} to {end_date}")
    
    CORPORATE_ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    for exchange_code, exchange_info in exchanges.items():
        logger.info(f"\n{exchange_info['name']} ({exchange_code}):")
        stats[exchange_code] = {}
        
        for action_type in ['splits', 'dividends']:
            logger.info(f"  Fetching {action_type}...")
            
            # Fetch new data for date range
            data = client.get_corporate_actions(
                exchange_code,
                action_type,
                start_date,
                end_date
            )
            
            if data is None or len(data) == 0:
                logger.info(f"    No new {action_type}")
                stats[exchange_code][action_type] = 0
                continue
            
            # Convert to DataFrame
            new_df = pd.DataFrame(data)
            new_df['exchange'] = exchange_code
            new_df['action_type'] = action_type
            new_df['download_timestamp'] = datetime.now().isoformat()
            
            output_file = CORPORATE_ACTIONS_DIR / f"{exchange_code}_{action_type}.parquet"
            
            # Check if file exists
            if output_file.exists():
                # Append to existing
                existing_df = pd.read_parquet(output_file)
                
                # Combine and deduplicate
                combined_df = pd.concat([existing_df, new_df], ignore_index=True)
                
                # Deduplicate by code + date (keep last occurrence)
                if 'code' in combined_df.columns and 'date' in combined_df.columns:
                    combined_df = combined_df.drop_duplicates(
                        subset=['code', 'date'], 
                        keep='last'
                    )
                
                # Save back
                combined_df.to_parquet(output_file, index=False, compression='snappy')
                
                new_records = len(new_df)
                total_records = len(combined_df)
                logger.info(f"    âœ“ Added {new_records:,} new {action_type} (total: {total_records:,})")
            else:
                # First time, just save
                new_df.to_parquet(output_file, index=False, compression='snappy')
                logger.info(f"    âœ“ Saved {len(new_df):,} {action_type} (new file)")
            
            stats[exchange_code][action_type] = len(new_df)
    
    return stats

# ============================================================================
# AUDIT LOGGING
# ============================================================================

def save_download_log(
    mode: str,
    start_date: str,
    end_date: str,
    ohlcv_stats: Dict,
    corporate_stats: Dict,
    start_time: datetime,
    end_time: datetime
):
    """Save comprehensive audit log of download session"""
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    
    log_file = METADATA_DIR / 'bulk_download_log.json'
    
    # Load existing logs or create new
    if log_file.exists():
        with open(log_file, 'r') as f:
            logs = json.load(f)
    else:
        logs = []
    
    # Create new log entry
    log_entry = {
        'session_id': datetime.now().strftime('%Y%m%d_%H%M%S'),
        'mode': mode,
        'date_range': {
            'start': start_date,
            'end': end_date
        },
        'execution': {
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': (end_time - start_time).total_seconds()
        },
        'ohlcv_stats': ohlcv_stats,
        'corporate_actions_stats': corporate_stats,
        'script_version': '1.0.0',
        'architecture_version': '3.1'
    }
    
    logs.append(log_entry)
    
    # Save updated logs
    with open(log_file, 'w') as f:
        json.dump(logs, f, indent=2)
    
    logger.info(f"\nÃ¢Å“â€œ Audit log saved to {log_file}")

# ============================================================================
# MAIN EXECUTION
# ============================================================================

def parse_arguments():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(
        description='EODHD Bulk Data Downloader - Script 1 (v3.2)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Execution Modes (3 total):

  MODE 1: INITIAL - Complete rebuild (run once at setup)
    python 01_download_eodhd_bulk.py --mode initial
    Ã¢â€ â€™ Downloads 400 days for ALL symbols on all 6 exchanges
    Ã¢â€ â€™ Creates baseline for future incremental updates
    Ã¢â€ â€™ API calls: ~2,412 | Time: 10-15 minutes

  MODE 2: INCREMENTAL - Smart delta load (production workhorse)
    python 01_download_eodhd_bulk.py --mode incremental
    Ã¢â€ â€™ Auto-detects gap (last_date Ã¢â€ â€™ today)
    Ã¢â€ â€™ Downloads only missing dates
    Ã¢â€ â€™ Discovers NEW symbols (IPOs, listings) automatically
    Ã¢â€ â€™ Flags DELISTED symbols automatically
    Ã¢â€ â€™ Self-healing (catches missed days automatically)
    Ã¢â€ â€™ API calls: 6-144 (depending on gap) | Time: 30s - 4min

  MODE 3: CUSTOM - Specific date range (manual backfill)
    python 01_download_eodhd_bulk.py --from 2024-01-01 --to 2024-12-31
    Ã¢â€ â€™ Downloads exact date range specified
    Ã¢â€ â€™ Useful for gap filling or testing
    Ã¢â€ â€™ Does NOT update metadata
    Ã¢â€ â€™ API calls: varies | Time: varies

Flags:
  --force    Skip confirmation prompts (for automation)

Examples:
  # Initial setup (first time)
  python 01_download_eodhd_bulk.py --mode initial
  
  # Daily operations (automated cron)
  0 6 * * 1-5 python 01_download_eodhd_bulk.py --mode incremental
  
  # Manual gap repair
  python 01_download_eodhd_bulk.py --from 2025-12-15 --to 2025-12-20
        """
    )
    
    parser.add_argument(
        '--mode',
        choices=['initial', 'incremental'],
        help='Download mode: initial (full rebuild) or incremental (delta update)'
    )
    
    parser.add_argument(
        '--from',
        dest='from_date',
        help='Start date for custom mode (YYYY-MM-DD)'
    )
    
    parser.add_argument(
        '--to',
        dest='to_date',
        help='End date for custom mode (YYYY-MM-DD)'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompts (for automation)'
    )

    parser.add_argument(
        '--exchanges',
        dest='exchanges_filter',
        default=None,
        help=(
            'Comma-separated list of exchange codes to process '
            '(e.g. --exchanges CC or --exchanges CC,LSE). '
            'Applies to all modes. Omit to process all exchanges in config.'
        )
    )

    args = parser.parse_args()
    
    # Validate arguments
    if not args.mode and not (args.from_date and args.to_date):
        parser.error("Must specify either --mode or both --from and --to")
    
    if args.mode and (args.from_date or args.to_date):
        parser.error("Cannot use --mode with --from/--to")
    
    return args

def main():
    """Main execution function"""
    start_time = datetime.now()
    
    logger.info("="*70)
    logger.info("EODHD BULK DOWNLOADER - Script 1")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("="*70)
    
    # Parse arguments
    args = parse_arguments()
    
    # Determine execution mode
    if args.from_date and args.to_date:
        mode = 'custom'
        logger.info(f"\nExecution Mode: CUSTOM")
        logger.info(f"Date Range: {args.from_date} to {args.to_date}")
    else:
        mode = args.mode
        logger.info(f"\nExecution Mode: {mode.upper()}")
    
    # Load configuration
    try:
        api_key = load_env()
        exchanges = load_exchange_config()
    except Exception as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Filter exchanges if --exchanges flag was provided
    if args.exchanges_filter:
        requested = [e.strip().upper() for e in args.exchanges_filter.split(',')]
        unknown = [e for e in requested if e not in exchanges]
        if unknown:
            logger.error(f"Unknown exchange(s): {', '.join(unknown)}")
            logger.error(f"Available: {', '.join(exchanges.keys())}")
            sys.exit(1)
        exchanges = {k: v for k, v in exchanges.items() if k in requested}
        logger.info(f"Exchange filter active: {', '.join(exchanges.keys())}")
    
    # Create API client
    client = EODHDClient(api_key)
    logger.info("Ã¢Å“â€œ EODHD client initialized with rate limiting")
    
    # Execute appropriate mode
    try:
        logger.info(f"\n{'='*70}")
        logger.info("PHASE 1: OHLCV DATA DOWNLOAD")
        logger.info(f"{'='*70}")
        
        if mode == 'initial':
            ohlcv_stats = initial_mode(client, exchanges)
            date_range = {
                'start': (datetime.now().date() - timedelta(days=400)).strftime('%Y-%m-%d'),
                'end': datetime.now().date().strftime('%Y-%m-%d')
            }
        elif mode == 'incremental':
            ohlcv_stats = incremental_mode(client, exchanges)
            last_updates = load_last_updates()
            date_range = {
                'start': min(last_updates.values()) if last_updates else 'N/A',
                'end': datetime.now().date().strftime('%Y-%m-%d')
            }
        elif mode == 'custom':
            ohlcv_stats = custom_mode(client, exchanges, args.from_date, args.to_date)
            date_range = {
                'start': args.from_date,
                'end': args.to_date
            }
        else:
            logger.error(f"Unknown mode: {mode}")
            sys.exit(1)
        
        logger.info(f"\n{'='*70}")
        logger.info("OHLCV DOWNLOAD SUMMARY")
        logger.info(f"{'='*70}")
        for exchange, records in ohlcv_stats.items():
            logger.info(f"{exchange:10s}: {records:,} records")
        
    except Exception as e:
        logger.error(f"Error downloading OHLCV data: {e}", exc_info=True)
        sys.exit(1)
    
    # Download corporate actions (skip for incremental mode unless it's initial setup)
    try:
        logger.info(f"\n{'='*70}")
        logger.info("PHASE 2: CORPORATE ACTIONS DOWNLOAD")
        logger.info(f"{'='*70}")
        
        # For initial mode: download 5 years of corporate actions
        # For incremental mode: skip (handled separately or on-demand)
        # For custom mode: download for specified range
        #
        # NOTE: Not all exchanges support bulk corporate actions endpoints.
        # Missing data is expected and normal. Corporate actions can be
        # fetched per-symbol using individual endpoints in later stages if needed.
        
        if mode == 'initial':
            corp_start = (datetime.now().date() - timedelta(days=1825)).strftime('%Y-%m-%d')  # 5 years
            corp_end = datetime.now().date().strftime('%Y-%m-%d')
            logger.info(f"Downloading 5 years of corporate actions ({corp_start} to {corp_end})")
            corporate_stats = download_corporate_actions(client, exchanges, corp_start, corp_end)
        elif mode == 'incremental':
            logger.info("Skipping corporate actions (incremental mode)")
            logger.info("Tip: Corporate actions can be updated separately if needed")
            corporate_stats = {}
        elif mode == 'custom':
            logger.info(f"Downloading corporate actions for custom range")
            corporate_stats = download_corporate_actions(
                client, exchanges, 
                args.from_date, args.to_date
            )
        
        if corporate_stats:
            logger.info(f"\n{'='*70}")
            logger.info("CORPORATE ACTIONS SUMMARY")
            logger.info(f"{'='*70}")
            for exchange, actions in corporate_stats.items():
                splits = actions.get('splits', 0)
                divs = actions.get('dividends', 0)
                if splits > 0 or divs > 0:
                    logger.info(f"{exchange:10s}: Ã¢Å“â€œ splits={splits:,}, dividends={divs:,}")
                else:
                    logger.info(f"{exchange:10s}: Ã¢â‚¬â€ (no bulk data available)")
        
    except Exception as e:
        logger.warning(f"Error downloading corporate actions: {e}")
        logger.info("Continuing without corporate actions data (can be fetched per-symbol later)")
        # Continue even if corporate actions fail
        corporate_stats = {}
    
    # Save audit log
    end_time = datetime.now()
    duration = end_time - start_time
    
    save_download_log(
        mode,
        date_range['start'],
        date_range['end'],
        ohlcv_stats,
        corporate_stats,
        start_time,
        end_time
    )
    
    # Final summary
    logger.info(f"\n{'='*70}")
    logger.info("DOWNLOAD COMPLETE")
    logger.info(f"{'='*70}")
    logger.info(f"Mode: {mode.upper()}")
    logger.info(f"Duration: {duration}")
    logger.info(f"OHLCV records: {sum(ohlcv_stats.values()):,}")
    logger.info(f"Exchanges processed: {len(exchanges)}")
    logger.info(f"Data location: {RAW_BULK_DIR}")
    
    if mode == 'incremental':
        logger.info(f"Metadata: {METADATA_DIR / 'last_update.json'}")
        logger.info(f"New symbols: {METADATA_DIR / 'new_symbols.jsonl'}")
        logger.info(f"Delisted: {METADATA_DIR / 'delisted_symbols.jsonl'}")
    
    logger.info(f"Audit log: {METADATA_DIR / 'bulk_download_log.json'}")
    logger.info("="*70)
    
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\n\nDownload interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
