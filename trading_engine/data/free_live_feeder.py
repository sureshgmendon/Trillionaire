"""
Trillionaire Free Live NSE Market Feeder
Asynchronously streams live NSE equity and sector index quotes without requiring Zerodha API keys.
Polls Yahoo Finance / public NSE market data endpoints with concurrency limits,
normalizes incoming quotes to validated Tick schemas, and dispatches TickEvents to the EventBus.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import httpx
import yaml
from loguru import logger

from shared.constants import IST_TZ
from shared.enums import DataFeederType
from shared.schemas import Tick
from trading_engine.core.event_bus import EventBus, TickEvent, bus

# Sector Index mapping to Yahoo Finance symbols & Zerodha token IDs
DEFAULT_SECTOR_INDICES = {
    "NIFTY 50": {"yahoo": "^NSEI", "token": 256265},
    "NIFTY BANK": {"yahoo": "^NSEBANK", "token": 260105},
    "NIFTY IT": {"yahoo": "^CNXIT", "token": 259849},
    "NIFTY AUTO": {"yahoo": "^CNXAUTO", "token": 259601},
    "NIFTY PHARMA": {"yahoo": "^CNXPHARMA", "token": 260865},
    "NIFTY ENERGY": {"yahoo": "^CNXENERGY", "token": 260609},
    "NIFTY FMCG": {"yahoo": "^CNXFMCG", "token": 261889},
    "NIFTY METAL": {"yahoo": "^CNXMETAL", "token": 261377},
}

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


YAHOO_SYMBOL_OVERRIDES: Dict[str, str] = {
    "TMPV": "TMPV.NS",
    "TATAMOTORS": "TMPV.NS",
    "LTIM": "LTM.NS",
    "MCDOWELL-N": "UNITDSPR.NS",
}


class FreeLiveMarketFeeder:
    """
    Zero-cost asynchronous market data feeder for live NSE equities and sector indices.
    Conforms to the standard ticker lifecycle: start() and stop().
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        poll_interval_sec: float = 3.0,
        max_concurrency: int = 12,
        symbols_map: Optional[Dict[str, Dict[str, Any]]] = None,
        custom_client: Optional[httpx.AsyncClient] = None,
    ):
        self.bus = event_bus or bus
        self.poll_interval_sec = poll_interval_sec
        self.max_concurrency = max_concurrency
        self._custom_client = custom_client
        self._client: Optional[httpx.AsyncClient] = None

        # Tracked universe: tradingsymbol -> {token, yahoo_symbol, is_index}
        self.tracked_symbols: Dict[str, Dict[str, Any]] = symbols_map or {}
        if not self.tracked_symbols:
            self._load_universe_from_sector_map()

        self.latest_ticks: Dict[str, Tick] = {}
        self._is_running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _load_universe_from_sector_map(self) -> None:
        """Loads tracked symbols and tokens from config/sector_map.yaml."""
        # 1. Add benchmark sector indices
        for idx_name, meta in DEFAULT_SECTOR_INDICES.items():
            self.tracked_symbols[idx_name] = {
                "token": meta["token"],
                "yahoo_symbol": meta["yahoo"],
                "is_index": True,
            }

        # 2. Add individual stocks from sector_map.yaml
        sector_map_file = Path("config/sector_map.yaml")
        if sector_map_file.exists():
            try:
                with open(sector_map_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                for stock in data.get("stocks", []):
                    sym = stock["tradingsymbol"]
                    token = stock["instrument_token"]
                    self.tracked_symbols[sym] = {
                        "token": token,
                        "yahoo_symbol": YAHOO_SYMBOL_OVERRIDES.get(sym, f"{sym}.NS"),
                        "is_index": False,
                    }
            except Exception as e:
                logger.warning(f"[FREE LIVE FEED] Failed to load sector_map.yaml: {e}")

        logger.info(f"[FREE LIVE FEED] Initialized {len(self.tracked_symbols)} instruments for live polling")

    async def _fetch_quote(
        self,
        symbol: str,
        meta: Dict[str, Any],
        client: httpx.AsyncClient,
    ) -> Optional[Tick]:
        """Fetches live 1-minute quote data from Yahoo Finance and normalizes to Tick."""
        yahoo_sym = meta["yahoo_symbol"]
        token = meta["token"]
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?interval=1m&range=1d"

        async with self._semaphore:
            try:
                resp = await client.get(url, timeout=4.0)
                if resp.status_code != 200:
                    logger.debug(f"[FREE LIVE FEED] HTTP {resp.status_code} for {symbol} ({yahoo_sym})")
                    return None

                data = resp.json()
                results = data.get("chart", {}).get("result", [])
                if not results:
                    return None

                chart_meta = results[0].get("meta", {})
                price = chart_meta.get("regularMarketPrice")
                if price is None or price <= 0:
                    # Fallback to chart previous close or last indicator close
                    price = chart_meta.get("chartPreviousClose") or chart_meta.get("previousClose")

                if price is None or price <= 0:
                    return None

                raw_vol = chart_meta.get("regularMarketVolume", 0)
                vol = int(raw_vol) if raw_vol is not None else 0

                # Determine timestamp
                raw_time = chart_meta.get("regularMarketTime")
                if raw_time:
                    tick_dt = datetime.fromtimestamp(raw_time, tz=IST_TZ)
                else:
                    tick_dt = datetime.now(IST_TZ)

                # Generate clean bid/ask spread (0.05 tick size)
                ltp = round(float(price), 2)
                spread = max(0.05, round(ltp * 0.0002, 2))
                bid = round(max(0.05, ltp - spread), 2)
                ask = round(ltp + spread, 2)

                tick = Tick(
                    instrument_token=token,
                    tradingsymbol=symbol,
                    timestamp_ist=tick_dt,
                    ltp=ltp,
                    volume=vol,
                    bid=bid,
                    ask=ask,
                    oi=0,
                )
                return tick
            except httpx.HTTPError as he:
                logger.debug(f"[FREE LIVE FEED] Network timeout/error for {symbol}: {he}")
                return None
            except Exception as e:
                logger.debug(f"[FREE LIVE FEED] Parse error for {symbol}: {e}")
                return None

    async def poll_once(self) -> Dict[str, Tick]:
        """Performs a single complete round of parallel quote polling across all instruments."""
        if not self._client:
            return {}

        tasks = [
            self._fetch_quote(sym, meta, self._client)
            for sym, meta in self.tracked_symbols.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        emitted_ticks: Dict[str, Tick] = {}
        for res in results:
            if isinstance(res, Tick):
                emitted_ticks[res.tradingsymbol] = res
                self.latest_ticks[res.tradingsymbol] = res
                # Publish TickEvent to EventBus
                await self.bus.publish(TickEvent(tick=res))

        return emitted_ticks

    async def _poll_loop(self) -> None:
        """Continuous background loop polling live quotes at poll_interval_sec."""
        logger.info(f"[FREE LIVE FEED] Started live polling loop (Interval: {self.poll_interval_sec}s)")
        while self._is_running:
            try:
                start_t = asyncio.get_event_loop().time()
                ticks = await self.poll_once()
                elapsed = asyncio.get_event_loop().time() - start_t
                logger.debug(f"[FREE LIVE FEED] Emitted {len(ticks)} live ticks in {elapsed:.2f}s")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FREE LIVE FEED] Error in poll loop: {e}")

            await asyncio.sleep(self.poll_interval_sec)

    async def start(self) -> None:
        """Starts live market feeder and begins background polling loop."""
        if self._is_running:
            return

        self._is_running = True
        if self._custom_client is not None:
            self._client = self._custom_client
        else:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENTS},
                follow_redirects=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[FREE LIVE FEED] FreeLiveMarketFeeder online and streaming")

    async def stop(self) -> None:
        """Halts the market feeder and cleans up background tasks and HTTP client."""
        if not self._is_running:
            return

        logger.info("[FREE LIVE FEED] Halting FreeLiveMarketFeeder...")
        self._is_running = False

        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                # Check if current running loop matches task's loop before awaiting
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None

                if current_loop is not None and self._poll_task.get_loop() is current_loop:
                    await self._poll_task
            except (asyncio.CancelledError, RuntimeError):
                pass
            self._poll_task = None

        if self._client is not None and self._client != self._custom_client:
            try:
                await self._client.aclose()
            except (RuntimeError, Exception):
                pass
            self._client = None

        logger.info("[FREE LIVE FEED] FreeLiveMarketFeeder stopped successfully")
