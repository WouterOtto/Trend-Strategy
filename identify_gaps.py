#!/usr/bin/env python3
"""
Identify Missing Dates - Diagnostic Tool
=========================================
Scans data_cache/raw_bulk/ and identifies which trading days are missing per exchange

Usage:
    python identify_gaps.py
    python identify_gaps.py --exchange NYSE
    python identify_gaps.py --lookback 400
"""

import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Set

# Project paths
PROJECT_ROOT = Path(__file__).parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
RAW_BULK_DIR = DATA_CACHE_DIR / "raw_bulk"
CONFIG_DIR = PROJECT_ROOT / "config"

def is_crypto_exchange(exchange_code: str) -> bool:
    """Return True if this is a 24/7 crypto exchange"""
    return exchange_code.upper() in ('CC', 'CRYPTO')

def generate_expected_days(start_date: datetime, end_date: datetime, include_weekends: bool = False) -> Set[str]:
    """Generate set of expected trading days"""
    expected = set()
    current = start_date
    
    while current <= end_date:
        if include_weekends or current.weekday() < 5:  # Monday=0, Friday=4
            expected.add(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)
    
    return expected

def get_existing_files(exchange_dir: Path) -> Set[str]:
    """Get set of dates that have data files"""
    if not exchange_dir.exists():
        return set()
    
    return {f.stem for f in exchange_dir.glob("*.parquet")}

def analyze_exchange(exchange_code: str, lookback_days: int = 400, from_date: str = '2023-01-01') -> Dict:
    """Analyze one exchange for missing dates"""
    exchange_dir = RAW_BULK_DIR / exchange_code
    
    if not exchange_dir.exists():
        return {
            'exchange': exchange_code,
            'status': 'no_data',
            'missing_count': 0,
            'existing_count': 0,
            'missing_dates': []
        }
    
    # Get existing files
    existing_dates = get_existing_files(exchange_dir)
    
    if len(existing_dates) == 0:
        return {
            'exchange': exchange_code,
            'status': 'empty',
            'missing_count': 0,
            'existing_count': 0,
            'missing_dates': []
        }
    
    # Calculate date range
    existing_date_objs = [datetime.strptime(d, '%Y-%m-%d') for d in existing_dates]
    earliest = min(existing_date_objs)
    latest = max(existing_date_objs)
    
    # If lookback specified, limit the range
    end_date = datetime.now()
    # Calculate start date: either --from date or lookback
    if from_date:
        # User specified --from date, use it directly
        start_date = datetime.strptime(from_date, '%Y-%m-%d')
    else:
        # Fallback to lookback
        start_date = end_date - timedelta(days=lookback_days)
    
    # Use the later of (earliest existing, specified start)
    analysis_start = max(earliest, start_date)
    # CRITICAL: Check to TODAY, not latest file (detects gaps at the end)
    analysis_end = end_date
    
    # Generate expected dates
    is_crypto = is_crypto_exchange(exchange_code)
    expected_dates = generate_expected_days(analysis_start, analysis_end, include_weekends=is_crypto)
    
    # Find missing dates
    missing_dates = sorted(expected_dates - existing_dates)
    
    # Classify gaps
    gap_groups = []
    if missing_dates:
        current_gap = [missing_dates[0]]
        for i in range(1, len(missing_dates)):
            prev_date = datetime.strptime(missing_dates[i-1], '%Y-%m-%d')
            curr_date = datetime.strptime(missing_dates[i], '%Y-%m-%d')
            
            # If dates are consecutive (allowing for weekends)
            days_apart = (curr_date - prev_date).days
            if days_apart <= 3:  # Same gap
                current_gap.append(missing_dates[i])
            else:  # New gap
                gap_groups.append(current_gap)
                current_gap = [missing_dates[i]]
        
        gap_groups.append(current_gap)
    
    return {
        'exchange': exchange_code,
        'status': 'ok',
        'date_range': {
            'earliest': earliest.strftime('%Y-%m-%d'),
            'latest': latest.strftime('%Y-%m-%d'),
            'analysis_start': analysis_start.strftime('%Y-%m-%d'),
            'analysis_end': analysis_end.strftime('%Y-%m-%d')
        },
        'existing_count': len(existing_dates),
        'expected_count': len(expected_dates),
        'missing_count': len(missing_dates),
        'missing_dates': missing_dates,
        'gap_groups': gap_groups,
        'largest_gap': max([len(g) for g in gap_groups]) if gap_groups else 0
    }

def load_exchanges() -> Dict:
    """Load exchange configuration"""
    config_file = CONFIG_DIR / "exchanges.json"
    
    if not config_file.exists():
        return {}
    
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    return config.get('exchanges', {})

