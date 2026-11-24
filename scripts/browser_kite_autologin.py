import os
import sys
import json
import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import pyotp
import requests
from playwright.sync_api import sync_playwright
from kiteconnect import KiteConnect

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "browser_autologin.log"

def log(msg: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Failed to write to log file: {e}")

try:
    log("=== Starting Trillionaire Kite Browser Auto-Login ===")
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        raise FileNotFoundError(f".env file not found at {env_file}")

    env = {}
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")

    api_key = env.get("KITE_API_KEY", "")
    api_secret = env.get("KITE_API_SECRET", "")
    user_id = env.get("KITE_USER_ID", "")
    password = env.get("KITE_PASSWORD", "")
    totp_secret = env.get("KITE_TOTP_SECRET", "")
    server_url = env.get("TRILLIONAIRE_REMOTE_URL", "http://32.236.106.39").rstrip("/")
    admin_key = env.get("TRILLIONAIRE_ADMIN_KEY", "trillionaire-local-admin-123")

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        raise ValueError("Missing required Kite credentials in .env")

    log(f"Initiating headless Chromium login for user: {user_id}")
    request_token = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_request(req):
            global request_token
            if "request_token=" in req.url and not request_token:
                qs = parse_qs(urlparse(req.url).query)
                if "request_token" in qs:
                    request_token = qs["request_token"][0]

        page.on("request", handle_request)
        page.goto(f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}", wait_until="domcontentloaded")
        page.fill("input#userid", user_id)
        page.fill("input#password", password)
        page.click("button[type='submit']")

        totp_input = page.locator("input[maxlength='6']").first
        totp_input.wait_for(state="visible", timeout=15000)
        totp_input.fill(pyotp.TOTP(totp_secret).now())

        # Zerodha auto-submits on 6th digit; only click if still enabled and no token yet
        page.wait_for_timeout(1500)
        if not request_token:
            btn = page.locator("button[type='submit']:not([disabled])").first
            if btn.count() > 0:
                btn.click()

        for _ in range(30):
            if request_token:
                break
            page.wait_for_timeout(500)
        browser.close()

    if not request_token:
        raise RuntimeError("Failed to capture request_token via Chromium")

    log("Captured request_token successfully. Generating KiteConnect session...")
    kite = KiteConnect(api_key=api_key)
    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]

    log(f"Generated access_token ({access_token[:6]}...). Pushing to server: {server_url}...")
    res = requests.post(
        f"{server_url}/api/v1/kite/push-token",
        headers={"X-API-Key": admin_key},
        json={"access_token": access_token},
        timeout=15,
    )
    log(f"SUCCESS! Server response: {res.status_code} {res.text}")
except Exception as e:
    log(f"ERROR: {str(e)}")
    sys.exit(1)
