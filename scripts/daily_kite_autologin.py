"""
Trillionaire Daily Zerodha Kite Auto-Login & Remote Token Dispatcher
Runs from residential broadband/local PC (where Zerodha does NOT block/rate-limit logins),
performs automated headless 2FA TOTP login, and securely pushes the daily access_token
to the production AWS Trillionaire Engine via POST /api/v1/kite/push-token.

Usage:
    python scripts/daily_kite_autologin.py
    python scripts/daily_kite_autologin.py --server-url http://32.236.106.39
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Dict, Optional

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx
from loguru import logger

from shared.constants import IST_TZ


def load_env_credentials(env_path: Optional[Path] = None) -> Dict[str, str]:
    """Loads environment variables from specified or default .env file."""
    search_paths = [
        env_path,
        REPO_ROOT / ".env",
        REPO_ROOT / ".env.production",
        Path.cwd() / ".env",
    ]
    env_vars: Dict[str, str] = {}

    for p in search_paths:
        if p and p.is_file():
            logger.info(f"Loading environment credentials from {p}")
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        clean_k = k.strip()
                        clean_v = v.strip().strip('"').strip("'")
                        env_vars[clean_k] = clean_v
            break

    # Merge with os.environ (system environment variables take precedence)
    for k, v in os.environ.items():
        env_vars[k] = v

    return env_vars


def mask(val: Optional[str], visible: int = 4) -> str:
    """Masks secret for secure log output."""
    if not val:
        return "<not set>"
    if len(val) <= visible:
        return "*" * len(val)
    return val[:visible] + "*" * (len(val) - visible)


def run_autologin_and_push(
    server_url: str,
    admin_key: str,
    env_vars: Dict[str, str],
    timeout_sec: float = 30.0,
) -> bool:
    """
    Executes headless login via KiteAuthManager and pushes access_token to the target server.
    """
    api_key = env_vars.get("KITE_API_KEY", "")
    api_secret = env_vars.get("KITE_API_SECRET", "")
    user_id = env_vars.get("KITE_USER_ID", "")
    password = env_vars.get("KITE_PASSWORD", "")
    totp_secret = env_vars.get("KITE_TOTP_SECRET", "")

    logger.info("=" * 70)
    logger.info("TRILLIONAIRE ZERO-TOUCH KITE AUTOLOGIN RUNNER")
    logger.info(f"Timestamp (IST)   : {datetime.now(IST_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Target Server     : {server_url}")
    logger.info(f"Zerodha User ID   : {user_id}")
    logger.info(f"Kite API Key      : {mask(api_key)}")
    logger.info(f"Kite API Secret   : {mask(api_secret)}")
    logger.info(f"Kite TOTP Secret  : {mask(totp_secret)}")
    logger.info(f"Admin API Key     : {mask(admin_key)}")
    logger.info("=" * 70)

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        logger.error(
            "Missing required Kite credentials! Ensure KITE_API_KEY, KITE_API_SECRET, "
            "KITE_USER_ID, KITE_PASSWORD, and KITE_TOTP_SECRET are set in .env"
        )
        return False

    # 1. Instantiate KiteAuthManager with residential credentials
    from trading_engine.brokers.kite.auth import KiteAuthManager

    auth_mgr = KiteAuthManager(
        api_key=api_key,
        api_secret=api_secret,
        user_id=user_id,
        password=password,
        totp_secret=totp_secret,
    )

    # 2. Perform headless login with TOTP generation
    logger.info("Executing automated headless login against Zerodha Kite Connect...")
    try:
        access_token = auth_mgr.perform_headless_login()
        logger.info(f"SUCCESS: Acquired valid Kite access_token: {mask(access_token, visible=6)}")
    except Exception as e:
        logger.error(f"FAILURE: Headless login failed: {e}")
        return False

    # 3. Push token to remote server endpoint
    endpoint = f"{server_url.rstrip('/')}/api/v1/kite/push-token"
    logger.info(f"Pushing access token to {endpoint} ...")

    headers = {
        "X-API-Key": admin_key,
        "Content-Type": "application/json",
    }
    payload = {"access_token": access_token}

    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(endpoint, headers=headers, json=payload)

        if resp.status_code == 200:
            data = resp.json()
            logger.info("SUCCESS: Remote engine confirmed token activation!")
            logger.info(f"Response: {json.dumps(data, indent=2)}")
            return True
        else:
            logger.error(f"FAILURE: Remote server returned HTTP {resp.status_code}: {resp.text}")
            return False

    except Exception as push_err:
        logger.error(f"FAILURE: Could not reach target server {endpoint}: {push_err}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Trillionaire Daily Kite Auto-Login & Token Push")
    parser.add_argument(
        "--server-url",
        type=str,
        default=None,
        help="Target Trillionaire API server URL (default: AWS_SERVER_URL from .env or http://32.236.106.39)",
    )
    parser.add_argument(
        "--admin-key",
        type=str,
        default=None,
        help="Trillionaire Admin API Key for authentication (default: TRILLIONAIRE_ADMIN_KEY from .env)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to custom .env file",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds (default: 30.0)",
    )

    args = parser.parse_args()

    env_vars = load_env_credentials(args.env_file)

    server_url = (
        args.server_url
        or env_vars.get("AWS_SERVER_URL")
        or env_vars.get("TRILLIONAIRE_SERVER_URL")
        or "http://32.236.106.39"
    )

    admin_key = (
        args.admin_key
        or env_vars.get("TRILLIONAIRE_ADMIN_KEY")
        or "trillionaire-algo-prod-key-99"
    )

    success = run_autologin_and_push(
        server_url=server_url,
        admin_key=admin_key,
        env_vars=env_vars,
        timeout_sec=args.timeout,
    )

    if success:
        logger.info("Daily Kite auto-login completed successfully. Exiting code 0.")
        sys.exit(0)
    else:
        logger.error("Daily Kite auto-login failed. Exiting code 1.")
        sys.exit(1)


if __name__ == "__main__":
    main()
