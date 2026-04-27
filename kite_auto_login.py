
import os
import pyotp
import logging
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

API_KEY       = os.environ.get("API_KEY")
API_SECRET    = os.environ.get("API_SECRET")
USER_ID       = os.environ.get("KITE_USER_ID")
PASSWORD      = os.environ.get("KITE_PASSWORD")
TOTP_SECRET   = os.environ.get("TOTP_SECRET")

def get_access_token():
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()

        page.goto(login_url)

        page.fill("#userid", USER_ID)
        page.fill("#password", PASSWORD)
        page.click("button[type='submit']")

        page.wait_for_timeout(2000)

        totp = pyotp.TOTP(TOTP_SECRET).now()
        page.fill("input[type='number']", totp)
        page.click("button[type='submit']")

        page.wait_for_timeout(3000)

        url = page.url
        browser.close()

    if "request_token=" not in url:
        raise Exception("Login failed, request_token not found")

    request_token = url.split("request_token=")[1].split("&")[0]

    data = kite.generate_session(request_token, api_secret=API_SECRET)
    return data["access_token"]
