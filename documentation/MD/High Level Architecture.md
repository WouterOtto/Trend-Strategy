Let's revise the architecture to follow the next steps:
1. Select universe:
	1. Stock and ETF: all relevant exchanges (NYSE, NASDAQ, LSE, XETRA, AS, PA)
	2. Crypto: limited scope
2. Download data
	1. Mode Initial:
		1. Load all end-of-day prices per exchange for the last 2 years from EODHD by using the bulk load method.
		2. Load all end-of-day splits per exchange for the last 2 years from EODHD by using the bulk load method.
		3. Load all end-of-day dividends per exchange for the last 2 years from EODHD by using the bulk load method.
		4. Load all fundamentals per ticker from Yahoo.
	2. Mode Full New:
		1. Identify new tickers on the relevant exchanges and continue the next steps only for these new tickers.
		2. Load all end-of-day prices per exchange for the last 2 years from EODHD by using the bulk load method.
		3. Load all end-of-day splits per exchange for the last 2 years from EODHD by using the bulk load method.
		4. Load all end-of-day dividends per exchange for the last 2 years from EODHD by using the bulk load method.
		5. Load all fundamentals per ticker from Yahoo.
	3. Mode Delta:
		1. Load all end-of-day prices per exchange that are missing since the last load and today
		2. Load all end-of-day splits per exchange that are missing since the last load and today
		3. Load all end-of-day dividends per exchange that are missing since the last load and today
3. Filter tickers based:
	1. Liquidity criteria
	2. Market Cap (stock and ETF only)
4. Calculate all metrics
	1. Technical Indicators
	2. Momentum Analysis
	3. Position Sizing Logic
	4. Stop-Loss Conditions
	5. Exit Conditions
	6. Risk Metrics
5. Execution System
	1. Generate reports: price evolution, SMA50, SM200, Volume, ADX, ATR last 200 days
	2. Daily monitoring system
	3. Monthly rebalancing recommendations
6. Performance Monitoring System
	1. 3-6 month backtesting
	2. Portfolio performance review