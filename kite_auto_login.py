
import os
from kiteconnect import KiteConnect

def get_access_token(force_refresh=False):
    api_key = os.environ["API_KEY"]
    api_secret = os.environ["API_SECRET"]

    kite = KiteConnect(api_key=api_key)

    # ⚠️ MANUAL TOKEN METHOD (NO PLAYWRIGHT)
    request_token = os.environ.get("REQUEST_TOKEN")

    if not request_token:
        raise Exception("REQUEST_TOKEN missing in Railway env")

    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]
