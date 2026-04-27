
import logging
from kite_auto_login import get_access_token
from dashboard import run_dashboard, start_ticker
from scanner import OCSScanner

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def start():
    log.info("🚀 Starting system...")

    token = get_access_token()
    log.info("✅ Token generated")

    scanner = OCSScanner(token)

    start_ticker(token, scanner)

    # MAIN PROCESS (important for Railway)
    run_dashboard()

if __name__ == "__main__":
    start()
