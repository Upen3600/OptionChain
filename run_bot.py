
import os
import logging
from kite_auto_login import get_access_token
from dashboard import run_dashboard, start_ticker
from scanner import OCSScanner

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def start():
    log.info("Starting system...")

    token = get_access_token()
    log.info(f"Token received: {token[:10]}...")

    scanner = OCSScanner(access_token=token)

    # Start ticker
    start_ticker(token, scanner)

    # Run dashboard as MAIN process (IMPORTANT)
    run_dashboard()

if __name__ == "__main__":
    start()
