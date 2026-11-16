"""
Trillionaire Algo Engine - Shared Constants
Defines global namespaces, market timings, and statutory charges for Zerodha MIS intraday.
"""

from datetime import time
from zoneinfo import ZoneInfo

# ---------------------------------------------------------
# Application & Redis Namespaces
# ---------------------------------------------------------
APP_NAME = "Trillionaire Algo Engine"
APP_VERSION = "0.1.0"
REDIS_PREFIX = "trillionaire:"

# Redis Channel & Stream Keys
STREAM_TICKS = f"{REDIS_PREFIX}stream:ticks"
STREAM_CANDLES = f"{REDIS_PREFIX}stream:candles"
STREAM_SIGNALS = f"{REDIS_PREFIX}stream:signals"
STREAM_ORDERS = f"{REDIS_PREFIX}stream:orders"

KEY_STOCK_PROFILES = f"{REDIS_PREFIX}cache:stock_profiles"
KEY_SENTINEL_TAGS = f"{REDIS_PREFIX}cache:sentinel_tags"
KEY_CAPITAL_TIER = f"{REDIS_PREFIX}state:capital_tier"
KEY_SYSTEM_KILL_SWITCH = f"{REDIS_PREFIX}control:kill_switch"
KEY_DAILY_METRICS = f"{REDIS_PREFIX}metrics:daily"
KEY_KITE_TOKEN = f"{REDIS_PREFIX}auth:kite_token"

# ---------------------------------------------------------
# NSE Market Timing & Session Rules (IST)
# ---------------------------------------------------------
IST_TZ = ZoneInfo("Asia/Kolkata")
MARKET_PRE_OPEN = time(9, 0, 0)
MARKET_OPEN = time(9, 15, 0)

# Algorithmic Trading Window Constraints
ENTRY_WINDOW_START = time(9, 25, 0)       # No new entries before 09:25 IST
ENTRY_WINDOW_END = time(14, 30, 0)         # No new entries after 14:30 IST
FORCE_SQUARE_OFF_TRIGGER = time(15, 10, 0) # Auto liquidate all open MIS positions at 15:10 IST
MARKET_CLOSE = time(15, 30, 0)

# ---------------------------------------------------------
# Zerodha Equity MIS Intraday Statutory Charge Rates
# ---------------------------------------------------------
# Brokerage: 0.03% or Rs 20 per executed order, whichever is lower
BROKERAGE_PCT = 0.0003
BROKERAGE_CAP_INR = 20.0

# Securities Transaction Tax (STT): 0.025% on Sell side only for equity intraday
STT_INTRADAY_SELL_RATE = 0.00025

# Exchange Transaction Charges (NSE): 0.00297% of turnover
NSE_EXCHANGE_TURNOVER_RATE = 0.0000297

# Goods and Services Tax (GST): 18% on (Brokerage + Exchange Charges + SEBI Charges)
GST_RATE = 0.18

# SEBI Turnover Charges: Rs 10 per crore (0.0001%)
SEBI_TURNOVER_RATE = 0.000001

# Stamp Duty: 0.003% (Rs 300 per crore) on Buy side turnover
STAMP_DUTY_BUY_RATE = 0.00003
