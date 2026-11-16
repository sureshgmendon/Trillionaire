# Trillionaire - Automated NSE MIS Intraday Algorithmic Engine

Production-grade quantitative trading platform targeting Zerodha Kite Connect for algorithmic execution on top liquid NSE NIFTY 50 equities and index derivatives.

---

## 🏛️ Monorepo Architecture

```
Trillionaire/
├── config/             # Runtime settings, capital sizing tiers, and sector universe maps
│   ├── settings.py     # Pydantic-settings environment parser
│   ├── capital_tiers.yaml  # Tier 0 to Tier 3 capital & drawdown rules
│   └── sector_map.yaml     # Top 25 NSE NIFTY 50 stock-to-sector mappings
├── shared/             # Shared domain kernel (Pydantic v2 schemas, enums, constants, exceptions)
│   ├── schemas.py      # Tick, Candle, Signal, Order, Position, StockProfile, SentinelTag
│   ├── enums.py        # TradingMode, ClusterRegime, SentinelStatus, Side, OrderType, OrderStatus
│   ├── constants.py    # Redis namespaces, IST market timings, statutory charge rates
│   └── exceptions.py   # Domain risk and execution exception hierarchy
├── research_lab/       # Historical data ingestion, regime profiling, and backtesting
│   ├── data_loader/    # Kite historical API & tick parquet persistence
│   ├── profiler/       # Beta, ADR%, RVOL cluster classification
│   ├── backtester/     # Vectorized & event-driven backtesting engine
│   └── artifacts/      # Backtest reports, equity curves, tearsheets
├── sentinel/           # Pre-trade and real-time risk triage
│   ├── news_triage/    # NLP headline scoring and event-risk sentiment
│   └── calendar_guard/ # RBI policy, earnings, and macro lockout guards
├── trading_engine/     # Live execution and risk runtime
│   ├── core/           # Engine orchestration lifecycle and state machine
│   ├── data/           # Real-time Kite WebSocket ticker & candle aggregator
│   ├── strategy/       # Trend momentum, mean reversion, and breakout strategies
│   ├── risk/           # Pre-trade capital allocation & portfolio circuit breakers
│   ├── execution/      # Smart limit/SL-limit order router and fill reconciler
│   └── brokers/kite/   # Zerodha Kite Connect REST & WebSocket client wrapper
├── api_bridge/         # High-speed FastAPI REST & WebSocket telemetry gateway
│   ├── routes/         # Operator endpoints (orders, positions, kill-switch)
│   ├── ws/             # Real-time WebSocket streaming to frontend
│   └── main.py         # ASGI application entrypoint
├── web_dashboard/      # Next.js 14 Cockpit (Scheduled for Sprint 9)
├── scripts/            # DevOps automation & environment bootstrap scripts
├── tests/              # Pytest test suite (unit & integration)
└── pyproject.toml      # Project metadata & test configuration
```

---

## 🚀 Quickstart

### 1. Prerequisites
- Python 3.11+
- Redis (v7+)
- PostgreSQL (v15+)

### 2. Environment Initialization
Run the bootstrap script to create a virtual environment and configure `.env`:
```powershell
.\scripts\init_dev.ps1
```

### 3. Run Test Suite
```bash
python -m pytest tests/ -v
```
