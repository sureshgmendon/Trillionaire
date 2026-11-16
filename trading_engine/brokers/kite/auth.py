"""
Trillionaire Kite Broker - Automated Daily TOTP Authentication & Token Vault
Handles Zerodha Kite Connect's daily 6:00 AM IST access token expiration.
Workflow:
1. Validates existing cached token for today's IST trading date (Memory -> Redis -> File).
2. If expired or missing, executes automated headless login:
   - Login with KITE_USER_ID & KITE_PASSWORD
   - 2FA authentication using 6-digit TOTP from pyotp.TOTP(KITE_TOTP_SECRET).now()
   - Intercepts request_token from OAuth redirect
   - Exchanges request_token for Kite Connect access_token (kite.generate_session())
3. Caches access_token in Redis (trillionaire:auth:kite_token) and gitignored access_token.json.
4. Provides mockable HTTP transport for deterministic unit testing.
"""

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
import httpx
from loguru import logger
import pyotp

from config.settings import settings
from shared.constants import IST_TZ, KEY_KITE_TOKEN
from shared.exceptions import BrokerConnectionError

DEFAULT_TOKEN_CACHE_FILE = Path(__file__).resolve().parents[4] / "access_token.json"


class KiteAuthManager:
    """Automates daily headless Kite Connect login, TOTP generation, and token caching."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        user_id: Optional[str] = None,
        password: Optional[str] = None,
        totp_secret: Optional[str] = None,
        token_cache_path: Path = DEFAULT_TOKEN_CACHE_FILE,
        redis_url: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        kite_client: Optional[Any] = None,
    ):
        self.api_key = api_key or settings.kite_api_key
        self.api_secret = api_secret or settings.kite_api_secret
        self.user_id = user_id or settings.kite_user_id
        self.password = password or settings.kite_password
        self.totp_secret = totp_secret or settings.kite_totp_secret
        self.token_cache_path = token_cache_path
        self.redis_url = redis_url or settings.redis_url
        self._http_client = http_client
        self._kite_client = kite_client

        # Memory cache
        self._cached_token: Optional[str] = None
        self._cached_date: Optional[date] = None

    def generate_totp_code(self) -> str:
        """Generates real-time 6-digit TOTP code using Base32 secret."""
        if not self.totp_secret:
            raise BrokerConnectionError("KITE_TOTP_SECRET is required to generate 2FA code.")
        totp = pyotp.TOTP(self.totp_secret)
        code = totp.now()
        logger.debug(f"Generated headless 2FA TOTP code for {self.user_id}")
        return code

    def is_token_valid_for_today(self, token_data: Dict[str, Any], today_date: Optional[date] = None) -> bool:
        """Checks if a cached token dictionary is valid for the specified IST date."""
        if today_date is None:
            today_date = datetime.now(IST_TZ).date()

        token = token_data.get("access_token")
        token_date_str = token_data.get("date")

        if not token or not token_date_str:
            return False

        try:
            cached_dt = date.fromisoformat(token_date_str)
            return cached_dt == today_date
        except Exception:
            return False

    def _get_redis_token(self, today_date: date) -> Optional[str]:
        """Attempts to retrieve valid access token from Redis."""
        try:
            import redis
            r = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            raw = r.get(KEY_KITE_TOKEN)
            if raw:
                data = json.loads(raw)
                if self.is_token_valid_for_today(data, today_date):
                    logger.info("Retrieved valid Kite access_token from Redis cache.")
                    return data["access_token"]
        except Exception as e:
            logger.debug(f"Redis token lookup bypassed: {e}")
        return None

    def _get_disk_token(self, today_date: date) -> Optional[str]:
        """Attempts to retrieve valid access token from local gitignored JSON cache."""
        if not self.token_cache_path.exists():
            return None

        try:
            with open(self.token_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if self.is_token_valid_for_today(data, today_date):
                logger.info(f"Retrieved valid Kite access_token from disk ({self.token_cache_path}).")
                return data["access_token"]
        except Exception as e:
            logger.warning(f"Failed to read disk token cache: {e}")
        return None

    def _save_token(self, access_token: str, today_date: date) -> None:
        """Persists access token to memory, disk file, and Redis."""
        self._cached_token = access_token
        self._cached_date = today_date

        payload = {
            "date": today_date.isoformat(),
            "access_token": access_token,
            "saved_at_ist": datetime.now(IST_TZ).isoformat(),
        }

        # 1. Save to disk file
        try:
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"Persisted Kite access_token to {self.token_cache_path}")
        except Exception as e:
            logger.error(f"Failed to write disk token cache: {e}")

        # 2. Save to Redis
        try:
            import redis
            r = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            r.set(KEY_KITE_TOKEN, json.dumps(payload))
            logger.info(f"Persisted Kite access_token to Redis under {KEY_KITE_TOKEN}")
        except Exception as e:
            logger.debug(f"Redis token save bypassed: {e}")

    def get_valid_access_token(
        self,
        trade_date: Optional[date] = None,
        force_refresh: bool = False,
    ) -> str:
        """
        Returns active Kite access token for today.
        Reuses cached token if valid; otherwise automates headless login.
        """
        today_date = trade_date or datetime.now(IST_TZ).date()

        if not force_refresh:
            # 1. Memory check
            if self._cached_token and self._cached_date == today_date:
                return self._cached_token

            # 2. Redis check
            redis_tok = self._get_redis_token(today_date)
            if redis_tok:
                self._cached_token = redis_tok
                self._cached_date = today_date
                return redis_tok

            # 3. Disk check
            disk_tok = self._get_disk_token(today_date)
            if disk_tok:
                self._cached_token = disk_tok
                self._cached_date = today_date
                return disk_tok

        logger.info(f"No valid Kite access_token for {today_date}. Initiating automated headless login...")
        new_token = self.perform_headless_login(trade_date=today_date)
        self._save_token(new_token, today_date)
        return new_token

    def perform_headless_login(self, trade_date: Optional[date] = None) -> str:
        """
        Executes automated HTTP session login against Zerodha Kite:
        1. POST /api/login -> gets request_id
        2. POST /api/twofa -> submits TOTP 2FA code
        3. GET /connect/login?api_key=... -> extracts request_token
        4. kite.generate_session() -> returns access_token
        """
        if not self.user_id or not self.password or not self.api_key or not self.api_secret:
            raise BrokerConnectionError(
                "Incomplete Kite credentials: KITE_USER_ID, KITE_PASSWORD, KITE_API_KEY, "
                "and KITE_API_SECRET must be configured."
            )

        client = self._http_client or httpx.Client(
            follow_redirects=True,
            timeout=10.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )

        try:
            # 1. Step 1: Submit Credentials
            login_url = "https://kite.zerodha.com/api/login"
            login_resp = client.post(
                login_url,
                data={"user_id": self.user_id, "password": self.password},
            )
            login_json = login_resp.json()
            if login_json.get("status") != "success":
                err_msg = login_json.get("message", "Login credential submission failed")
                raise BrokerConnectionError(f"Kite headless login step 1 failed: {err_msg}")

            request_id = login_json["data"]["request_id"]
            logger.debug(f"Kite login step 1 success: request_id={request_id}")

            # 2. Step 2: Submit 2FA TOTP Code
            totp_code = self.generate_totp_code()
            twofa_url = "https://kite.zerodha.com/api/twofa"
            twofa_resp = client.post(
                twofa_url,
                data={
                    "user_id": self.user_id,
                    "request_id": request_id,
                    "twofa_value": totp_code,
                    "twofa_type": "totp",
                },
            )
            twofa_json = twofa_resp.json()
            if twofa_json.get("status") != "success":
                err_msg = twofa_json.get("message", "TOTP 2FA verification failed")
                raise BrokerConnectionError(f"Kite headless login step 2 (2FA) failed: {err_msg}")

            logger.debug("Kite login step 2 (TOTP 2FA) verified successfully.")

            # 3. Step 3: OAuth redirect to obtain request_token
            connect_url = f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"
            connect_resp = client.get(connect_url)

            # Extract request_token from final URL query params
            final_url = str(connect_resp.url)
            parsed_url = urlparse(final_url)
            query_params = parse_qs(parsed_url.query)

            request_token_list = query_params.get("request_token")
            if not request_token_list:
                raise BrokerConnectionError(
                    f"Failed to capture request_token from OAuth redirect URL: {final_url}"
                )

            request_token = request_token_list[0]
            logger.info("Captured valid Kite request_token via headless OAuth flow.")

            # 4. Step 4: Exchange request_token for access_token
            access_token = self._exchange_request_token(request_token)
            return access_token

        except BrokerConnectionError:
            raise
        except Exception as e:
            logger.error(f"Headless Kite login encounter unexpected error: {e}")
            raise BrokerConnectionError(f"Headless Kite login failure: {e}") from e
        finally:
            if self._http_client is None:
                client.close()

    def _exchange_request_token(self, request_token: str) -> str:
        """Exchanges request_token for access_token using KiteConnect SDK."""
        if self._kite_client is not None:
            session_data = self._kite_client.generate_session(
                request_token=request_token,
                api_secret=self.api_secret,
            )
            return session_data["access_token"]

        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=self.api_key)
        session_data = kite.generate_session(
            request_token=request_token,
            api_secret=self.api_secret,
        )
        return session_data["access_token"]

    def get_kite_client(self, force_refresh: bool = False) -> Any:
        """Returns initialized and authenticated KiteConnect instance."""
        token = self.get_valid_access_token(force_refresh=force_refresh)
        if self._kite_client is not None:
            self._kite_client.set_access_token(token)
            return self._kite_client

        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=self.api_key)
        kite.set_access_token(token)
        return kite