def main():
    parser = argparse.ArgumentParser(
        description='Identify missing trading days per exchange'
    )
    parser.add_argument(
        '--exchange',
        help='Analyze specific exchange only (e.g., NYSE)'
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
        '--show-dates',
        action='store_true',
        help='Show all missing dates (not just count)'
    )
    
    args = parser.parse_args()
    
    print("="*80)
    print("GAP ANALYSIS - Missing Trading Days")
    print("="*80)
    print(f"Analysis start date: {args.from_date}")
    print()
    
    # Load exchanges
    exchanges = load_exchanges()
    
    if not exchanges:
        print("⚠️  No exchanges configured in config/exchanges.json")
        return
    
    # Filter to specific exchange if requested
    if args.exchange:
        if args.exchange.upper() not in exchanges:
            print(f"❌ Exchange {args.exchange} not found in config")
            return
        exchanges = {args.exchange.upper(): exchanges[args.exchange.upper()]}
    
    # Analyze each exchange
    results = []
    total_missing = 0
    
    for exchange_code in exchanges.keys():
        analysis = analyze_exchange(exchange_code, args.lookback, args.from_date)
        results.append(analysis)
        
        print(f"{'='*80}")
        print(f"Exchange: {exchange_code}")
        print(f"{'='*80}")
        
        if analysis['status'] == 'no_data':
            print("❌ No data directory found")
            print("   Run: python scripts/01_download_eodhd_bulk.py --mode initial")
        elif analysis['status'] == 'empty':
            print("❌ Directory exists but contains no files")
        else:
            print(f"Date range: {analysis['date_range']['earliest']} → {analysis['date_range']['latest']}")
            print(f"Analysis period: {analysis['date_range']['analysis_start']} → {analysis['date_range']['analysis_end']}")
            print(f"Files found: {analysis['existing_count']:,}")
            print(f"Expected: {analysis['expected_count']:,}")
            print(f"Missing: {analysis['missing_count']:,}")
            
            if analysis['missing_count'] == 0:
                print("✅ Complete - no gaps!")
            else:
                pct_missing = (analysis['missing_count'] / analysis['expected_count']) * 100
                print(f"Gap coverage: {100 - pct_missing:.1f}%")
                print(f"Number of gaps: {len(analysis['gap_groups'])}")
                print(f"Largest gap: {analysis['largest_gap']} days")
                
                # Show gap details
                if len(analysis['gap_groups']) <= 10:
                    print("\nGap details:")
                    for idx, gap in enumerate(analysis['gap_groups'], 1):
                        if len(gap) == 1:
                            print(f"  Gap {idx}: {gap[0]} (1 day)")
                        else:
                            print(f"  Gap {idx}: {gap[0]} → {gap[-1]} ({len(gap)} days)")
                else:
                    print(f"\nShowing first 10 gaps (of {len(analysis['gap_groups'])} total):")
                    for idx, gap in enumerate(analysis['gap_groups'][:10], 1):
                        if len(gap) == 1:
                            print(f"  Gap {idx}: {gap[0]} (1 day)")
                        else:
                            print(f"  Gap {idx}: {gap[0]} → {gap[-1]} ({len(gap)} days)")
                
                if args.show_dates:
                    print(f"\nAll missing dates:")
                    for date in analysis['missing_dates']:
                        print(f"  {date}")
                
                total_missing += analysis['missing_count']
        
        print()
    
    # Summary
    print("="*80)
    print("SUMMARY")
    print("="*80)
    
    complete_exchanges = [r for r in results if r['status'] == 'ok' and r['missing_count'] == 0]
    incomplete_exchanges = [r for r in results if r['status'] == 'ok' and r['missing_count'] > 0]
    no_data_exchanges = [r for r in results if r['status'] in ('no_data', 'empty')]
    
    if complete_exchanges:
        print(f"✅ Complete exchanges ({len(complete_exchanges)}):")
        for r in complete_exchanges:
            print(f"   {r['exchange']}: {r['existing_count']:,} files")
    
    if incomplete_exchanges:
        print(f"\n⚠️  Exchanges with gaps ({len(incomplete_exchanges)}):")
        for r in incomplete_exchanges:
            print(f"   {r['exchange']}: {r['missing_count']:,} missing days ({len(r['gap_groups'])} gaps)")
    
    if no_data_exchanges:
        print(f"\n❌ Exchanges with no data ({len(no_data_exchanges)}):")
        for r in no_data_exchanges:
            print(f"   {r['exchange']}")
    
    if total_missing > 0:
        print()
        print("="*80)
        print("RECOMMENDED ACTION:")
        print("="*80)
        print()
        print(f"Total missing dates: {total_missing:,}")
        print()
        print("To fill all gaps, run:")
        print("  python fill_gaps.py")
        print()
        print("To fill specific exchange:")
        print("  python fill_gaps.py --exchange NYSE")

if __name__ == '__main__':
    main()
