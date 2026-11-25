"""
Trillionaire Kite Broker - Daily NSE Instrument Master Sync & Surveillance Guard
Synchronizes NSE instrument tokens, tick sizes, and lot sizes for live execution:
1. Downloads NSE instrument dump (kite.instruments("NSE") or cached JSON after 07:30 IST).
2. Maps tradingsymbol <-> instrument_token and tick_size strictly 1-to-1 for equities & indices.
3. ASM/GSM and Restricted Series Guard: Excludes Trade-to-Trade or surveillance series (BE, BZ, T2T, IL, SM).
4. Mathematical Tick-Size Rounding: Rounds all limit and marketable prices strictly to NSE tick sizes.
"""

from datetime import datetime, time
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from loguru import logger

from config.settings import settings
from shared.constants import IST_TZ
from shared.exceptions import BrokerConnectionError

INSTRUMENT_CACHE_FILE = (
    Path(__file__).resolve().parents[3] / "research_lab" / "artifacts" / "nse_instruments_cache.json"
)

# NSE Restricted / Surveillance series to exclude from trading
RESTRICTED_SERIES = {"BE", "BZ", "T2T", "SM", "IL", "ST", "BL"}
DAILY_CACHE_REFRESH_TIME = time(7, 30, 0)


def round_to_tick_size(price: float, tick_size: float = 0.05) -> float:
    """
    Rounds price strictly to the nearest valid exchange tick-size increment.
    Example: 2502.48 with tick_size 0.05 -> 2502.50.
    """
    if tick_size <= 0:
        return round(price, 2)
    inv = 1.0 / tick_size
    return round(round(price * inv) / inv, 2)


