"""
gdrive.py — Append loss records to a Google Sheet.

Setup
-----
1. Create a Google Cloud project and enable Google Sheets API.
2. Create a service account; download the JSON key file.
3. Share the target sheet with the service account email (editor role).
4. Set env vars:
       GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account-key.json
       GOOGLE_LOSSES_SHEET_ID=1fSkXFTD2lQUvMun5t7csowy7HWvRtRmUtW-iGCWBK1c
       GOOGLE_LOSSES_WORKSHEET_GID=736645775   (optional, falls back to tab name)
       GOOGLE_LOSSES_WORKSHEET=Losses          (tab name fallback)
"""

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("gdrive")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
_SHEET_ID             = os.getenv("GOOGLE_LOSSES_SHEET_ID", "1fSkXFTD2lQUvMun5t7csowy7HWvRtRmUtW-iGCWBK1c")
_WORKSHEET_GID        = os.getenv("GOOGLE_LOSSES_WORKSHEET_GID", "736645775")
_WORKSHEET_NAME       = os.getenv("GOOGLE_LOSSES_WORKSHEET", "Losses")

_HEADERS = [
    "Timestamp (UTC)", "Ticker", "League",
    "Away", "Home", "Leading Team", "Point Diff",
    "Period", "Time Remaining (min)",
    "Entry $", "Fill $", "Contracts", "Cost $", "PnL $",
]

_ws = None  # gspread.Worksheet


def _get_worksheet():
    """Lazy-init: connect once, reuse thereafter."""
    global _ws
    if _ws is not None:
        return _ws

    if not _SERVICE_ACCOUNT_JSON:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — losses will not sync to Google Sheets")
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(_SERVICE_ACCOUNT_JSON, scopes=_SCOPES)
        gc    = gspread.authorize(creds)
        sh    = gc.open_by_key(_SHEET_ID)

        # Prefer lookup by GID (stable even if tab is renamed)
        if _WORKSHEET_GID:
            try:
                _ws = sh.get_worksheet_by_id(int(_WORKSHEET_GID))
                log.info("Google Sheets connected: sheet=%s gid=%s", _SHEET_ID[:12] + "…", _WORKSHEET_GID)
                return _ws
            except Exception:
                pass  # fall through to name lookup

        # Fall back to name; create the tab if missing
        existing = [ws.title for ws in sh.worksheets()]
        if _WORKSHEET_NAME in existing:
            _ws = sh.worksheet(_WORKSHEET_NAME)
        else:
            _ws = sh.add_worksheet(title=_WORKSHEET_NAME, rows=1000, cols=len(_HEADERS))
            _ws.append_row(_HEADERS)
            log.info("Created worksheet '%s' with headers", _WORKSHEET_NAME)

        log.info("Google Sheets connected: sheet=%s tab=%s", _SHEET_ID[:12] + "…", _WORKSHEET_NAME)
        return _ws

    except Exception as exc:
        log.error("Google Sheets init failed: %s", exc)
        _ws = None
        return None


def log_loss(
    *,
    ticker: str,
    league: str,
    away_team: str,
    home_team: str,
    leading_team: str,
    diff: int,
    period: int,
    time_remaining_sec: float,
    entry_price: float,
    fill_price: float,
    contracts: int,
    cost: float,
    pnl: float,
) -> None:
    """Append one loss row to the Google Sheet. Errors are logged, not raised."""
    ws = _get_worksheet()
    if ws is None:
        return

    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    row = [
        ts,
        ticker,
        league,
        away_team,
        home_team,
        leading_team,
        diff,
        period,
        round(time_remaining_sec / 60, 1),
        round(entry_price, 4),
        round(fill_price, 4),
        contracts,
        round(cost, 2),
        round(pnl, 2),
    ]

    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("Loss logged to Google Sheets: %s  PnL $%.2f", ticker, pnl)
    except Exception as exc:
        log.error("Failed to append loss to Google Sheets: %s", exc)
        global _ws
        _ws = None  # reset so next call retries the connection
