"""
Kalshi RSA authentication.

Kalshi v2 API uses PKCS1v15 + SHA-256 signing.
Each authenticated request must include three headers:
    KALSHI-ACCESS-KEY       — your API key ID
    KALSHI-ACCESS-TIMESTAMP — current Unix time as a string (milliseconds)
    KALSHI-ACCESS-SIGNATURE — base64(RSA_PKCS1v15_SHA256(timestamp + method + path))

The signature covers: f"{timestamp}{method.upper()}{path}"
where `path` is the request path WITHOUT the base URL and WITHOUT query params.

Example:
    timestamp = "1712000000000"
    method    = "GET"
    path      = "/trade-api/v2/markets"
    msg       = "1712000000000GET/trade-api/v2/markets"
"""

import base64
import time
from pathlib import Path
from typing import Dict

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config
from logger import get_logger

log = get_logger(__name__)

_private_key = None  # module-level cache


def _load_private_key():
    """Load the RSA private key from disk (PEM format). Cached after first load."""
    global _private_key
    if _private_key is not None:
        return _private_key

    key_path = config.KALSHI_PRIVATE_KEY_PATH
    if not key_path:
        raise ValueError(
            "KALSHI_PRIVATE_KEY_PATH is not set. "
            "Export it as an environment variable pointing to your PEM key file."
        )

    path = Path(key_path)
    if not path.exists():
        raise FileNotFoundError(f"Private key file not found: {key_path}")

    pem_bytes = path.read_bytes()

    try:
        # Try loading without a passphrase first
        _private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    except TypeError:
        raise ValueError(
            "Private key is encrypted with a passphrase. "
            "Please provide an unencrypted key or decrypt it first."
        )

    log.info("RSA private key loaded from %s", key_path)
    return _private_key


def get_auth_headers(method: str, path: str) -> Dict[str, str]:
    """
    Build the three Kalshi authentication headers for a single request.

    Args:
        method: HTTP verb, e.g. "GET", "POST", "DELETE"
        path:   Full URL path including /trade-api/v2 prefix,
                WITHOUT the host and WITHOUT query parameters.
                Example: "/trade-api/v2/portfolio/orders"

    Returns:
        Dict with KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
    """
    key_id = config.KALSHI_API_KEY_ID
    if not key_id:
        raise ValueError(
            "KALSHI_API_KEY_ID is not set. "
            "Export it as an environment variable containing your API key ID."
        )

    # Timestamp in milliseconds (Kalshi expects ms, not seconds)
    timestamp_ms = str(int(time.time() * 1000))

    message = f"{timestamp_ms}{method.upper()}{path}"
    message_bytes = message.encode("utf-8")

    private_key = _load_private_key()

    signature_bytes = private_key.sign(
        message_bytes,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    signature_b64 = base64.b64encode(signature_bytes).decode("utf-8")

    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature_b64,
    }


def verify_auth_config() -> bool:
    """
    Validate that auth config is present and the private key can be loaded.
    Returns True on success, raises on failure.
    """
    if not config.KALSHI_API_KEY_ID:
        raise ValueError("KALSHI_API_KEY_ID environment variable is not set.")
    if not config.KALSHI_PRIVATE_KEY_PATH:
        raise ValueError("KALSHI_PRIVATE_KEY_PATH environment variable is not set.")

    _load_private_key()  # Will raise if the file is missing or malformed
    log.info(
        "Auth config verified: key_id=%s, key_path=%s",
        config.KALSHI_API_KEY_ID,
        config.KALSHI_PRIVATE_KEY_PATH,
    )
    return True