class InstrumentSync:
    """Synchronizes daily NSE instrument definitions and provides unique token & tick-size lookups."""

    def __init__(
        self,
        kite_client: Optional[Any] = None,
        cache_path: Path = INSTRUMENT_CACHE_FILE,
        asm_gsm_symbols: Optional[Set[str]] = None,
    ):
        self.kite = kite_client
        self.cache_path = cache_path
        self.asm_gsm_symbols: Set[str] = asm_gsm_symbols or set()

        # Lookups (strictly 1-to-1)
        self.symbol_to_token: Dict[str, int] = {}
        self.token_to_symbol: Dict[int, str] = {}
        self.symbol_to_tick_size: Dict[str, float] = {}
        self.symbol_to_lot_size: Dict[str, int] = {}
        self.active_instruments: Dict[str, Dict[str, Any]] = {}
        self.last_sync_date: Optional[str] = None

        # Load cache if available, otherwise seed canonical baseline from sector_map.yaml
        loaded = self.load_cache_from_disk()
        if not loaded or len(self.symbol_to_token) == 0:
            self._seed_from_sector_map()

    def _seed_from_sector_map(self) -> int:
        """Populates fallback 1-to-1 symbol/token mappings from config/sector_map.yaml."""
        try:
            smap = settings.load_sector_map()
            count = 0
            for idx_name, idx_info in smap.get("benchmark_indices", {}).items():
                tok = int(idx_info.get("token", 0))
                if idx_name and tok > 0:
                    self.symbol_to_token[idx_name] = tok
                    self.token_to_symbol[tok] = idx_name
                    self.symbol_to_tick_size.setdefault(idx_name, 0.05)
                    self.symbol_to_lot_size.setdefault(idx_name, 1)
                    count += 1

            for item in smap.get("stocks", []):
                sym = item.get("tradingsymbol", "")
                tok = int(item.get("instrument_token", 0))
                if not sym or tok <= 0:
                    continue
                if tok in self.token_to_symbol and self.token_to_symbol[tok] != sym:
                    logger.error(
                        f"[INSTRUMENT AUDIT] Duplicate token {tok} in sector_map.yaml: "
                        f"{self.token_to_symbol[tok]} vs {sym}. Skipping collision."
                    )
                    continue
                self.symbol_to_token[sym] = tok
                self.token_to_symbol[tok] = sym
                self.symbol_to_tick_size.setdefault(sym, 0.05)
                self.symbol_to_lot_size.setdefault(sym, 1)
                count += 1
            if count > 0:
                logger.info(f"[INSTRUMENT SYNC] Seeded {count} unique symbols/indices from sector_map.yaml")
            return count
        except Exception as e:
            logger.debug(f"[INSTRUMENT SYNC] Could not seed from sector_map.yaml: {e}")
            return 0

    def is_daily_cache_fresh(self, now_ist: Optional[datetime] = None) -> bool:
        """Returns True if today's NSE instrument cache has already been synced after 07:30 IST."""
        dt = now_ist or datetime.now(IST_TZ)
        today_iso = dt.date().isoformat()
        if len(self.symbol_to_token) == 0 or self.last_sync_date != today_iso:
            return False
        return dt.time() >= DAILY_CACHE_REFRESH_TIME

    def sync_instruments(
        self,
        raw_instruments: Optional[List[Dict[str, Any]]] = None,
        force: bool = False,
    ) -> int:
        """
        Synchronizes instrument universe from live KiteConnect dump or provided dataset.
        Caches daily instruments() locally after 07:30 IST to avoid redundant API calls.
        Filters out surveillance-restricted instruments (BE, BZ, ASM, GSM).
        """
        if raw_instruments is None:
            if not force and self.is_daily_cache_fresh():
                logger.info(
                    f"[INSTRUMENT SYNC] Reusing fresh daily cache for {self.last_sync_date} "
                    f"({len(self.symbol_to_token)} instruments)."
                )
                return len(self.symbol_to_token)

            if self.kite is None:
                logger.warning("No KiteConnect client provided; relying on local instrument cache.")
                return len(self.symbol_to_token)
            try:
                logger.info("Downloading fresh NSE instrument master from Kite Connect...")
                raw_instruments = self.kite.instruments("NSE")
            except Exception as e:
                logger.error(f"Failed to fetch live instruments from Kite Connect: {e}")
                if self.load_cache_from_disk() and len(self.symbol_to_token) > 0:
                    return len(self.symbol_to_token)
                raise BrokerConnectionError(f"Could not download or load NSE instruments: {e}") from e

        symbol_token_map: Dict[str, int] = {}
        token_symbol_map: Dict[int, str] = {}
        tick_size_map: Dict[str, float] = {}
        lot_size_map: Dict[str, int] = {}
        active_map: Dict[str, Dict[str, Any]] = {}

        excluded_count = 0

        for item in raw_instruments:
            sym = item.get("tradingsymbol", "")
            token = int(item.get("instrument_token", 0))
            inst_type = item.get("instrument_type", "")
            segment = item.get("segment", "")
            tick_size = float(item.get("tick_size", 0.05))
            lot_size = int(item.get("lot_size", 1))

            if not sym or token <= 0:
                continue

            # Enforce strict 1-to-1 token uniqueness
            if token in token_symbol_map and token_symbol_map[token] != sym:
                logger.warning(
                    f"[INSTRUMENT COLLISION] Token {token} already mapped to {token_symbol_map[token]}; skipping {sym}"
                )
                excluded_count += 1
                continue

            # 1. Handle Indices (NIFTY 50, NIFTY BANK, NIFTY IT, etc.)
            if inst_type == "INDEX" or segment == "INDICES":
                symbol_token_map[sym] = token
                token_symbol_map[token] = sym
                tick_size_map[sym] = tick_size
                lot_size_map[sym] = lot_size
                active_map[sym] = item
                continue

            # 2. Filter Equities: Exclude non-EQ series
            if inst_type in RESTRICTED_SERIES:
                excluded_count += 1
                continue

            # 3. Filter ASM/GSM restricted equities
            if sym in self.asm_gsm_symbols:
                logger.warning(f"Excluding {sym}: Flagged under ASM/GSM surveillance list")
                excluded_count += 1
                continue

            # Keep valid EQ series
            if inst_type == "EQ" or segment == "NSE":
                symbol_token_map[sym] = token
                token_symbol_map[token] = sym
                tick_size_map[sym] = tick_size
                lot_size_map[sym] = lot_size
                active_map[sym] = item

        self.symbol_to_token = symbol_token_map
        self.token_to_symbol = token_symbol_map
        self.symbol_to_tick_size = tick_size_map
        self.symbol_to_lot_size = lot_size_map
        self.active_instruments = active_map
        self.last_sync_date = datetime.now(IST_TZ).date().isoformat()

        logger.info(
            f"Synchronized {len(symbol_token_map)} valid instruments ({excluded_count} restricted/surveillance excluded)"
        )

        # Save to disk cache
        self._save_cache_to_disk()
        return len(symbol_token_map)

    def load_cache_from_disk(self) -> bool:
        """Loads cached instrument data from JSON file if available."""
        if not self.cache_path.exists():
            return False

        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            s2t = {k: int(v) for k, v in data.get("symbol_to_token", {}).items()}
            t2s = {int(k): v for k, v in data.get("token_to_symbol", {}).items()}
            if len(s2t) == 0 or len(t2s) == 0:
                return False
            self.symbol_to_token = s2t
            self.token_to_symbol = t2s
            self.symbol_to_tick_size = {k: float(v) for k, v in data.get("symbol_to_tick_size", {}).items()}
            self.symbol_to_lot_size = {k: int(v) for k, v in data.get("symbol_to_lot_size", {}).items()}
            self.last_sync_date = data.get("sync_date")
            logger.info(f"Loaded {len(self.symbol_to_token)} instrument tokens from disk cache: {self.cache_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to load instrument cache: {e}")
            return False

    def _save_cache_to_disk(self) -> None:
        """Saves current synchronized instruments to disk."""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            now_ist = datetime.now(IST_TZ)
            payload = {
                "sync_date": self.last_sync_date or now_ist.date().isoformat(),
                "synced_at_ist": now_ist.isoformat(),
                "symbol_to_token": self.symbol_to_token,
                "token_to_symbol": {str(k): v for k, v in self.token_to_symbol.items()},
                "symbol_to_tick_size": self.symbol_to_tick_size,
                "symbol_to_lot_size": self.symbol_to_lot_size,
            }
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"Persisted instrument master cache to {self.cache_path}")
        except Exception as e:
            logger.error(f"Failed to persist instrument cache: {e}")

    def get_token(self, symbol: str) -> Optional[int]:
        """Returns Kite instrument token for symbol."""
        return self.symbol_to_token.get(symbol)

    def get_symbol(self, token: int) -> Optional[str]:
        """Returns tradingsymbol for Kite instrument token."""
        return self.token_to_symbol.get(token)

    def get_tick_size(self, symbol: str) -> float:
        """Returns tick size for symbol (default Rs. 0.05)."""
        return self.symbol_to_tick_size.get(symbol, 0.05)

    def round_price(self, symbol: str, price: float) -> float:
        """Rounds price to symbol's exact NSE tick size."""
        tick_sz = self.get_tick_size(symbol)
        return round_to_tick_size(price, tick_sz)
