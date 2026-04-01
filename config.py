import os
from dotenv import load_dotenv

load_dotenv()

# Kalshi API
KALSHI_API_BASE        = os.getenv("KALSHI_API_BASE", "https://demo-api.kalshi.co/trade-api/v2")
KALSHI_API_KEY_ID      = os.getenv("KALSHI_API_KEY_ID") or os.getenv("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# Logging
LOG_FILE  = "bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
