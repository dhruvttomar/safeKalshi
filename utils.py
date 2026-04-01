"""
Shared HTTP helper with rate-limit backoff, and misc utility functions.
"""

import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

import config
from auth import get_auth_headers
from logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# HTTP session (shared across the process for connection pooling)
# ---------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})


def _path_from_url(url: str) -> str:
    """
    Extract the path component from a full URL.
    e.g. "https://demo-api.kalshi.co/trade-api/v2/markets" → "/trade-api/v2/markets"
    """
    parsed = urlparse(url)
    return parsed.path


def api_request(
    method: str,
    url: str,
    *,
    authenticated: bool = False,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    max_retries: int = 5,
    base_backoff: float = 1.0,
) -> Any:
    """
    Make a Kalshi API request with automatic retry on 429 / 5xx.

    Args:
        method:        HTTP verb (GET, POST, DELETE, …)
        url:           Full URL
        authenticated: If True, add RSA auth headers
        params:        Query string parameters
        json_body:     Request body (for POST/PUT)
        max_retries:   Maximum number of retry attempts
        base_backoff:  Initial backoff in seconds (doubles each retry)

    Returns:
        Parsed JSON response as a Python object.

    Raises:
        requests.HTTPError for non-retryable errors (4xx except 429).
        RuntimeError if all retries are exhausted.
    """
    headers: Dict[str, str] = {}
    if authenticated:
        path = _path_from_url(url)
        headers.update(get_auth_headers(method, path))

    attempt = 0
    backoff = base_backoff

    while attempt <= max_retries:
        try:
            response = _session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=15,
            )

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", backoff))
                log.warning(
                    "Rate limited (429) on %s %s — sleeping %.1fs (attempt %d/%d)",
                    method,
                    url,
                    retry_after,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(retry_after)
                backoff = min(backoff * 2, 60)
                attempt += 1
                # Rebuild auth headers with a fresh timestamp for the retry
                if authenticated:
                    path = _path_from_url(url)
                    headers.update(get_auth_headers(method, path))
                continue

            if response.status_code >= 500:
                log.warning(
                    "Server error %d on %s %s — sleeping %.1fs (attempt %d/%d)",
                    response.status_code,
                    method,
                    url,
                    backoff,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                attempt += 1
                if authenticated:
                    path = _path_from_url(url)
                    headers.update(get_auth_headers(method, path))
                continue

            response.raise_for_status()
            return response.json()

        except requests.exceptions.ConnectionError as exc:
            if attempt >= max_retries:
                raise
            log.warning(
                "Connection error on %s %s: %s — retrying in %.1fs",
                method,
                url,
                exc,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            attempt += 1

    raise RuntimeError(
        f"All {max_retries} retries exhausted for {method} {url}"
    )


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def parse_price(value: Any) -> float:
    """
    Safely parse a Kalshi price value to a float.
    Kalshi returns prices as dollar strings like "0.9200" or sometimes floats.
    Returns 0.0 if the value is None or unparseable.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def dollars_to_cents(dollars: float) -> int:
    """Convert dollar price (0.92) to integer cents (92)."""
    return round(dollars * 100)


def cents_to_dollars(cents: int) -> float:
    """Convert integer cents (92) to dollar price (0.92)."""
    return cents / 100.0


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_ts() -> float:
    """Current Unix timestamp as a float."""
    return time.time()


def elapsed_sec(since_ts: float) -> float:
    """Seconds elapsed since `since_ts`."""
    return time.time() - since_ts
